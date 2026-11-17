//! `redact`: the identifiers found in free text, each replaced by a label or a
//! keyed value of its shape. `documents.RedactStrategy`.
//!
//! Python finds them with six regular expressions, four of which look one
//! character behind or ahead of a match. Rust's `regex` crate has no
//! lookaround, and a backtracking engine that does (`fancy-regex`) was slower
//! than Python's own. So each lookaround is written out instead, in a way that
//! finds exactly the spans Python's backtracking finds:
//!
//! - A lookbehind, "not preceded by one of these", is checked by hand against
//!   the character before a match. Where it fails, the search starts again one
//!   character later, as Python's matcher moves on to the next start.
//! - A lookahead, "not followed by one of these", becomes one more character
//!   the pattern consumes -- any other character, or the end of the text -- and
//!   is trimmed off again. Leftmost-first matching tries a pattern's ends in
//!   the order a backtracking matcher does, so it settles on the end Python
//!   settles on. Each such pattern ends in a digit and no consumed tail holds
//!   one, so the tail is whatever trails the match's last digit.
//!
//! Only ASCII text is handled. Python's `\d`, `\w` and `\b` are Unicode's,
//! whose classes Rust's do not match exactly; anything else goes back to
//! Python, as it does for every strategy here. With ASCII guaranteed, each
//! class is spelled in ASCII, which also keeps the matcher on its fastest path.
//!
//! Python's `patterns` option, regular expressions of the policy's own, stays
//! in Python, which builds no native masker for a policy that sets it.

use regex::Regex;

use crate::cheap::{maskDigitsText, maskEmail};
use crate::error::{MaskError, Masked};
use crate::key::{Charset, KeyStrategy};
use crate::KeyedHash;

/// The kinds Python detects, in its order: where matches overlap, the earlier
/// kind wins. `documents._DETECTOR_KINDS`.
pub const KINDS: [&str; 6] = ["email", "iban", "card", "ssn", "ip", "phone"];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    Email,
    Iban,
    Card,
    Ssn,
    Ip,
    Phone,
}

impl Kind {
    fn parse(name: &str) -> Option<Self> {
        Some(match name {
            "email" => Kind::Email,
            "iban" => Kind::Iban,
            "card" => Kind::Card,
            "ssn" => Kind::Ssn,
            "ip" => Kind::Ip,
            "phone" => Kind::Phone,
            _ => return None,
        })
    }

    fn label(&self) -> &'static str {
        match self {
            Kind::Email => "[EMAIL]",
            Kind::Iban => "[IBAN]",
            Kind::Card => "[CARD]",
            Kind::Ssn => "[SSN]",
            Kind::Ip => "[IP]",
            Kind::Phone => "[PHONE]",
        }
    }
}

/// Whether a character may not precede a match: a one-character lookbehind.
type Precedes = fn(u8) -> bool;

/// One of Python's detectors, its lookarounds written out.
struct Detector {
    kind: Kind,
    /// The pattern, with any lookahead as one consumed character.
    pattern: Regex,
    /// What may not precede a match: a lookbehind, or none.
    notAfter: Option<Precedes>,
    /// Whether the pattern ends in a consumed lookahead to trim.
    trims: bool,
}

fn isDigitOrDash(byte: u8) -> bool {
    byte.is_ascii_digit() || byte == b'-'
}

fn isDigitOrDot(byte: u8) -> bool {
    byte.is_ascii_digit() || byte == b'.'
}

/// Python's `[\w+]` over ASCII.
fn isWordOrPlus(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'+'
}

impl Detector {
    /// `documents._DETECTORS`, pattern by pattern, each Python spelling above
    /// the one here.
    fn of(kind: Kind) -> Self {
        let (pattern, notAfter, trims): (&str, Option<Precedes>, bool) = match kind {
            // [A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}
            Kind::Email => (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}", None, false),
            // \b[A-Z]{2}\d{2}(?: ?[A-Z\d]){11,30}\b
            Kind::Iban => (r"(?-u:\b)[A-Z]{2}[0-9]{2}(?: ?[A-Z0-9]){11,30}(?-u:\b)", None, false),
            // (?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])
            Kind::Card => (r"[0-9](?:[ -]?[0-9]){12,18}(?:[^0-9-]|\z)", Some(isDigitOrDash), true),
            // (?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])
            Kind::Ssn => (r"[0-9]{3}-[0-9]{2}-[0-9]{4}(?:[^0-9-]|\z)", Some(isDigitOrDash), true),
            // (?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\.?\d): not followed by a
            // digit, nor by a dot and a digit.
            Kind::Ip => (r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?:[^.0-9]|\.[^0-9]|\.\z|\z)", Some(isDigitOrDot), true),
            // (?<![\w+])\+?\(?\d[\d ().-]{5,}\d(?!\w)
            Kind::Phone => (r"\+?\(?[0-9][0-9 ().-]{5,}[0-9](?:[^A-Za-z0-9_]|\z)", Some(isWordOrPlus), true),
        };

        Detector { kind, pattern: Regex::new(pattern).expect("a valid pattern"), notAfter, trims }
    }
}

/// Python's checks on what a pattern found, past its shape.
fn passes(kind: Kind, found: &[u8], dateLike: &Regex) -> bool {
    match kind {
        Kind::Email | Kind::Ssn => true,
        Kind::Iban => validIban(found),
        Kind::Card => luhnValid(found),
        Kind::Ip => validIpv4(found),
        Kind::Phone => phoneLike(found, dateLike),
    }
}

/// `common.luhnValid` over the digits of `found`.
fn luhnValid(found: &[u8]) -> bool {
    let mut total = 0u32;
    for (position, byte) in found.iter().filter(|byte| byte.is_ascii_digit()).rev().enumerate() {
        let mut digit = u32::from(byte - b'0');
        if position % 2 == 1 {
            digit = if digit > 4 { digit * 2 - 9 } else { digit * 2 };
        }
        total += digit;
    }

    total % 10 == 0
}

/// `documents._validIban`: 15 to 34 characters, and the ISO 13616 check --
/// the country and check digits moved to the end, each letter as two digits,
/// the whole a number whose remainder by 97 is 1, taken a digit at a time.
fn validIban(found: &[u8]) -> bool {
    let compact: Vec<u8> = found.iter().copied().filter(|byte| *byte != b' ').collect();
    if !(15..=34).contains(&compact.len()) {
        return false;
    }

    let mut remainder = 0u32;
    for byte in compact[4..].iter().chain(&compact[..4]) {
        remainder = if byte.is_ascii_digit() {
            (remainder * 10 + u32::from(byte - b'0')) % 97
        } else {
            (remainder * 100 + u32::from(byte - b'A') + 10) % 97
        };
    }

    remainder == 1
}

/// `documents._validIpv4`: every octet at most 255.
fn validIpv4(found: &[u8]) -> bool {
    found.split(|byte| *byte == b'.').all(|octet| octet.iter().fold(0u32, |total, digit| total * 10 + u32::from(digit - b'0')) <= 255)
}

/// `documents._phoneLike`: 7 to 15 digits, and not a date. What the phone
/// pattern finds holds no whitespace but spaces, so Python's `strip()` is
/// trimming spaces.
fn phoneLike(found: &[u8], dateLike: &Regex) -> bool {
    let digits = found.iter().filter(|byte| byte.is_ascii_digit()).count();
    let text = std::str::from_utf8(found).expect("ASCII").trim_matches(' ');

    (7..=15).contains(&digits) && !dateLike.is_match(text)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Replacement {
    Label,
    Mask,
}

impl Replacement {
    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "label" => Some(Replacement::Label),
            "mask" => Some(Replacement::Mask),
            _ => None,
        }
    }
}

pub struct RedactStrategy {
    detectors: Vec<Detector>,
    replacement: Replacement,
    /// `documents._DATE_LIKE`, which the phone check refuses.
    dateLike: Regex,
    key: KeyStrategy,
}

impl RedactStrategy {
    /// `detect` names the kinds to find, every one where it's None; they are
    /// found in Python's order whatever order it names them in.
    pub fn new(detect: Option<&[String]>, replacement: Replacement) -> Result<Self, String> {
        if let Some(names) = detect {
            if let Some(unknown) = names.iter().find(|name| Kind::parse(name).is_none()) {
                return Err(format!("redact detects no kind {unknown:?}"));
            }
        }
        let wanted = |kind: &str| detect.is_none_or(|names| names.iter().any(|name| name == kind));
        let detectors = KINDS.iter().filter(|kind| wanted(kind)).map(|kind| Detector::of(Kind::parse(kind).expect("a kind"))).collect();

        Ok(Self {
            detectors,
            replacement,
            dateLike: Regex::new(r"^(?:[0-9]{4}[-/.][0-9]{1,2}[-/.][0-9]{1,2}|[0-9]{1,2}[-/.][0-9]{1,2}[-/.][0-9]{2,4})$").expect("a valid pattern"),
            key: KeyStrategy::new(Charset::Alphanumeric),
        })
    }

    /// The identifiers in `text`, as (start, end, kind), in order and never
    /// overlapping: `RedactStrategy._spans`.
    fn spans(&self, text: &str) -> Vec<(usize, usize, Kind)> {
        let bytes = text.as_bytes();
        // Every pattern but email's needs a digit, and email's an @: a text
        // without either is searched for neither. Most free text has no @.
        let hasDigit = bytes.iter().any(u8::is_ascii_digit);
        let hasAt = bytes.contains(&b'@');
        let mut occupied = vec![false; bytes.len()];
        let mut taken = Vec::new();

        for detector in &self.detectors {
            if !(if detector.kind == Kind::Email { hasAt } else { hasDigit }) {
                continue;
            }

            let mut position = 0;
            while let Some(found) = detector.pattern.find_at(text, position) {
                let start = found.start();
                if start > 0 && detector.notAfter.is_some_and(|precedes| precedes(bytes[start - 1])) {
                    position = start + 1;
                    continue;
                }

                let mut end = found.end();
                if detector.trims {
                    while !bytes[end - 1].is_ascii_digit() {
                        end -= 1;
                    }
                }
                // Python's finditer goes on from the end of a match, whether or
                // not the span is taken.
                position = end;

                if occupied[start..end].contains(&true) || !passes(detector.kind, &bytes[start..end], &self.dateLike) {
                    continue;
                }
                occupied[start..end].fill(true);
                taken.push((start, end, detector.kind));
            }
        }

        taken.sort_unstable_by_key(|(start, _, _)| *start);
        taken
    }

    /// `RedactStrategy._replace`, for `replacement: mask`.
    fn masked(&self, hash: &KeyedHash, kind: Kind, found: &str) -> Masked<String> {
        match kind {
            Kind::Email => maskEmail(hash, found, 12, "example.test", false),
            Kind::Card => maskDigitsText(hash, found, 0, 4),
            Kind::Phone | Kind::Ssn => maskDigitsText(hash, found, 0, 0),
            Kind::Iban => Ok(format!("{}{}", &found[..2], self.key.maskText(hash, &found[2..])?)),
            Kind::Ip => {
                let octets = hash.digest(found.as_bytes(), b"ip");
                Ok(format!("10.{}.{}.{}", octets[0], octets[1], octets[2]))
            }
        }
    }

    /// `text` with every identifier found in it replaced. Anything a strategy
    /// it replaces with refuses goes back to Python whole, to raise as Python
    /// raises it.
    pub fn mask(&self, hash: &KeyedHash, text: &str) -> Masked<String> {
        if !text.is_ascii() {
            return Err(MaskError::Unsupported);
        }

        let spans = self.spans(text);
        if spans.is_empty() {
            return Ok(text.to_owned());
        }

        let mut out = String::with_capacity(text.len() + 8 * spans.len());
        let mut position = 0;
        for (start, end, kind) in spans {
            out.push_str(&text[position..start]);
            match self.replacement {
                Replacement::Label => out.push_str(kind.label()),
                Replacement::Mask => out.push_str(&self.masked(hash, kind, &text[start..end]).map_err(|_| MaskError::Unsupported)?),
            }
            position = end;
        }
        out.push_str(&text[position..]);

        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn kinds(strategy: &RedactStrategy, text: &str) -> Vec<(String, Kind)> {
        strategy.spans(text).into_iter().map(|(start, end, kind)| (text[start..end].to_owned(), kind)).collect()
    }

    #[test]
    fn a_failed_lookahead_backtracks_to_a_shorter_match_as_python_does() {
        let strategy = RedactStrategy::new(Some(&["phone".to_owned()]), Replacement::Label).unwrap();

        // The whole number is followed by a letter, so Python backs off to the
        // longest prefix ending in a digit not followed by one, whose seven
        // digits then pass as a phone number.
        assert_eq!(kinds(&strategy, "555 1234-567ab"), vec![("555 1234".to_owned(), Kind::Phone)]);
    }

    #[test]
    fn a_failed_lookbehind_moves_on_one_character() {
        let strategy = RedactStrategy::new(Some(&["ssn".to_owned()]), Replacement::Label).unwrap();

        assert!(kinds(&strategy, "1123-45-6789").is_empty());
        assert_eq!(kinds(&strategy, "a123-45-6789"), vec![("123-45-6789".to_owned(), Kind::Ssn)]);
    }

    #[test]
    fn an_ip_is_not_followed_by_a_dot_and_a_digit() {
        let strategy = RedactStrategy::new(Some(&["ip".to_owned()]), Replacement::Label).unwrap();

        assert!(kinds(&strategy, "1.2.3.4.5").is_empty());
        assert_eq!(kinds(&strategy, "at 1.2.3.4. Then"), vec![("1.2.3.4".to_owned(), Kind::Ip)]);
        assert_eq!(kinds(&strategy, "1.2.3.4."), vec![("1.2.3.4".to_owned(), Kind::Ip)]);
        assert!(kinds(&strategy, "1.2.3.400").is_empty(), "an octet past 255");
    }

    #[test]
    fn an_earlier_kind_wins_an_overlap() {
        let strategy = RedactStrategy::new(None, Replacement::Label).unwrap();

        assert_eq!(kinds(&strategy, "4111 1111 1111 1111"), vec![("4111 1111 1111 1111".to_owned(), Kind::Card)]);
    }

    #[test]
    fn text_outside_ascii_goes_back_to_python() {
        let strategy = RedactStrategy::new(None, Replacement::Label).unwrap();
        let hash = KeyedHash::new("a-test-key-that-is-long-enough", "redact");

        assert_eq!(strategy.mask(&hash, "olá 123-45-6789"), Err(MaskError::Unsupported));
        assert_eq!(strategy.mask(&hash, "ssn 123-45-6789.").unwrap(), "ssn [SSN].");
    }

    #[test]
    fn a_long_run_of_digits_takes_linear_time() {
        let strategy = RedactStrategy::new(None, Replacement::Mask).unwrap();
        let hash = KeyedHash::new("a-test-key-that-is-long-enough", "redact");
        let text = "7".repeat(200_000) + "@x";

        let started = std::time::Instant::now();
        strategy.mask(&hash, &text).unwrap();
        assert!(started.elapsed().as_secs() < 2, "took {:?}", started.elapsed());
    }
}
