//! The `number` strategy: Python's `decimal` arithmetic, reproduced for the
//! operations `NumberStrategy.mask` uses, so a masked number is the same
//! digits whichever implementation computed it.
//!
//! Python masks inside a context of 60 significant digits, rounding half to
//! even, and every intermediate result is rounded there. The decimals here
//! do the same: exact arithmetic on a coefficient and an exponent, then that
//! one rounding. A result's representation -- `12.30` rather than `12.3` --
//! matters only where Python returns it, and there it follows the same
//! exponent rules.
//!
//! What this doesn't reproduce it hands back as `Unsupported`, and Python
//! masks that value: zeros, whose sign Python tracks through every step;
//! non-finite values; values too long or too far from 1 for the fast path;
//! decimals `canonical` would round; and a `quantize` Python would refuse.

use std::borrow::Cow;
use std::cmp::Ordering;
use std::sync::OnceLock;

use num_bigint::{BigInt, BigUint, Sign};
use num_integer::Integer;
use num_traits::{FromPrimitive, Zero};

use crate::digits::{decimalDigits, pow10};
use crate::{KeyedHash, MaskError};

/// Python's `_PRECISION`, the context `NumberStrategy._mask` computes in.
const PRECISION: usize = 60;

/// The default context's precision, which `canonical` runs in: a decimal
/// with more significant digits than this is rounded by `normalize()`.
const CANONICAL_PRECISION: usize = 28;

/// The longest coefficient, and the furthest exponent, a value may have to be
/// masked here; anything beyond goes to Python.
const MAXIMUM_DIGITS: usize = 38;
const MAXIMUM_EXPONENT: i64 = 400;

/// `KeyedHash::unit` has 53 bits of resolution: it is a whole number over
/// 2**53, which is also exactly what `Decimal(float)` makes of it.
const UNIT_BITS: i32 = 53;

/// A finite decimal: `(-1)**negative * coefficient * 10**exponent`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Dec {
    pub negative: bool,
    pub coefficient: BigUint,
    pub exponent: i64,
}

#[derive(Clone, Copy)]
enum Rounding {
    HalfEven,
    Ceiling,
    Floor,
}

/// `coefficient * 10**shift`, borrowed where the shift is zero.
fn scaled(coefficient: &BigUint, shift: i64) -> Cow<'_, BigUint> {
    if shift == 0 { Cow::Borrowed(coefficient) } else { Cow::Owned(coefficient * pow10(shift as u64).as_ref()) }
}

/// Whether dropping `remainder` (out of `divisor`) from quotient `quotient`
/// rounds the magnitude up.
fn roundsUp(quotient: &BigUint, remainder: &BigUint, divisor: &BigUint, rounding: Rounding, negative: bool) -> bool {
    if remainder.is_zero() {
        return false;
    }
    match rounding {
        Rounding::HalfEven => match (remainder << 1u8).cmp(divisor) {
            Ordering::Greater => true,
            Ordering::Less => false,
            Ordering::Equal => quotient.bit(0),
        },
        Rounding::Ceiling => !negative,
        Rounding::Floor => negative,
    }
}

impl Dec {
    fn new(negative: bool, coefficient: BigUint, exponent: i64) -> Self {
        Dec { negative, coefficient, exponent }
    }

    fn whole(value: u32) -> Self {
        Dec::new(false, BigUint::from(value), 0)
    }

    pub fn isZero(&self) -> bool {
        self.coefficient.is_zero()
    }

    fn fromInt(value: &BigInt) -> Self {
        Dec::new(value.sign() == Sign::Minus, value.magnitude().clone(), 0)
    }

    /// `Decimal(unit)`: exact, as Python's constructor is whatever the context.
    fn fromUnit(unit: f64) -> Self {
        static FIVES: OnceLock<BigUint> = OnceLock::new();
        let fives = FIVES.get_or_init(|| BigUint::from(5u8).pow(UNIT_BITS as u32));
        let numerator = (unit * f64::from(2u32).powi(UNIT_BITS)) as u64;
        Dec::new(false, fives * numerator, -(UNIT_BITS as i64))
    }

    /// A decimal as Python writes one -- `str(Decimal)` or `repr(float)`:
    /// `-12.340`, `1.2E+5`, `1e-07`. None for anything else, NaN and the
    /// infinities included.
    pub fn parse(text: &str) -> Option<Self> {
        let (negative, rest) = match text.as_bytes().first()? {
            b'-' => (true, &text[1..]),
            b'+' => (false, &text[1..]),
            _ => (false, text),
        };
        let (mantissa, exponent) = match rest.find(['e', 'E']) {
            Some(at) => (&rest[..at], rest[at + 1..].parse::<i64>().ok()?),
            None => (rest, 0),
        };
        let (whole, fraction) = match mantissa.find('.') {
            Some(at) => (&mantissa[..at], &mantissa[at + 1..]),
            None => (mantissa, ""),
        };
        if whole.is_empty() && fraction.is_empty() {
            return None;
        }
        if !whole.bytes().chain(fraction.bytes()).all(|byte| byte.is_ascii_digit()) {
            return None;
        }
        let digits = format!("{whole}{fraction}");
        let coefficient = BigUint::parse_bytes(digits.as_bytes(), 10)?;

        Some(Dec::new(negative, coefficient, exponent - fraction.len() as i64))
    }

    /// Rounded to `precision` significant digits, half to even, as every
    /// result in Python's context is.
    fn rounded(self, precision: usize) -> Self {
        let count = decimalDigits(&self.coefficient);
        if count <= precision {
            return self;
        }
        let drop = (count - precision) as u64;
        let divisor = pow10(drop);
        let (mut quotient, remainder) = self.coefficient.div_rem(divisor.as_ref());
        if roundsUp(&quotient, &remainder, divisor.as_ref(), Rounding::HalfEven, self.negative) {
            quotient += 1u8;
        }
        let mut exponent = self.exponent + drop as i64;
        // Rounding 99...9 up gives 10**precision, one digit too many.
        if decimalDigits(&quotient) > precision {
            quotient /= 10u8;
            exponent += 1;
        }
        Dec::new(self.negative, quotient, exponent)
    }

    fn negated(&self) -> Self {
        Dec::new(!self.negative, self.coefficient.clone(), self.exponent)
    }

    fn add(&self, other: &Dec) -> Dec {
        let exponent = self.exponent.min(other.exponent);
        let left = scaled(&self.coefficient, self.exponent - exponent);
        let right = scaled(&other.coefficient, other.exponent - exponent);

        let (negative, coefficient) = if self.negative == other.negative {
            (self.negative, left.as_ref() + right.as_ref())
        } else if left >= right {
            (self.negative, left.as_ref() - right.as_ref())
        } else {
            (other.negative, right.as_ref() - left.as_ref())
        };
        // An exact zero is negative only when both operands were.
        let negative = if coefficient.is_zero() { self.negative && other.negative } else { negative };

        Dec::new(negative, coefficient, exponent).rounded(PRECISION)
    }

    fn subtract(&self, other: &Dec) -> Dec {
        self.add(&other.negated())
    }

    fn multiply(&self, other: &Dec) -> Dec {
        Dec::new(self.negative != other.negative, &self.coefficient * &other.coefficient, self.exponent + other.exponent).rounded(PRECISION)
    }

    /// `quantize` to `10**exponent`. Unsupported where Python raises
    /// InvalidOperation: a result longer than the precision.
    fn quantize(&self, exponent: i64, rounding: Rounding) -> Result<Dec, MaskError> {
        let coefficient = if self.exponent >= exponent {
            &self.coefficient * pow10((self.exponent - exponent) as u64).as_ref()
        } else {
            let divisor = pow10((exponent - self.exponent) as u64);
            let (mut quotient, remainder) = self.coefficient.div_rem(divisor.as_ref());
            if roundsUp(&quotient, &remainder, divisor.as_ref(), rounding, self.negative) {
                quotient += 1u8;
            }
            quotient
        };
        if decimalDigits(&coefficient) > PRECISION {
            return Err(MaskError::Unsupported);
        }

        Ok(Dec::new(self.negative, coefficient, exponent))
    }

    /// Numeric comparison: `-0 == 0`, and `1.20 == 1.2`.
    fn compare(&self, other: &Dec) -> Ordering {
        match (self.isZero(), other.isZero()) {
            (true, true) => return Ordering::Equal,
            (true, false) => return if other.negative { Ordering::Greater } else { Ordering::Less },
            (false, true) => return if self.negative { Ordering::Less } else { Ordering::Greater },
            _ => {}
        }
        if self.negative != other.negative {
            return if self.negative { Ordering::Less } else { Ordering::Greater };
        }
        let exponent = self.exponent.min(other.exponent);
        let left = scaled(&self.coefficient, self.exponent - exponent);
        let right = scaled(&other.coefficient, other.exponent - exponent);
        let magnitude = left.cmp(&right);

        if self.negative { magnitude.reverse() } else { magnitude }
    }

    /// `int(self)` for a value with no fractional part.
    fn toBigInt(&self) -> BigInt {
        let magnitude = if self.exponent >= 0 {
            &self.coefficient * pow10(self.exponent as u64).as_ref()
        } else {
            &self.coefficient / pow10((-self.exponent) as u64).as_ref()
        };
        BigInt::from_biguint(if self.negative { Sign::Minus } else { Sign::Plus }, magnitude)
    }

    /// `float(self)`: correctly rounded, as Python's is.
    fn toF64(&self) -> f64 {
        format!("{}{}e{}", if self.negative { "-" } else { "" }, self.coefficient, self.exponent).parse().unwrap_or(f64::NAN)
    }

    /// A spelling `decimal.Decimal` reads back as exactly this coefficient
    /// and exponent, so the digits Python would have returned are the ones
    /// returned.
    pub fn toText(&self) -> String {
        format!("{}{}E{}", if self.negative { "-" } else { "" }, self.coefficient, self.exponent)
    }

    fn integral(&self) -> bool {
        self.exponent >= 0 || (&self.coefficient % pow10((-self.exponent) as u64).as_ref()).is_zero()
    }
}

/// A value `number` masks, as it crossed from Python: a float keeps its
/// `repr` and a decimal its `str`, which are what Python computes from.
pub enum NumberInput<'a> {
    Int(&'a BigInt),
    Float { value: f64, repr: &'a str },
    Decimal(&'a str),
}

#[derive(Clone, Debug, PartialEq)]
pub enum NumberOutput {
    Int(BigInt),
    Float(f64),
    /// Spelled for `decimal.Decimal` to read; see `Dec::toText`.
    Decimal(String),
}

/// `masking.canonical`, for the three types `number` takes; None where only
/// Python's `normalize()` could say.
fn canonical(input: &NumberInput) -> Option<String> {
    match input {
        NumberInput::Int(value) => Some(value.to_string()),
        NumberInput::Float { value, repr } => {
            if value.fract() == 0.0 {
                // str(int(value)): every digit of the whole number it is.
                Some(BigInt::from_f64(*value)?.to_string())
            } else {
                Some((*repr).to_owned())
            }
        }
        NumberInput::Decimal(text) => canonicalDecimal(&Dec::parse(text)?),
    }
}

/// `canonical` of a Decimal, already parsed.
fn canonicalDecimal(value: &Dec) -> Option<String> {
    if value.integral() {
        return Some(value.toBigInt().to_string());
    }
    // format(value.normalize(), 'f'): trailing zeros dropped, then written
    // out with no exponent. normalize() rounds past the default context's 28
    // digits; those stay with Python.
    let written = value.coefficient.to_string();
    let digits = written.trim_end_matches('0');
    let exponent = value.exponent + (written.len() - digits.len()) as i64;
    if digits.len() > CANONICAL_PRECISION {
        return None;
    }
    let places = (-exponent) as usize;
    let written = if digits.len() > places {
        format!("{}.{}", &digits[..digits.len() - places], &digits[digits.len() - places..])
    } else {
        format!("0.{}{}", "0".repeat(places - digits.len()), digits)
    };
    Some(if value.negative { format!("-{written}") } else { written })
}

/// `NumberStrategy`, with its options already validated by Python.
pub struct NumberStrategy {
    range: Option<(Dec, Dec)>,
    variance: Dec,
    decimals: Option<u32>,
}

impl NumberStrategy {
    /// `min`, `max` and `variance` as `str(Decimal)` spells them.
    pub fn new(minimum: Option<&str>, maximum: Option<&str>, variance: Option<&str>, decimals: Option<u32>) -> Result<Self, String> {
        let parse = |name: &str, text: &str| Dec::parse(text).ok_or_else(|| format!("number {name} is not a finite decimal: {text:?}"));

        let range = match (minimum, maximum) {
            (Some(minimum), Some(maximum)) => Some((parse("min", minimum)?, parse("max", maximum)?)),
            (None, None) => None,
            _ => return Err("number needs both min and max, or neither".to_owned()),
        };
        let variance = match variance {
            Some(text) => parse("variance", text)?,
            None => Dec::new(false, BigUint::from(1u8), -1),
        };

        Ok(NumberStrategy { range, variance, decimals })
    }

    /// `_target`: the masked value before it is rounded to the column's precision.
    fn target(&self, value: &Dec, hash: &KeyedHash, message: &[u8]) -> Dec {
        let fraction = Dec::fromUnit(hash.unit(message, b""));

        match &self.range {
            Some((minimum, maximum)) => minimum.add(&maximum.subtract(minimum).multiply(&fraction)),
            None => {
                let swing = Dec::whole(2).multiply(&fraction).subtract(&Dec::whole(1)).multiply(&self.variance);
                value.multiply(&Dec::whole(1).add(&swing))
            }
        }
    }

    /// `_clamp`: rounding can step just outside a range whose bounds aren't on the grid.
    fn clamp(&self, number: Dec, exponent: i64) -> Result<Dec, MaskError> {
        let Some((minimum, maximum)) = &self.range else { return Ok(number) };

        let low = minimum.quantize(exponent, Rounding::Ceiling)?;
        let high = maximum.quantize(exponent, Rounding::Floor)?;
        // min(max(number, low), high), whose ties keep the first argument.
        let raised = if low.compare(&number) == Ordering::Greater { low } else { number };

        Ok(if high.compare(&raised) == Ordering::Less { high } else { raised })
    }

    /// `_moved`: one step from `value` where the variance rounded back to it.
    fn moved(&self, value: &Dec, masked: Dec, step: &Dec, hash: &KeyedHash, message: &[u8]) -> Dec {
        if self.range.is_some() || masked.compare(value) != Ordering::Equal || value.isZero() {
            return masked;
        }
        if hash.unit(message, b"step") >= 0.5 { value.add(step) } else { value.subtract(step) }
    }

    fn fits(value: &Dec) -> bool {
        decimalDigits(&value.coefficient) <= MAXIMUM_DIGITS && value.exponent.abs() <= MAXIMUM_EXPONENT
    }

    pub fn mask(&self, hash: &KeyedHash, input: &NumberInput) -> Result<NumberOutput, MaskError> {
        // A Decimal is parsed once, for the bytes it is keyed on and the
        // arithmetic alike.
        let decimal = match input {
            NumberInput::Decimal(text) => Some(Dec::parse(text).ok_or(MaskError::Unsupported)?),
            _ => None,
        };
        let message = match &decimal {
            Some(value) => canonicalDecimal(value),
            None => canonical(input),
        };
        let message = message.ok_or(MaskError::Unsupported)?;
        let message = message.as_bytes();

        match input {
            NumberInput::Int(number) => {
                let value = Dec::fromInt(number);
                if !Self::fits(&value) {
                    return Err(MaskError::Unsupported);
                }
                let step = Dec::whole(1);
                let masked = self.clamp(self.target(&value, hash, message).quantize(0, Rounding::HalfEven)?, 0)?;

                Ok(NumberOutput::Int(self.moved(&value, masked, &step, hash, message).toBigInt()))
            }

            NumberInput::Float { value, repr } => {
                if !value.is_finite() || *value == 0.0 {
                    return Err(MaskError::Unsupported);
                }
                let decimal = Dec::parse(repr).ok_or(MaskError::Unsupported)?;
                if !Self::fits(&decimal) {
                    return Err(MaskError::Unsupported);
                }
                let target = self.target(&decimal, hash, message);
                let masked = match self.decimals {
                    Some(decimals) => {
                        let exponent = -i64::from(decimals);
                        let step = Dec::new(false, BigUint::from(1u8), exponent);
                        let masked = self.clamp(target.quantize(exponent, Rounding::HalfEven)?, exponent)?;
                        self.moved(&decimal, masked, &step, hash, message).toF64()
                    }
                    None => target.toF64(),
                };
                // A zero keeps the sign Python tracked; a non-finite result is Python's to spell.
                if masked == 0.0 || !masked.is_finite() {
                    return Err(MaskError::Unsupported);
                }

                Ok(NumberOutput::Float(masked))
            }

            NumberInput::Decimal(_) => {
                let value = decimal.expect("parsed above");
                if value.isZero() || !Self::fits(&value) {
                    return Err(MaskError::Unsupported);
                }
                let exponent = match self.decimals {
                    Some(decimals) => -i64::from(decimals),
                    None => value.exponent.min(0),
                };
                let step = Dec::new(false, BigUint::from(1u8), exponent);
                let masked = self.clamp(self.target(&value, hash, message).quantize(exponent, Rounding::HalfEven)?, exponent)?;
                let moved = self.moved(&value, masked, &step, hash, message);
                if moved.isZero() {
                    return Err(MaskError::Unsupported);
                }

                Ok(NumberOutput::Decimal(moved.toText()))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dec(text: &str) -> Dec {
        Dec::parse(text).unwrap()
    }

    #[test]
    fn parses_what_python_writes() {
        assert_eq!(dec("-12.340"), Dec::new(true, BigUint::from(12340u32), -3));
        assert_eq!(dec("1.2E+5"), Dec::new(false, BigUint::from(12u32), 4));
        assert_eq!(dec("1e-07"), Dec::new(false, BigUint::from(1u32), -7));
        assert_eq!(dec("0.0"), Dec::new(false, BigUint::from(0u32), -1));
        assert!(Dec::parse("NaN").is_none() && Dec::parse("inf").is_none() && Dec::parse("-Infinity").is_none());
    }

    #[test]
    fn rounds_half_even_at_the_precision() {
        let digits = |count: usize, last: &str| dec(&format!("{}{}", "1".repeat(count), last));
        // 61 digits ending in 5, even before it: stays; odd before it: up.
        assert_eq!(digits(59, "25").rounded(PRECISION), Dec::new(false, BigUint::parse_bytes(format!("{}2", "1".repeat(59)).as_bytes(), 10).unwrap(), 1));
        assert_eq!(digits(59, "35").rounded(PRECISION), Dec::new(false, BigUint::parse_bytes(format!("{}4", "1".repeat(59)).as_bytes(), 10).unwrap(), 1));
        // 99...95 carries into a new digit.
        let nines = dec(&format!("{}5", "9".repeat(60)));
        assert_eq!(nines.rounded(PRECISION), Dec::new(false, pow10(59).into_owned(), 2));
    }

    #[test]
    fn quantizes_each_way() {
        assert_eq!(dec("2.345").quantize(-2, Rounding::HalfEven).unwrap(), dec("2.34"));
        assert_eq!(dec("2.355").quantize(-2, Rounding::HalfEven).unwrap(), dec("2.36"));
        assert_eq!(dec("-2.341").quantize(-2, Rounding::Ceiling).unwrap(), dec("-2.34"));
        assert_eq!(dec("-2.341").quantize(-2, Rounding::Floor).unwrap(), dec("-2.35"));
        assert_eq!(dec("7").quantize(-2, Rounding::HalfEven).unwrap(), dec("7.00"));
    }

    #[test]
    fn canonical_matches_python() {
        let text = |value: &str| canonical(&NumberInput::Decimal(value));
        assert_eq!(text("1.20E+3").as_deref(), Some("1200"));
        assert_eq!(text("-0.00").as_deref(), Some("0"));
        assert_eq!(text("12.3400").as_deref(), Some("12.34"));
        assert_eq!(text("0.0000123").as_deref(), Some("0.0000123"));
        assert_eq!(text("-0.5").as_deref(), Some("-0.5"));
        assert_eq!(text("1.2345678901234567890123456789"), None, "29 digits: normalize() would round");
        let float = |value: f64, repr: &str| canonical(&NumberInput::Float { value, repr });
        assert_eq!(float(1e22, "1e+22").as_deref(), Some("10000000000000000000000"));
        assert_eq!(float(2.5, "2.5").as_deref(), Some("2.5"));
    }
}
