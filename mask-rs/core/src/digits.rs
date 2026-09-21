//! Powers of ten, and counting a number's decimal digits against them --
//! which `key` and `fpe` ask of every integer and `number` of every
//! intermediate result.

use std::borrow::Cow;
use std::sync::OnceLock;

use num_bigint::BigUint;
use num_traits::Zero;

/// The powers of ten kept ready: past the furthest `number` scales a value
/// (twice its 400-digit exponent limit and its 60-digit precision), and so
/// past `key`'s 256-digit limit too.
const TABLE_SIZE: usize = 2 * 400 + 2 * 60 + 1;

fn powersOfTen() -> &'static [BigUint] {
    static POWERS: OnceLock<Vec<BigUint>> = OnceLock::new();
    POWERS.get_or_init(|| {
        let mut powers = vec![BigUint::from(1u8)];
        while powers.len() < TABLE_SIZE {
            let next = powers.last().unwrap() * 10u8;
            powers.push(next);
        }
        powers
    })
}

/// `10**exponent`, from the table where it's there.
pub fn pow10(exponent: u64) -> Cow<'static, BigUint> {
    match powersOfTen().get(exponent as usize) {
        Some(power) => Cow::Borrowed(power),
        None => Cow::Owned(BigUint::from(10u8).pow(exponent as u32)),
    }
}

/// How many decimal digits `value` has, one for zero. Exact up to the
/// table's 920 digits and an estimate past them, so that a limit below
/// that -- `key`'s 256 -- is never reached by writing a long number out,
/// which Python refuses past 4,300 digits.
pub fn decimalDigits(value: &BigUint) -> usize {
    if value.is_zero() {
        return 1;
    }
    let powers = powersOfTen();
    // log10(2) under-estimates by at most one digit; the table settles it.
    let mut count = ((value.bits() - 1) as f64 * std::f64::consts::LOG10_2) as usize + 1;
    while count < powers.len() && value >= &powers[count] {
        count += 1;
    }
    while count > 1 && count - 1 < powers.len() && value < &powers[count - 1] {
        count -= 1;
    }
    count
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decimal_digits_counts_what_python_counts() {
        for (value, expected) in [(0u64, 1), (1, 1), (9, 1), (10, 2), (99, 2), (100, 3), (12345, 5), (u64::MAX, 20)] {
            assert_eq!(decimalDigits(&BigUint::from(value)), expected, "digits of {value}");
        }
        assert_eq!(decimalDigits(&BigUint::from(10u8).pow(255)), 256);
        assert_eq!(decimalDigits(&(BigUint::from(10u8).pow(256) - 1u8)), 256);
        assert_eq!(decimalDigits(&BigUint::from(10u8).pow(256)), 257);
        assert!(decimalDigits(&BigUint::from(10u8).pow(5000)) > 256, "past the table, still past key's limit");
    }

    #[test]
    fn powers_past_the_table_are_computed() {
        assert_eq!(pow10(3).as_ref(), &BigUint::from(1000u32));
        assert_eq!(pow10(TABLE_SIZE as u64 + 5).as_ref(), &BigUint::from(10u8).pow(TABLE_SIZE as u32 + 5));
    }
}
