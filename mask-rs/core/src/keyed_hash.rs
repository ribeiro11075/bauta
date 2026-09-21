//! HMAC-SHA256 under a per-domain subkey, as `bauta.masking.KeyedHash`
//! computes it.
//!
//! Every function here has a Python counterpart whose output it must match byte
//! for byte. A difference is not a bug that shows up as a wrong answer -- it is a
//! silent key change, which reads downstream as every join quietly failing. The
//! vectors in `mask-rs/vectors/reference.json` are the contract; `tests/vectors.rs`
//! checks it.

use num_bigint::BigUint;
use ring::{digest, hmac};

/// SHA-256's compression block, which is what HMAC pads its key out to.
const BLOCK_SIZE: usize = 64;


/// Feistel rounds for `permute`. FF1 and FF3-1 use 10 and 8; Python picks the
/// conservative end, and this has to agree with it.
const FEISTEL_ROUNDS: u8 = 10;

pub struct KeyedHash {
    /// The two pad states, fed their padded key and then cloned per value --
    /// the same shortcut the Python takes, and the reason neither pays to
    /// re-key per value.
    ///
    /// Built against `ring::digest` rather than `ring::hmac`, which allocates a
    /// `Key` and a `Tag` per signature: at forty digests a value that machinery
    /// cost more than the hashing (0.183 us a digest through `hmac`, against
    /// two compressions' 0.052 us).
    ///
    /// The backend matters more than anything else here: `ring` reaches
    /// 2.48 GB/s on this machine against the pure-Rust `sha2` crate's 0.32,
    /// because it uses the CPU's SHA-256 instructions. A build that silently
    /// fell back to software would give up most of the port's reason to exist,
    /// so `tests/backend.rs` fails if the throughput collapses.
    inner: digest::Context,
    outer: digest::Context,
}

impl KeyedHash {
    pub fn new(key: &str, domain: &str) -> Self {
        let mut message = Vec::with_capacity(b"domain\x00".len() + domain.len());
        message.extend_from_slice(b"domain\x00");
        message.extend_from_slice(domain.as_bytes());

        let subkey = hmac::sign(&hmac::Key::new(hmac::HMAC_SHA256, key.as_bytes()), &message);

        // The subkey is a SHA-256 digest, so it is always shorter than the
        // 64-byte block and is zero-padded rather than hashed first -- the
        // same assumption the Python makes, and asserted there too.
        Self::fromSubkey(subkey.as_ref().try_into().expect("a SHA-256 digest is 32 bytes"))
    }

    /// A KeyedHash from an already-derived subkey.
    ///
    /// What the Python layer hands over, so the masking key itself never
    /// crosses into this crate -- `KeyedHash` in Python holds only the subkey
    /// too, and security.md is deliberate that the key stays out of anything
    /// that might end up in a repr or a traceback.
    pub fn fromSubkey(subkey: &[u8; 32]) -> Self {
        let mut padded = [0u8; BLOCK_SIZE];
        padded[..32].copy_from_slice(subkey);

        let mut innerPad = padded;
        let mut outerPad = padded;
        for index in 0..BLOCK_SIZE {
            innerPad[index] ^= 0x36;
            outerPad[index] ^= 0x5c;
        }

        let mut inner = digest::Context::new(&digest::SHA256);
        inner.update(&innerPad);
        let mut outer = digest::Context::new(&digest::SHA256);
        outer.update(&outerPad);

        Self { inner, outer }
    }

    /// `HMAC-SHA256(subkey, purpose || 0x00 || message)`.
    pub fn digest(&self, message: &[u8], purpose: &[u8]) -> [u8; 32] {
        let mut inner = self.inner.clone();
        inner.update(purpose);
        inner.update(&[0u8]);
        inner.update(message);

        let mut outer = self.outer.clone();
        outer.update(inner.finish().as_ref());

        let mut out = [0u8; 32];
        out.copy_from_slice(outer.finish().as_ref());

        out
    }

    /// `length` pseudorandom bytes, in counter mode past one digest's 32.
    ///
    /// The counter goes into the *purpose*, not the message, so the buffer the
    /// caller passed is never copied per block.
    pub fn expand(&self, message: &[u8], length: usize, purpose: &[u8]) -> Vec<u8> {
        let mut output = Vec::with_capacity(length.next_multiple_of(32));
        let mut counter: u32 = 0;

        while output.len() < length {
            let mut counted = Vec::with_capacity(purpose.len() + 5);
            counted.extend_from_slice(purpose);
            counted.push(b'#');
            counted.extend_from_slice(&counter.to_be_bytes());

            output.extend_from_slice(&self.digest(message, &counted));
            counter += 1;
        }

        output.truncate(length);
        output
    }

    /// An integer in `[0, upper)`. Python reduces 32 spare bytes, which keeps
    /// the modulo bias below 2**-128 for any `upper` this package asks for.
    pub fn below(&self, message: &[u8], upper: &BigUint, purpose: &[u8]) -> BigUint {
        let width = std::cmp::max(32, (upper.bits() as usize).div_ceil(8) + 32);

        BigUint::from_bytes_be(&self.expand(message, width, purpose)) % upper
    }

    /// A float in `[0, 1)` with 53 bits of resolution.
    pub fn unit(&self, message: &[u8], purpose: &[u8]) -> f64 {
        let digest = self.digest(message, purpose);
        let mut seven = [0u8; 8];
        seven[1..].copy_from_slice(&digest[..7]);

        ((u64::from_be_bytes(seven) >> 3) as f64) / ((1u64 << 53) as f64)
    }

    /// A keyed permutation of `0..size`: every input maps to a distinct output.
    ///
    /// A balanced Feistel network over the smallest even bit width covering
    /// `size`, cycle-walked back into range. Python's construction, including
    /// its choice to round the width up to an even number of bits -- which
    /// costs cycle-walking passes, and is deliberately kept, because changing
    /// it would change every mask.
    ///
    /// Run in machine integers where each half fits a u64, which is any domain
    /// up to 2**128 and so every identifier in practice: the same bytes are
    /// hashed, without a BigUint allocated per round. That was 40% of an
    /// 11-character `key` mask. Wider domains take the BigUint path.
    pub fn permute(&self, size: &BigUint, value: &BigUint, purpose: &[u8]) -> BigUint {
        if size <= &BigUint::from(1u8) {
            return value.clone();
        }

        let mut network = Network::new(size, purpose);
        if network.half <= 64 {
            BigUint::from(self.permuteNarrow(&mut network, size, value))
        } else {
            self.permuteWide(&mut network, size, value)
        }
    }

    /// `permute` with each half in a u64.
    fn permuteNarrow(&self, network: &mut Network, size: &BigUint, value: &BigUint) -> u128 {
        let half = network.half;
        let halfBytes = network.halfBytes;
        // size - 1, since a domain of exactly 2**128 is itself one past a u128.
        let last = u128::try_from(size - 1u8).expect("halves of at most 64 bits bound size by 2**128");
        let halfMask = u64::MAX >> (64 - half);
        let mut expanded = [0u8; 32];
        let mut result = u128::try_from(value).expect("value is below size");

        loop {
            let mut left = (result >> half) as u64;
            let mut right = result as u64 & halfMask;

            for round in 0..FEISTEL_ROUNDS {
                self.roundBytes(network, round, &right.to_be_bytes()[8 - halfBytes..], &mut expanded);

                let mut word = [0u8; 8];
                word[8 - halfBytes..].copy_from_slice(&expanded[..halfBytes]);
                let next = left ^ (u64::from_be_bytes(word) & halfMask);
                left = right;
                right = next;
            }

            result = (u128::from(left) << half) | u128::from(right);

            if result <= last {
                return result;
            }
        }
    }

    /// `permute` in BigUints, for halves wider than 64 bits.
    fn permuteWide(&self, network: &mut Network, size: &BigUint, value: &BigUint) -> BigUint {
        let half = network.half;
        let halfBytes = network.halfBytes;
        let halfMask = (BigUint::from(1u8) << half) - 1u8;
        let mut message = vec![0u8; halfBytes];
        let mut expanded = vec![0u8; halfBytes.next_multiple_of(32)];
        let mut result = value.clone();

        loop {
            let mut left = &result >> half;
            let mut right = &result & &halfMask;

            for round in 0..FEISTEL_ROUNDS {
                writeLeftPadded(&right, &mut message);
                self.roundBytes(network, round, &message, &mut expanded);

                let roundValue = BigUint::from_bytes_be(&expanded[..halfBytes]);

                let next = left ^ (roundValue & &halfMask);
                left = right;
                right = next;
            }

            result = (left << half) | right;

            if &result < size {
                return result;
            }
        }
    }

    /// The round function: `halfBytes` bytes of `expand(message)` under the
    /// round's purpose, written to the front of `expanded`.
    fn roundBytes(&self, network: &mut Network, round: u8, message: &[u8], expanded: &mut [u8]) {
        network.prefix[network.roundIndex] = round;

        let mut counter: u32 = 0;
        let mut written = 0;
        while written < network.halfBytes {
            network.prefix[network.counterIndex..].copy_from_slice(&counter.to_be_bytes());
            expanded[written..written + 32].copy_from_slice(&self.digest(message, &network.prefix));
            written += 32;
            counter += 1;
        }
    }
}

/// What `permute` works out once per value: the halves' width, and the round
/// function's purpose string.
struct Network {
    half: usize,
    halfBytes: usize,
    prefix: Vec<u8>,
    roundIndex: usize,
    counterIndex: usize,
}

impl Network {
    fn new(size: &BigUint, purpose: &[u8]) -> Self {
        let mut bits = std::cmp::max(2, (size - 1u8).bits() as usize);
        bits += bits % 2;
        let half = bits / 2;

        // Python writes `size` with the fewest bytes that hold it, so a domain
        // of 2**128 takes 17 -- the boundary a fixed-width integer would miss.
        let sizeBytes = (size.bits() as usize).div_ceil(8);
        let mut prefix = Vec::with_capacity(purpose.len() + 1 + sizeBytes + 6);
        prefix.extend_from_slice(purpose);
        prefix.push(b'|');
        prefix.extend_from_slice(&leftPad(&size.to_bytes_be(), sizeBytes));

        // The round function's whole purpose string, laid out once:
        //
        //     purpose | '|' | size | round | '#' | counter
        //
        // Only the round byte and the counter change per digest, so each round
        // writes those two places rather than rebuilding the buffer. At
        // roughly forty digests a value, the allocations this saves cost more
        // than the hashing does.
        let roundIndex = prefix.len();
        prefix.push(0);
        prefix.push(b'#');
        prefix.extend_from_slice(&0u32.to_be_bytes());
        let counterIndex = prefix.len() - 4;

        Network { half, halfBytes: half.div_ceil(8), prefix, roundIndex, counterIndex }
    }
}

/// `BigUint::to_bytes_be` drops leading zeroes and returns `[0]` for zero;
/// Python's `int.to_bytes(width, 'big')` keeps the width. The Feistel round
/// function hashes this, so the padding is part of the construction.
/// `leftPad` into a buffer the caller keeps, for the Feistel round function,
/// which calls it once per round and would otherwise allocate every time.
fn writeLeftPadded(value: &BigUint, into: &mut [u8]) {
    let bytes = value.to_bytes_be();
    let trimmed: &[u8] = if bytes == [0u8] { &[] } else { &bytes };

    let split = into.len() - trimmed.len();
    into[..split].fill(0);
    into[split..].copy_from_slice(trimmed);
}

fn leftPad(bytes: &[u8], width: usize) -> Vec<u8> {
    let mut padded = vec![0u8; width];
    let trimmed: &[u8] = if bytes == [0u8] { &[] } else { bytes };

    if trimmed.len() <= width {
        padded[width - trimmed.len()..].copy_from_slice(trimmed);
    } else {
        // Python raises OverflowError here. Reaching it means a half outgrew
        // the width its own bit count implies, which is a porting mistake
        // rather than anything a value can cause.
        panic!("{} bytes do not fit in {}", trimmed.len(), width);
    }

    padded
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hmac_matches_rfc_4231() {
        // Vectors 1, 2 and 6: a short key, a shorter one, and one past the
        // block, which is hashed first. The backend is swappable, so this
        // pins it to the standard rather than to whatever it does today.
        let cases: [(&[u8], &[u8], &str); 3] = [
            (&[0x0b; 20], b"Hi There", "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"),
            (b"Jefe", b"what do ya want for nothing?", "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"),
            (&[0xaa; 131], b"Test Using Larger Than Block-Size Key - Hash Key First",
             "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54"),
        ];

        for (key, message, expected) in cases {
            let signed = hmac::sign(&hmac::Key::new(hmac::HMAC_SHA256, key), message);
            assert_eq!(hex::encode(signed.as_ref()), expected);
        }
    }

    #[test]
    fn left_pad_widens_without_shifting() {
        assert_eq!(leftPad(&[0x01], 4), vec![0, 0, 0, 1]);
        assert_eq!(leftPad(&BigUint::from(0u8).to_bytes_be(), 3), vec![0, 0, 0]);
        assert_eq!(leftPad(&[0xff, 0xff], 2), vec![0xff, 0xff]);
    }

    #[test]
    fn narrow_and_wide_permutations_agree() {
        // Every domain `permute` sends down the u64 path, run down the BigUint
        // one as well: small ones, each whole byte of half width, and the
        // edges at 2**128, whose size alone is past a u128.
        let hash = KeyedHash::new("a-test-key-that-is-long-enough", "paths");
        let two = BigUint::from(2u8);
        let mut sizes: Vec<BigUint> = (2u32..40).map(BigUint::from).collect();
        for bits in [15u32, 16, 17, 63, 64, 65, 126, 127, 128] {
            sizes.extend([two.pow(bits) - 1u8, two.pow(bits), two.pow(bits) + 1u8]);
        }
        sizes.retain(|size| Network::new(size, b"").half <= 64);

        for size in sizes {
            for value in [BigUint::from(0u8), &size / 3u8, &size - 1u8] {
                let narrow = hash.permuteNarrow(&mut Network::new(&size, b"t"), &size, &value);
                let wide = hash.permuteWide(&mut Network::new(&size, b"t"), &size, &value);
                assert_eq!(BigUint::from(narrow), wide, "permute({size}, {value})");
            }
        }
    }
}
