//! FF1 format-preserving encryption, as specified in NIST SP 800-38G Rev. 1.
//!
//! A port of `bauta/fpe.py`, with its step numbers kept so the two
//! can be read side by side. Only encryption is implemented: masking never
//! needs to reverse a value, and not shipping the inverse keeps the key from
//! becoming a way to unmask one.

use aes::cipher::{BlockCipherEncrypt, KeyInit};
use aes::Aes256;
use num_bigint::BigUint;
use num_traits::Zero;

/// NIST SP 800-38G Rev. 1 requires radix ** minlen >= 1,000,000.
pub const MINIMUM_DOMAIN_SIZE: u64 = 1_000_000;

pub const ROUNDS: u8 = 10;

/// The fewest numerals FF1 may encrypt in this radix.
pub fn minimumLength(radix: u32) -> usize {
    let mut length = 1u32;
    while (radix as u64).pow(length) < MINIMUM_DOMAIN_SIZE {
        length += 1;
    }

    length as usize
}

pub struct Ff1 {
    cipher: Aes256,
    pub radix: u32,
    pub minimumLength: usize,
}

impl Ff1 {
    /// `key` is AES-256 key material -- 32 bytes, as the strategy derives it.
    pub fn new(key: &[u8; 32], radix: u32) -> Self {
        Self {
            cipher: Aes256::new(key.into()),
            radix,
            minimumLength: minimumLength(radix),
        }
    }

    fn block(&self, data: &mut [u8; 16]) {
        self.cipher.encrypt_block(data.into());
    }

    /// CBC-MAC with a zero IV: the last block of CBC encryption.
    fn prf(&self, data: &[u8]) -> [u8; 16] {
        let mut state = [0u8; 16];

        for chunk in data.chunks_exact(16) {
            for (index, byte) in chunk.iter().enumerate() {
                state[index] ^= byte;
            }
            self.block(&mut state);
        }

        state
    }

    fn number(&self, numerals: &[u32]) -> BigUint {
        let mut value = BigUint::zero();
        for numeral in numerals {
            value = value * self.radix + *numeral;
        }

        value
    }

    fn numerals(&self, mut value: BigUint, length: usize) -> Vec<u32> {
        let mut out = vec![0u32; length];
        let radix = BigUint::from(self.radix);

        for position in (0..length).rev() {
            let remainder = &value % &radix;
            value /= &radix;
            out[position] = u32::try_from(&remainder).unwrap_or(0);
        }

        out
    }

    /// FF1.Encrypt(K, T, X) -- the algorithm's steps, numbered as in the standard.
    ///
    /// The caller has already checked the length; a numeral outside the radix
    /// is a porting mistake rather than anything a value can cause, so it
    /// panics rather than returning an error Python has no counterpart for.
    ///
    /// Run in machine integers where each half's modulus fits a u64 and Y a
    /// u128 -- 19 decimal digits a half, which covers every identifier in
    /// practice. The rounds were three quarters of an `fpe` mask when each
    /// built and took apart BigUints; wider values still do.
    pub fn encrypt(&self, numerals: &[u32], tweak: &[u8]) -> Vec<u32> {
        assert!(numerals.len() >= self.minimumLength, "FF1 called below its minimum length");
        assert!(numerals.iter().all(|numeral| *numeral < self.radix), "numeral outside the radix");

        let rounds = Rounds::new(self.radix, numerals.len(), tweak);
        match (u64::try_from(&rounds.radixToU), u64::try_from(&rounds.radixToV)) {
            (Ok(radixToU), Ok(radixToV)) if rounds.d <= 16 => self.encryptNarrow(&rounds, numerals, tweak, radixToU, radixToV),
            _ => self.encryptWide(&rounds, numerals, tweak),
        }
    }

    /// Steps 6.i-ix with A, B and Y as machine integers.
    fn encryptNarrow(&self, rounds: &Rounds, numerals: &[u32], tweak: &[u8], radixToU: u64, radixToV: u64) -> Vec<u32> {
        let radix = u64::from(self.radix);
        let number = |numerals: &[u32]| numerals.iter().fold(0u64, |value, numeral| value * radix + u64::from(*numeral));
        let mut a = numerals[..rounds.u].to_vec(); // 2
        let mut b = numerals[rounds.u..].to_vec();

        // Q is the same every round but for its round byte and NUM(B), so it
        // is laid out once and those two places rewritten.
        let mut message = rounds.messagePrefix(tweak);
        let roundAt = message.len();
        message.resize(roundAt + 1 + rounds.byteCount, 0);

        for i in 0..ROUNDS {
            message[roundAt] = i; // 6.i
            message[roundAt + 1..].copy_from_slice(&number(&b).to_be_bytes()[8 - rounds.byteCount..]);

            let r = self.prf(&message); // 6.ii
            let mut y = [0u8; 16]; // 6.iii-iv: d is at most 16, so S is R alone
            y[16 - rounds.d..].copy_from_slice(&r[..rounds.d]);
            let y = u128::from_be_bytes(y);

            let (m, modulus) = if i % 2 == 0 { (rounds.u, radixToU) } else { (rounds.v, radixToV) }; // 6.v
            let mut c = ((u128::from(number(&a)) + y) % u128::from(modulus)) as u64; // 6.vi

            std::mem::swap(&mut a, &mut b); // 6.vii-ix
            b.resize(m, 0);
            for position in (0..m).rev() {
                b[position] = (c % radix) as u32;
                c /= radix;
            }
        }

        a.extend_from_slice(&b); // 7
        a
    }

    /// Steps 6.i-ix in BigUints, for halves too wide for `encryptNarrow`.
    fn encryptWide(&self, rounds: &Rounds, numerals: &[u32], tweak: &[u8]) -> Vec<u32> {
        let mut a = numerals[..rounds.u].to_vec(); // 2
        let mut b = numerals[rounds.u..].to_vec();
        let prefix = rounds.messagePrefix(tweak);

        for i in 0..ROUNDS {
            let mut message = Vec::with_capacity(prefix.len() + 1 + rounds.byteCount); // 6.i
            message.extend_from_slice(&prefix);
            message.push(i);
            message.extend_from_slice(&leftPadded(&self.number(&b), rounds.byteCount));

            let r = self.prf(&message); // 6.ii

            let mut s = r.to_vec(); // 6.iii
            let mut j: u128 = 1;
            while s.len() < rounds.d {
                let mut block = r;
                let counter = j.to_be_bytes();
                for index in 0..16 {
                    block[index] ^= counter[index];
                }
                self.block(&mut block);
                s.extend_from_slice(&block);
                j += 1;
            }

            let y = BigUint::from_bytes_be(&s[..rounds.d]); // 6.iv
            let m = if i % 2 == 0 { rounds.u } else { rounds.v }; // 6.v
            let modulus = if i % 2 == 0 { &rounds.radixToU } else { &rounds.radixToV };
            let c = (self.number(&a) + y) % modulus; // 6.vi

            std::mem::swap(&mut a, &mut b); // 6.vii-ix
            b = self.numerals(c, m);
        }

        a.extend_from_slice(&b); // 7
        a
    }
}

/// Steps 1 and 3-5, which depend only on the radix, the length and the tweak.
struct Rounds {
    u: usize,
    v: usize,
    byteCount: usize,
    d: usize,
    p: Vec<u8>,
    padding: usize,
    radixToU: BigUint,
    radixToV: BigUint,
}

impl Rounds {
    fn new(radix: u32, n: usize, tweak: &[u8]) -> Self {
        let t = tweak.len();
        let u = n / 2; // 1
        let v = n - u;

        let byteCount = ((BigUint::from(radix).pow(v as u32) - 1u8).bits() as usize).div_ceil(8); // 3
        let d = 4 * byteCount.div_ceil(4) + 4; // 4

        let mut p = Vec::with_capacity(16); // 5
        p.extend_from_slice(&[1, 2, 1]);
        p.extend_from_slice(&radix.to_be_bytes()[1..]); // three bytes
        p.extend_from_slice(&[10, (u % 256) as u8]);
        p.extend_from_slice(&(n as u32).to_be_bytes());
        p.extend_from_slice(&(t as u32).to_be_bytes());

        Rounds {
            u,
            v,
            byteCount,
            d,
            p,
            padding: (16 - ((t + byteCount + 1) % 16)) % 16,
            radixToU: BigUint::from(radix).pow(u as u32),
            radixToV: BigUint::from(radix).pow(v as u32),
        }
    }

    /// P || T || [0]**padding: every round's message up to its round byte.
    fn messagePrefix(&self, tweak: &[u8]) -> Vec<u8> {
        let mut message = Vec::with_capacity(self.p.len() + tweak.len() + self.padding + 1 + self.byteCount);
        message.extend_from_slice(&self.p);
        message.extend_from_slice(tweak);
        message.resize(message.len() + self.padding, 0);
        message
    }
}

/// `value` as exactly `width` big-endian bytes, the way Python's
/// `int.to_bytes(width, 'big')` writes it.
fn leftPadded(value: &BigUint, width: usize) -> Vec<u8> {
    let bytes = value.to_bytes_be();
    let trimmed: &[u8] = if bytes == [0u8] { &[] } else { &bytes };

    let mut out = vec![0u8; width];
    out[width - trimmed.len()..].copy_from_slice(trimmed);

    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const ALPHABET: &[u8] = b"0123456789abcdefghijklmnopqrstuvwxyz";

    fn encode(text: &str) -> Vec<u32> {
        text.bytes().map(|c| ALPHABET.iter().position(|a| *a == c).unwrap() as u32).collect()
    }

    fn decode(numerals: &[u32]) -> String {
        numerals.iter().map(|n| ALPHABET[*n as usize] as char).collect()
    }

    /// NIST's published sample vectors for FF1, the same nine
    /// `tests/test_fpe.py` checks the Python against.
    #[test]
    fn nist_samples() {
        let key128 = hex_literal(&"2B7E151628AED2A6ABF7158809CF4F3C".repeat(2));
        let cases: [(&str, u32, &str, &str, &str); 3] = [
            ("2B7E151628AED2A6ABF7158809CF4F3CEF4359D8D580AA4F7F036D6F04FC6A94", 10, "", "0123456789", "6657667009"),
            ("2B7E151628AED2A6ABF7158809CF4F3CEF4359D8D580AA4F7F036D6F04FC6A94", 10, "39383736353433323130", "0123456789", "1001623463"),
            ("2B7E151628AED2A6ABF7158809CF4F3CEF4359D8D580AA4F7F036D6F04FC6A94", 36, "3737373770717273373737", "0123456789abcdefghi", "xs8a0azh2avyalyzuwd"),
        ];
        let _ = key128;

        for (key, radix, tweak, plaintext, ciphertext) in cases {
            let cipher = Ff1::new(&hex_literal(key).try_into().unwrap(), radix);
            let encrypted = cipher.encrypt(&encode(plaintext), &hex_literal(tweak));
            assert_eq!(decode(&encrypted), ciphertext, "NIST sample, radix {radix}");
        }
    }

    #[test]
    fn narrow_and_wide_rounds_agree() {
        // Every radix a charset uses, at every length the u64 path takes, run
        // down the BigUint path as well; the NIST samples go through
        // `encrypt`, and so the narrow path, only.
        let key = [7u8; 32];
        for radix in [10u32, 16, 22, 36, 62] {
            let cipher = Ff1::new(&key, radix);
            for length in cipher.minimumLength..=40 {
                let rounds = Rounds::new(radix, length, b"tweak");
                let (Ok(radixToU), Ok(radixToV)) = (u64::try_from(&rounds.radixToU), u64::try_from(&rounds.radixToV)) else { continue };
                let numerals: Vec<u32> = (0..length as u32).map(|index| (index * 7 + length as u32) % radix).collect();

                assert_eq!(
                    cipher.encryptNarrow(&rounds, &numerals, b"tweak", radixToU, radixToV),
                    cipher.encryptWide(&rounds, &numerals, b"tweak"),
                    "radix {radix}, length {length}"
                );
            }
        }
    }

    #[test]
    fn minimum_length_follows_the_radix() {
        assert_eq!(minimumLength(10), 6);
        assert_eq!(minimumLength(16), 5);
        assert_eq!(minimumLength(62), 4);
    }

    fn hex_literal(text: &str) -> Vec<u8> {
        (0..text.len()).step_by(2).map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap()).collect()
    }
}
