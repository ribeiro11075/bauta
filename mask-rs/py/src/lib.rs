//! The PyO3 layer: conversions in, results out, and nothing else.
//!
//! Masking itself lives in `bauta-core`, which has no Python
//! dependency. Keeping the boundary thin is what lets the constructions be
//! tested without an interpreter.
//!
//! One call per chunk rather than per value (`maskChunk`), or per column
//! (`Masker.maskColumn`). The interpreter is entered once for the batch, and
//! the work between the conversions runs with the GIL released -- which is
//! what lets a masking thread overlap with a reader and a writer, and spread a
//! chunk over several cores (`setThreads`). A chunk's columns are masked at
//! once: called column by column, each column's own bookkeeping ran on one
//! thread while the rest waited, and the GIL changed hands after every column.
//! Every mask depends on its value alone, so neither the thread count nor the
//! cache changes a result: only how soon it arrives.
//!
//! Values this crate does not handle are not errors. `Decimal`, `UUID`, dates,
//! bytes and non-ASCII text come back marked, and the Python layer masks those
//! itself, in position order, so a refusal Python would raise still wins.

#![allow(non_snake_case)]

use std::collections::HashMap;
use std::sync::{Arc, Mutex, RwLock};

use rayon::prelude::*;

use num_bigint::BigInt;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyAnyMethods, PyDict, PyDictMethods, PyFloat, PyInt, PyList, PyListMethods, PyString, PyTuple, PyTupleMethods, PyType};

use bauta_core::cheap;
use bauta_core::{Charset, FakeKind, FakeLists, FakeStrategy, FpeStrategy, KeyStrategy, KeyedHash, MaskError, NumberInput, NumberOutput, NumberStrategy};

#[global_allocator]
static ALLOCATOR: mimalloc::MiMalloc = mimalloc::MiMalloc;

/// Values a `key`, `fpe` or `fake*` column remembers across chunks, and the
/// longest text it remembers. Foreign keys and statuses are mostly repeats, but seldom
/// within one chunk; the cache keeps the first values it sees rather than
/// churning, so a column of distinct values costs a lookup, not an eviction.
const CACHE_ENTRIES: usize = 65_536;
const CACHE_MAXIMUM_TEXT: usize = 64;

/// Distinct values in a call below which splitting it across threads costs
/// more than it saves.
const PARALLEL_MINIMUM: usize = 256;

/// The pool masking runs on in this process, or None for the calling thread
/// alone. Set by `setThreads`; one per process, shared by its columns.
static POOL: RwLock<Option<Arc<rayon::ThreadPool>>> = RwLock::new(None);

/// Why a position came back unmasked. The Python layer turns REFUSED into
/// MaskingError with the message alongside it, and FALLBACK into a call to its
/// own implementation.
const FALLBACK: &str = "fallback";
const REFUSED: &str = "refused";

/// What crossed the boundary, owned by Rust so the GIL can be dropped.
enum Input {
    Null,
    Int(BigInt),
    Text(String),
    /// A type this crate does not mask; Python's to handle.
    Other,
    /// A bool, which every strategy here refuses -- and must, since Python
    /// checks for it before the int branch that would otherwise swallow it.
    Bool,
    /// For `number` only: a float with the `repr` Python computes from, and a
    /// Decimal as `str` spells it. Every other strategy gets these as Other.
    Float { value: f64, repr: String },
    Decimal(String),
}

impl Input {
    /// The value as `number` takes it, or None for anything else.
    fn asNumber(&self) -> Option<NumberInput<'_>> {
        match self {
            Input::Int(value) => Some(NumberInput::Int(value)),
            Input::Float { value, repr } => Some(NumberInput::Float { value: *value, repr }),
            Input::Decimal(text) => Some(NumberInput::Decimal(text)),
            _ => None,
        }
    }
}

#[derive(Clone)]
enum Output {
    Null,
    Int(BigInt),
    Text(String),
    Float(f64),
    /// Spelled for `decimal.Decimal` to read back exactly.
    Decimal(String),
    Fallback,
    Refused(String),
}

enum Strategy {
    Key(KeyStrategy),
    Fpe(Box<FpeStrategy>),
    Hash { length: usize, prefix: String },
    Email { length: usize, mailDomain: String, keepDomain: bool },
    Digits { keepLeading: usize, keepTrailing: usize },
    Fake(Box<FakeStrategy>),
    Number(Box<NumberStrategy>),
}

impl Strategy {
    /// The strategy's own name, for the refusal messages that carry it.
    fn name(&self) -> &'static str {
        match self {
            Strategy::Key(_) => "key",
            Strategy::Fpe(_) => "fpe",
            Strategy::Hash { .. } => "hash",
            Strategy::Email { .. } => "email",
            Strategy::Digits { .. } => "digits",
            Strategy::Fake(_) => "fake",
            Strategy::Number(_) => "number",
        }
    }

    fn maskOne(&self, hash: &KeyedHash, input: &Input) -> Output {
        let result: Result<Output, MaskError> = match (self, input) {
            (_, Input::Null) => return Output::Null,
            (_, Input::Other) => return Output::Fallback,

            // number refuses anything but a number with a message Python
            // spells, so those go back; the numbers it masks here.
            (Strategy::Number(strategy), input) => match input.asNumber() {
                Some(number) => strategy.mask(hash, &number).map(Output::from),
                None => return Output::Fallback,
            },
            (_, Input::Float { .. } | Input::Decimal(_)) => return Output::Fallback,

            (Strategy::Key(_) | Strategy::Fpe(_), Input::Bool) => Err(MaskError::notABool(self.name())),
            // Keyed on the bytes Python keys them on: text as UTF-8, whatever
            // its script, and an integer's decimal digits.
            (Strategy::Fake(strategy), Input::Text(value)) => Ok(Output::Text(strategy.mask(hash, value.as_bytes()))),
            (Strategy::Fake(strategy), Input::Int(value)) => Ok(Output::Text(strategy.mask(hash, value.to_string().as_bytes()))),

            (Strategy::Digits { .. }, Input::Bool) => Err(MaskError::Refused(
                "the digits strategy needs text or an integer, got bool".to_owned(),
            )),
            // hash keys bools as 1/0 and email refuses them by type name, both
            // of which the Python layer is better placed to spell.
            (_, Input::Bool) => return Output::Fallback,

            (Strategy::Key(strategy), Input::Int(value)) => strategy.maskInteger(hash, value).map(Output::Int),
            (Strategy::Key(strategy), Input::Text(value)) => strategy.maskText(hash, value).map(Output::Text),

            (Strategy::Fpe(strategy), Input::Int(value)) => strategy.maskInteger(hash, value).map(Output::Int),
            (Strategy::Fpe(strategy), Input::Text(value)) => strategy.maskText(hash, value).map(Output::Text),

            (Strategy::Hash { length, prefix }, Input::Int(value)) => {
                Ok(Output::Text(cheap::maskHash(hash, value.to_string().as_bytes(), *length, prefix)))
            }
            (Strategy::Hash { length, prefix }, Input::Text(value)) => {
                Ok(Output::Text(cheap::maskHash(hash, value.as_bytes(), *length, prefix)))
            }

            // email takes text only, and names the offending type in its
            // refusal -- which Python spells, so an integer goes back.
            (Strategy::Email { .. }, Input::Int(_)) => return Output::Fallback,
            (Strategy::Email { length, mailDomain, keepDomain }, Input::Text(value)) => {
                cheap::maskEmail(hash, value, *length, mailDomain, *keepDomain).map(Output::Text)
            }

            (Strategy::Digits { keepLeading, keepTrailing }, Input::Int(value)) => {
                cheap::maskDigitsInteger(hash, value, *keepLeading, *keepTrailing).map(Output::Int)
            }
            (Strategy::Digits { keepLeading, keepTrailing }, Input::Text(value)) => {
                cheap::maskDigitsText(hash, value, *keepLeading, *keepTrailing).map(Output::Text)
            }
        };

        match result {
            Ok(output) => output,
            Err(MaskError::Unsupported) => Output::Fallback,
            Err(MaskError::Refused(message)) => Output::Refused(message),
        }
    }
}

impl From<NumberOutput> for Output {
    fn from(output: NumberOutput) -> Self {
        match output {
            NumberOutput::Int(number) => Output::Int(number),
            NumberOutput::Float(number) => Output::Float(number),
            NumberOutput::Decimal(text) => Output::Decimal(text),
        }
    }
}

/// A value as a cache or a batch's repeats know it.
#[derive(Clone, PartialEq, Eq, Hash)]
enum CacheKey {
    Text(String),
    Int(BigInt),
    /// A float by its bits, whose repr follows from them.
    Float(u64),
    Decimal(String),
}

impl CacheKey {
    fn of(input: &Input) -> Option<Self> {
        match input {
            Input::Text(text) => Some(CacheKey::Text(text.clone())),
            Input::Int(number) => Some(CacheKey::Int(number.clone())),
            Input::Float { value, .. } => Some(CacheKey::Float(value.to_bits())),
            Input::Decimal(text) => Some(CacheKey::Decimal(text.clone())),
            _ => None,
        }
    }

    fn remembered(&self) -> bool {
        match self {
            CacheKey::Text(text) => text.len() <= CACHE_MAXIMUM_TEXT,
            CacheKey::Int(_) | CacheKey::Float(_) | CacheKey::Decimal(_) => true,
        }
    }
}

/// One Python value as Rust knows it, owned so the GIL can be dropped after.
/// `decimal` is `decimal.Decimal` for a `number` column, which alone takes
/// floats and Decimals, and None for the rest.
fn convert(value: &Bound<'_, PyAny>, decimal: Option<&Bound<'_, PyAny>>) -> PyResult<Input> {
    Ok(if value.is_none() {
        Input::Null
    } else if value.is_instance_of::<pyo3::types::PyBool>() {
        Input::Bool
    } else if value.is_instance_of::<PyString>() {
        Input::Text(value.extract::<String>()?)
    } else if value.is_instance_of::<pyo3::types::PyInt>() {
        // An i64 first, which crosses through the C API. Under abi3 a BigInt
        // crosses by calling int.to_bytes, a Python method call per value with
        // the GIL held: it capped an integer column at eight threads at half
        // the throughput of a text one. Anything wider still takes that path.
        match value.extract::<i64>() {
            Ok(number) => Input::Int(BigInt::from(number)),
            Err(_) => match value.extract::<BigInt>() {
                Ok(number) => Input::Int(number),
                Err(_) => Input::Other,
            },
        }
    } else if decimal.is_some() && value.is_instance_of::<pyo3::types::PyFloat>() {
        Input::Float { value: value.extract::<f64>()?, repr: value.repr()?.extract::<String>()? }
    } else if decimal.is_some_and(|decimal| value.is_instance(decimal).unwrap_or(false)) {
        Input::Decimal(value.str()?.extract::<String>()?)
    } else {
        Input::Other
    })
}

/// Where a position's answer comes from.
enum Source {
    Ready(Output),
    Computed(usize),
}

/// One column's masker: the key, the domain and the strategy, built once and
/// called per chunk. Frozen, so `maskChunk` can reach it with the GIL
/// released: nothing in it changes but the cache, behind its own lock.
#[pyclass(frozen)]
struct Masker {
    hash: KeyedHash,
    strategy: Strategy,
    /// Masks remembered across calls, for the strategies that cost enough to
    /// be worth it; None for the rest.
    cache: Option<Mutex<HashMap<CacheKey, Output>>>,
}

#[pymethods]
impl Masker {
    /// `subkey` is what `masking.KeyedHash` already derived for this column,
    /// so the masking key itself never crosses the boundary.
    #[new]
    #[pyo3(signature = (subkey, strategy, options))]
    fn new(subkey: &[u8], strategy: &str, options: &Bound<'_, PyDict>) -> PyResult<Self> {
        let subkey: &[u8; 32] = subkey
            .try_into()
            .map_err(|_| PyValueError::new_err("a subkey is 32 bytes"))?;
        let hash = KeyedHash::fromSubkey(subkey);

        let text = |name: &str, fallback: &str| -> PyResult<String> {
            Ok(match options.get_item(name)? {
                Some(value) => value.extract::<String>()?,
                None => fallback.to_owned(),
            })
        };
        let number = |name: &str, fallback: usize| -> PyResult<usize> {
            Ok(match options.get_item(name)? {
                Some(value) => value.extract::<usize>()?,
                None => fallback,
            })
        };
        let flag = |name: &str| -> PyResult<bool> {
            Ok(match options.get_item(name)? {
                Some(value) => value.extract::<bool>()?,
                None => false,
            })
        };

        let charset = || -> PyResult<Charset> {
            let name = text("charset", "alphanumeric")?;
            Charset::parse(&name).ok_or_else(|| PyValueError::new_err(format!("unknown charset {name:?}")))
        };

        let strategy = match strategy {
            "key" => Strategy::Key(KeyStrategy::new(charset()?)),
            "fpe" => Strategy::Fpe(Box::new(FpeStrategy::new(&hash, charset()?, flag("strict")?))),
            "hash" => Strategy::Hash { length: number("length", 16)?, prefix: text("prefix", "")? },
            "email" => Strategy::Email {
                length: number("length", 12)?,
                mailDomain: text("mailDomain", "example.test")?,
                keepDomain: flag("keepDomain")?,
            },
            "digits" => Strategy::Digits {
                keepLeading: number("keepLeading", 0)?,
                keepTrailing: number("keepTrailing", 0)?,
            },
            other => match FakeKind::parse(other) {
                Some(kind) => {
                    let list = |name: &str| -> PyResult<Vec<String>> {
                        options.get_item(name)?.ok_or_else(|| PyValueError::new_err(format!("{other} needs {name}")))?.extract()
                    };
                    let lists = FakeLists {
                        firstNames: list("firstNames")?,
                        lastNames: list("lastNames")?,
                        cities: list("cities")?,
                        streets: list("streets")?,
                        streetKinds: list("streetKinds")?,
                        address: text("address", "")?,
                        companySuffixes: list("companySuffixes")?,
                        companyWords: list("companyWords")?,
                    };
                    let maxLength = match options.get_item("maxLength")? {
                        Some(value) if !value.is_none() => Some(value.extract::<usize>()?),
                        _ => None,
                    };
                    Strategy::Fake(Box::new(FakeStrategy::new(kind, lists, maxLength).map_err(PyValueError::new_err)?))
                }
                None if other == "number" => {
                    let optional = |name: &str| -> PyResult<Option<String>> {
                        Ok(match options.get_item(name)? {
                            Some(value) if !value.is_none() => Some(value.extract::<String>()?),
                            _ => None,
                        })
                    };
                    let decimals = match options.get_item("decimals")? {
                        Some(value) if !value.is_none() => Some(value.extract::<u32>()?),
                        _ => None,
                    };
                    let (minimum, maximum, variance) = (optional("min")?, optional("max")?, optional("variance")?);
                    Strategy::Number(Box::new(
                        NumberStrategy::new(minimum.as_deref(), maximum.as_deref(), variance.as_deref(), decimals).map_err(PyValueError::new_err)?,
                    ))
                }
                None => return Err(PyValueError::new_err(format!("no native masker for strategy {other:?}"))),
            },
        };

        let cache = matches!(strategy, Strategy::Key(_) | Strategy::Fpe(_) | Strategy::Fake(_)).then(|| Mutex::new(HashMap::new()));

        Ok(Self { hash, strategy, cache })
    }

    /// Masks one column.
    ///
    /// Returns `(masked, problems)`. `problems` maps a position to FALLBACK or
    /// to a REFUSED message; every other position of `masked` is the answer.
    /// The caller walks `problems` in position order, so a refusal Python would
    /// have raised first still raises first.
    ///
    /// `values` is any sequence, so the caller hands over the column it already
    /// has -- a list or a tuple -- rather than copying it into a list first.
    fn maskColumn<'py>(&self, py: Python<'py>, values: &Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyList>, Bound<'py, PyDict>)> {
        // Convert with the GIL held. A list and a tuple are walked by their own
        // iterators, which is markedly faster than the general protocol: a
        // column arrives as one or the other, and going through try_iter for a
        // tuple cost more than handing the column over uncopied saved.
        let decimal = match self.strategy {
            Strategy::Number(_) => Some(py.import("decimal")?.getattr("Decimal")?),
            _ => None,
        };
        let decimal = decimal.as_ref();
        let mut inputs = Vec::with_capacity(values.len().unwrap_or(0));
        if let Ok(list) = values.cast::<PyList>() {
            for value in list.iter() {
                inputs.push(convert(&value, decimal)?);
            }
        } else if let Ok(tuple) = values.cast::<PyTuple>() {
            for value in tuple.iter() {
                inputs.push(convert(&value, decimal)?);
            }
        } else {
            for value in values.try_iter()? {
                inputs.push(convert(&value?, decimal)?);
            }
        }

        // Compute without it: each distinct value once, from the cache where it
        // can be, the rest across the pool.
        let outputs = py.detach(|| self.maskInputs(&inputs));

        // Build results with it again.
        let masked = PyList::empty(py);
        let problems = PyDict::new(py);
        for (index, output) in outputs.into_iter().enumerate() {
            match &output {
                Output::Fallback => problems.set_item(index, FALLBACK)?,
                Output::Refused(message) => problems.set_item(index, (REFUSED, message))?,
                _ => {}
            }
            masked.append(toPython(py, &output, decimal)?)?;
        }

        Ok((masked, problems))
    }
}

/// An output as Python knows it, and None where the Python layer finishes the
/// position. `decimal` is `decimal.Decimal` for a `number` column.
fn toPython<'py>(py: Python<'py>, output: &Output, decimal: Option<&Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyAny>> {
    Ok(match output {
        // Back through the C API where it fits, for the reason convert gives.
        Output::Int(number) => match i64::try_from(number) {
            Ok(small) => small.into_pyobject(py)?.into_any(),
            Err(_) => number.into_pyobject(py)?.into_any(),
        },
        Output::Text(text) => PyString::new(py, text).into_any(),
        Output::Float(number) => PyFloat::new(py, *number).into_any(),
        Output::Decimal(text) => match decimal {
            Some(decimal) => decimal.call1((text.as_str(),))?,
            None => unreachable!("only a number column returns a Decimal"),
        },
        Output::Null | Output::Fallback | Output::Refused(_) => py.None().into_bound(py),
    })
}

/// Masks a chunk: `rows` is a sequence of rows, and `maskers` has one entry per
/// column, None for a column carried through as it is.
///
/// Returns `(rows, problems)`: the rows as tuples, and `problems` a list of
/// `(column, row, FALLBACK or (REFUSED, message))` in row order, each such
/// position None in `rows`. The caller finishes them column by column, each
/// column in row order, so the refusal Python would have raised first still
/// raises first.
///
/// The GIL is held to read the rows and to build the result, and released once
/// between, while every masked column is masked at once across the pool. What
/// Rust allocated is freed without it: the inputs once masked, the outputs on a
/// thread of their own once turned into Python objects. Freeing tens of
/// millions of strings a job is otherwise a quarter of the time the GIL is held.
#[pyfunction]
fn maskChunk<'py>(py: Python<'py>, maskers: Vec<Option<Py<Masker>>>, rows: &Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyList>, Bound<'py, PyList>)> {
    let width = maskers.len();
    let decimal = py.import("decimal")?.getattr("Decimal")?;
    let decimals: Vec<Option<&Bound<'py, PyAny>>> = maskers
        .iter()
        .map(|masker| match masker {
            Some(masker) if matches!(masker.get().strategy, Strategy::Number(_)) => Some(&decimal),
            _ => None,
        })
        .collect();

    // Rows as tuples, which the result reuses for the columns carried through;
    // a row of another kind is copied into one.
    let mut originals: Vec<Bound<'py, PyTuple>> = Vec::with_capacity(rows.len().unwrap_or(0));
    let mut inputs: Vec<Vec<Input>> = maskers.iter().map(|_| Vec::with_capacity(originals.capacity())).collect();
    let mut read = |row: Bound<'py, PyAny>| -> PyResult<()> {
        let row = match row.cast::<PyTuple>() {
            Ok(tuple) => tuple.clone(),
            Err(_) => PyTuple::new(py, row.try_iter()?.collect::<PyResult<Vec<_>>>()?)?,
        };
        if row.len() != width {
            return Err(PyValueError::new_err(format!("the policy covers {width} column(s) and a row has {}", row.len())));
        }
        for (index, masker) in maskers.iter().enumerate() {
            if masker.is_some() {
                inputs[index].push(convert(&row.get_item(index)?, decimals[index])?);
            }
        }
        originals.push(row);
        Ok(())
    };
    // A list walked by its own iterator, as maskColumn explains.
    if let Ok(list) = rows.cast::<PyList>() {
        for row in list.iter() {
            read(row)?;
        }
    } else {
        for row in rows.try_iter()? {
            read(row?)?;
        }
    }

    let outputs: Vec<Vec<Output>> = py.detach(|| {
        let mask = |(masker, inputs): (&Option<Py<Masker>>, &Vec<Input>)| match masker {
            Some(masker) => masker.get().maskInputs(inputs),
            None => Vec::new(),
        };
        let pool = POOL.read().unwrap_or_else(|poisoned| poisoned.into_inner()).clone();
        let outputs = match pool {
            // Nested in the pool: each column's own values spread over it too.
            Some(pool) => pool.install(|| maskers.par_iter().zip(inputs.par_iter()).map(mask).collect()),
            None => maskers.iter().zip(inputs.iter()).map(mask).collect(),
        };
        drop(std::mem::take(&mut inputs));
        outputs
    });

    let problems = PyList::empty(py);
    let mut columns: Vec<std::slice::Iter<Output>> = outputs.iter().map(|column| column.iter()).collect();
    let mut masked: Vec<Bound<'py, PyTuple>> = Vec::with_capacity(originals.len());
    let mut values: Vec<Bound<'py, PyAny>> = Vec::with_capacity(width);
    for (position, row) in originals.iter().enumerate() {
        for index in 0..width {
            if maskers[index].is_none() {
                values.push(row.get_item(index)?);
                continue;
            }
            let output = columns[index].next().expect("one output per input");
            match output {
                Output::Fallback => problems.append((index, position, FALLBACK))?,
                Output::Refused(message) => problems.append((index, position, (REFUSED, message.as_str())))?,
                _ => {}
            }
            values.push(toPython(py, output, decimals[index])?);
        }
        masked.push(PyTuple::new(py, values.drain(..))?);
    }
    std::thread::spawn(move || drop(outputs));

    Ok((PyList::new(py, masked)?, problems))
}

impl Masker {
    fn maskInputs(&self, inputs: &[Input]) -> Vec<Output> {
        let mut sources: Vec<Source> = Vec::with_capacity(inputs.len());
        let mut work: Vec<usize> = Vec::new();
        let mut firstOf: HashMap<CacheKey, usize> = HashMap::new();

        {
            let cache = self.cache.as_ref().map(|cache| cache.lock().unwrap_or_else(|poisoned| poisoned.into_inner()));

            for (index, input) in inputs.iter().enumerate() {
                let Some(key) = CacheKey::of(input) else {
                    // Nulls and values Python masks are cheap to answer here.
                    sources.push(Source::Ready(self.strategy.maskOne(&self.hash, input)));
                    continue;
                };
                if let Some(output) = cache.as_ref().and_then(|cache| cache.get(&key)) {
                    sources.push(Source::Ready(output.clone()));
                    continue;
                }
                let slot = *firstOf.entry(key).or_insert_with(|| {
                    work.push(index);
                    work.len() - 1
                });
                sources.push(Source::Computed(slot));
            }
        }

        let mask = |index: &usize| self.strategy.maskOne(&self.hash, &inputs[*index]);
        let pool = POOL.read().unwrap_or_else(|poisoned| poisoned.into_inner()).clone();
        let computed: Vec<Output> = match pool {
            Some(pool) if work.len() >= PARALLEL_MINIMUM => pool.install(|| work.par_iter().map(mask).collect()),
            _ => work.iter().map(mask).collect(),
        };

        if let Some(cache) = &self.cache {
            let mut cache = cache.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            for (slot, index) in work.iter().enumerate() {
                if cache.len() >= CACHE_ENTRIES {
                    break;
                }
                // Only answers: a refusal or a fallback is decided again, by
                // whichever implementation meets it first.
                if matches!(computed[slot], Output::Text(_) | Output::Int(_)) {
                    if let Some(key) = CacheKey::of(&inputs[*index]).filter(CacheKey::remembered) {
                        cache.insert(key, computed[slot].clone());
                    }
                }
            }
        }

        sources
            .into_iter()
            .map(|source| match source {
                Source::Ready(output) => output,
                Source::Computed(slot) => computed[slot].clone(),
            })
            .collect()
    }
}

/// The cores this process may use: a container's CPU quota on Linux, where
/// Python's os.cpu_count() reports the host's.
#[pyfunction]
fn availableCores() -> usize {
    std::thread::available_parallelism().map(|cores| cores.get()).unwrap_or(1)
}

/// Each column's exact types, and whether its ints all fit in 64 bits.
type ColumnKinds<'py> = (Vec<Vec<Bound<'py, PyType>>>, Vec<bool>);

/// What a load needs to know of a chunk before sending it to a database, in
/// one pass: each column's distinct exact types, in the order first met, and
/// whether every exact `int` in it fits in 64 bits. None where `rows` isn't a
/// list of tuples of one width, which the Python layer then reads itself.
///
/// Not masking, but the same per-value walk the GIL makes slow in Python:
/// `bauta.database.values.ValuePreparer` finds the columns a driver takes
/// differently from these.
#[pyfunction]
fn columnKinds<'py>(py: Python<'py>, rows: &Bound<'py, PyAny>) -> PyResult<Option<ColumnKinds<'py>>> {
    let Ok(rows) = rows.cast::<PyList>() else {
        return Ok(None);
    };
    // Types told apart by address, which costs no reference count; a type is
    // taken as an object only the first time a column meets it.
    let int = py.get_type::<PyInt>().as_type_ptr();
    let mut kinds: Vec<Vec<(*mut pyo3::ffi::PyTypeObject, Bound<'py, PyType>)>> = Vec::new();
    let mut fits: Vec<bool> = Vec::new();

    for (position, row) in rows.iter().enumerate() {
        let Ok(row) = row.cast::<PyTuple>() else {
            return Ok(None);
        };
        if position == 0 {
            kinds = (0..row.len()).map(|_| Vec::new()).collect();
            fits = vec![true; row.len()];
        } else if row.len() != kinds.len() {
            return Ok(None);
        }
        for (index, value) in row.iter().enumerate() {
            let kind = value.get_type_ptr();
            // Exactly int: a bool, or an IntEnum, is another kind.
            if fits[index] && kind == int && value.extract::<i64>().is_err() {
                fits[index] = false;
            }
            if !kinds[index].iter().any(|(known, _)| *known == kind) {
                kinds[index].push((kind, value.get_type()));
            }
        }
    }

    Ok(Some((kinds.into_iter().map(|column| column.into_iter().map(|(_, kind)| kind).collect()).collect(), fits)))
}

/// How many threads mask a chunk in this process, 1 for the calling thread
/// alone. Results are the same for any count.
#[pyfunction]
fn setThreads(threads: usize) -> PyResult<()> {
    if threads == 0 {
        return Err(PyValueError::new_err("threads must be at least 1"));
    }
    let pool = if threads == 1 {
        None
    } else {
        let built = rayon::ThreadPoolBuilder::new()
            .num_threads(threads)
            .thread_name(|index| format!("bauta-mask-{index}"))
            .build()
            .map_err(|error| PyValueError::new_err(format!("could not start {threads} masking threads: {error}")))?;
        Some(Arc::new(built))
    };
    *POOL.write().unwrap_or_else(|poisoned| poisoned.into_inner()) = pool;

    Ok(())
}

/// The masking threads in this process.
#[pyfunction]
fn threads() -> usize {
    POOL.read().unwrap_or_else(|poisoned| poisoned.into_inner()).as_ref().map_or(1, |pool| pool.current_num_threads())
}

#[pymodule]
fn bauta_rs(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    module.add("FALLBACK", FALLBACK)?;
    module.add("REFUSED", REFUSED)?;
    module.add_class::<Masker>()?;
    module.add_function(wrap_pyfunction!(maskChunk, module)?)?;
    module.add_function(wrap_pyfunction!(columnKinds, module)?)?;
    module.add_function(wrap_pyfunction!(availableCores, module)?)?;
    module.add_function(wrap_pyfunction!(setThreads, module)?)?;
    module.add_function(wrap_pyfunction!(threads, module)?)?;

    Ok(())
}
