//! Observation JSONL serialization and normalization.
//!
//! Rust port of `services/mine_sentinel/storage/codec.py`. The Python
//! wrapper delegates `normalize_record` / `record_to_json` / `json_line` /
//! `dedupe_key` here when the native extension is importable.
//!
//! Performance design: each per-record call crosses the PyO3 boundary exactly
//! once. Inside Rust we convert the relevant record fields into native
//! `serde_json::Value` trees (or primitive Vec/Strings) and do all truncate /
//! compact / hash work without ever calling back into Python. `json_line`
//! serializes with `serde_json` directly — no `json.dumps` round-trip.

use blake2::digest::{Update, VariableOutput};
use blake2::Blake2bVar;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use pyo3::BoundObject;
use serde_json::{Map, Value};

/// Drop-in replacement for `ObservationRecordCodec`. Holds the small set of
/// config-derived limits needed by `normalize_record` / `record_to_json` /
/// `dedupe_key`. The Python `MineSentinelConfig` is unpacked once at
/// construction; per-record work never touches Python attribute access for
/// config.
#[pyclass]
pub struct ObservationRecordCodec {
    max_content_length: usize,
    max_tags_per_record: usize,
    max_raw_fields: usize,
    include_raw: bool,
    dedupe_window_seconds: i64,
}

#[pymethods]
impl ObservationRecordCodec {
    #[new]
    #[pyo3(signature = (
        max_content_length = 4000,
        max_tags_per_record = 8,
        max_raw_fields = 16,
        include_raw = false,
        dedupe_window_seconds = 120,
    ))]
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        max_content_length: usize,
        max_tags_per_record: usize,
        max_raw_fields: usize,
        include_raw: bool,
        dedupe_window_seconds: i64,
    ) -> Self {
        Self {
            max_content_length,
            max_tags_per_record,
            max_raw_fields,
            include_raw,
            dedupe_window_seconds,
        }
    }

    /// Mutate the Python `ObservationRecord` in place. Each Python attribute
    /// is read once, transformed in Rust, and written back once — no
    /// per-field Python callbacks. Operates directly on PyDict (no serde_json
    /// round-trip) because we must hand a Python dict back anyway.
    pub fn normalize_record(&self, py: Python, record: &Bound<PyAny>) -> PyResult<()> {
        // ---- content ----
        let content: String = record.getattr("content")?.extract()?;
        record.setattr("content", truncate(&content, self.max_content_length))?;

        // ---- tags ----
        let tags: Vec<String> = record.getattr("tags")?.extract()?;
        let limit = self.max_tags_per_record.min(tags.len());
        let new_tags: Vec<String> = tags
            .into_iter()
            .take(limit)
            .map(|t| truncate(&t, self.max_content_length))
            .collect();
        record.setattr("tags", new_tags)?;

        // ---- context ----
        let context_obj = record.getattr("context")?;
        let context_dict: Bound<PyDict> = context_obj.extract()?;
        let compacted_context = compact_py_dict(py, &context_dict, self.max_raw_fields, self.max_content_length)?;
        record.setattr("context", compacted_context)?;

        // ---- raw ----
        if self.include_raw {
            let raw_obj = record.getattr("raw")?;
            let raw_dict: Bound<PyDict> = raw_obj.extract()?;
            let compacted_raw = compact_py_dict(py, &raw_dict, self.max_raw_fields, self.max_content_length)?;
            record.setattr("raw", compacted_raw)?;
        } else {
            record.setattr("raw", PyDict::new(py))?;
        }
        Ok(())
    }

    /// Build the JSONL-safe dict mirroring `record_to_json`. Returns a new
    /// Python dict; the input record is left untouched.
    pub fn record_to_json<'py>(
        &self,
        py: Python<'py>,
        record: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let event_id: String = record.getattr("event_id")?.extract()?;
        let kind: String = record.getattr("kind")?.extract()?;
        let timestamp: i64 = record.getattr("timestamp")?.extract()?;
        let server_id: String = record.getattr("server_id")?.extract()?;
        let server_name: String = record.getattr("server_name")?.extract()?;
        let backend_server: String = record.getattr("backend_server")?.extract()?;
        let proxy_id: String = record.getattr("proxy_id")?.extract()?;
        let player_name: String = record.getattr("player_name")?.extract()?;
        let player_uuid_hash: String = record.getattr("player_uuid_hash")?.extract()?;
        let content: String = record.getattr("content")?.extract()?;
        let tags: Vec<String> = record.getattr("tags")?.extract()?;
        let context_obj = record.getattr("context")?;
        let context_value = py_any_to_json(&context_obj)?;
        let raw_value = if self.include_raw {
            let raw_obj = record.getattr("raw")?;
            py_any_to_json(&raw_obj)?
        } else {
            Value::Object(Map::new())
        };

        // Build the output as a serde_json::Value so json_line can reuse this
        // path without re-crossing into Python. For record_to_json we hand it
        // back to Python as a dict.
        let mut player = Map::new();
        player.insert("name".to_string(), Value::String(player_name));
        player.insert("uuidHash".to_string(), Value::String(player_uuid_hash));

        let mut out = Map::new();
        out.insert("eventId".to_string(), Value::String(event_id));
        out.insert("kind".to_string(), Value::String(kind));
        out.insert("timestamp".to_string(), Value::Number(timestamp.into()));
        out.insert("serverId".to_string(), Value::String(server_id));
        out.insert("serverName".to_string(), Value::String(server_name));
        out.insert("backendServer".to_string(), Value::String(backend_server));
        out.insert("proxyId".to_string(), Value::String(proxy_id));
        out.insert("player".to_string(), Value::Object(player));
        out.insert("content".to_string(), Value::String(content));
        out.insert(
            "tags".to_string(),
            Value::Array(tags.into_iter().map(Value::String).collect()),
        );
        out.insert("context".to_string(), context_value);
        out.insert("raw".to_string(), raw_value);

        json_to_py(py, &Value::Object(out))?
            .extract::<Bound<PyDict>>()
            .map_err(Into::into)
    }

    /// Serialize record to a single compact JSONL line (no ensure_ascii).
    /// Pure Rust: `serde_json::to_string` — no Python `json.dumps` callback.
    pub fn json_line(&self, _py: Python, record: &Bound<PyAny>) -> PyResult<String> {
        // Build the JSON tree directly (no PyDict intermediate), then serialize.
        let event_id: String = record.getattr("event_id")?.extract()?;
        let kind: String = record.getattr("kind")?.extract()?;
        let timestamp: i64 = record.getattr("timestamp")?.extract()?;
        let server_id: String = record.getattr("server_id")?.extract()?;
        let server_name: String = record.getattr("server_name")?.extract()?;
        let backend_server: String = record.getattr("backend_server")?.extract()?;
        let proxy_id: String = record.getattr("proxy_id")?.extract()?;
        let player_name: String = record.getattr("player_name")?.extract()?;
        let player_uuid_hash: String = record.getattr("player_uuid_hash")?.extract()?;
        let content: String = record.getattr("content")?.extract()?;
        let tags: Vec<String> = record.getattr("tags")?.extract()?;
        let context_obj = record.getattr("context")?;
        let context_value = py_any_to_json(&context_obj)?;
        let raw_value = if self.include_raw {
            let raw_obj = record.getattr("raw")?;
            py_any_to_json(&raw_obj)?
        } else {
            Value::Object(Map::new())
        };

        let mut player = Map::new();
        player.insert("name".to_string(), Value::String(player_name));
        player.insert("uuidHash".to_string(), Value::String(player_uuid_hash));

        let mut out = Map::new();
        out.insert("eventId".to_string(), Value::String(event_id));
        out.insert("kind".to_string(), Value::String(kind));
        out.insert("timestamp".to_string(), Value::Number(timestamp.into()));
        out.insert("serverId".to_string(), Value::String(server_id));
        out.insert("serverName".to_string(), Value::String(server_name));
        out.insert("backendServer".to_string(), Value::String(backend_server));
        out.insert("proxyId".to_string(), Value::String(proxy_id));
        out.insert("player".to_string(), Value::Object(player));
        out.insert("content".to_string(), Value::String(content));
        out.insert(
            "tags".to_string(),
            Value::Array(tags.into_iter().map(Value::String).collect()),
        );
        out.insert("context".to_string(), context_value);
        out.insert("raw".to_string(), raw_value);

        serde_json::to_string(&Value::Object(out))
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))
    }

    /// Compute the dedupe key for a record. Mirrors `dedupe_key`:
    /// - if event_id non-empty → use it
    /// - else blake2b16 of `kind|server_id|identity|content_lower|bucket`
    pub fn dedupe_key(&self, record: &Bound<PyAny>) -> PyResult<String> {
        let event_id: String = record.getattr("event_id")?.extract()?;
        if !event_id.is_empty() {
            return Ok(event_id);
        }
        let kind: String = record.getattr("kind")?.extract()?;
        let server_id: String = record.getattr("server_id")?.extract()?;
        // `identity` is a property on the Python dataclass; getattr triggers it.
        // Use unwrap_or_default so a missing/None identity doesn't blow up.
        let identity: String = record
            .getattr("identity")?
            .extract()
            .unwrap_or_default();
        let content: String = record.getattr("content")?.extract()?;
        let timestamp: i64 = record.getattr("timestamp")?.extract()?;
        let bucket = timestamp / self.dedupe_window_seconds.max(1).saturating_mul(1000);
        let content_lower = normalize_ws_lower(&content);
        let raw = format!(
            "{}|{}|{}|{}|{}",
            kind, server_id, identity, content_lower, bucket
        );
        let mut hasher = Blake2bVar::new(16).expect("blake2b 16 bytes");
        hasher.update(raw.as_bytes());
        let mut out = [0u8; 16];
        hasher.finalize_variable(&mut out);
        Ok(format!("h:{}", hex_encode(&out)))
    }
}

// ===== shared helpers =====

/// Mirror `compact_dict` + `compact_value` operating directly on PyDict.
/// Preserves insertion order (PyDict iteration is insertion-ordered). For
/// each value: None/bool/int/float pass through; str → truncate; anything
/// else → `str()` then truncate (mirrors `json.dumps(value, default=str)`
/// for the common cases we see in context/raw).
fn compact_py_dict<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyDict>,
    max_fields: usize,
    max_len: usize,
) -> PyResult<Bound<'py, PyDict>> {
    let out = PyDict::new(py);
    let mut count = 0;
    for (key, value) in data.iter() {
        if count >= max_fields {
            break;
        }
        let key_str: String = key.extract().unwrap_or_else(|_| key.to_string());
        let compacted = compact_py_value(py, &value, max_len, max_fields)?;
        out.set_item(key_str, compacted)?;
        count += 1;
    }
    Ok(out)
}

/// Mirror `compact_value` operating on a single Python value.
/// Recursively compacts nested dicts/lists to preserve structure (e.g.
/// `context["otel"]`), instead of JSON-dumping them to a string.
fn compact_py_value<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
    max_len: usize,
    max_fields: usize,
) -> PyResult<Bound<'py, PyAny>> {
    if value.is_none() {
        return Ok(value.clone());
    }
    // bool must be checked before int (bool is a subclass of int in Python).
    if value.extract::<bool>().is_ok() {
        return Ok(value.clone());
    }
    if value.extract::<i64>().is_ok()
        || value.extract::<u64>().is_ok()
        || value.extract::<f64>().is_ok()
    {
        return Ok(value.clone());
    }
    if let Ok(s) = value.extract::<String>() {
        return Ok(truncate(&s, max_len).into_pyobject(py)?.into_any());
    }
    // 嵌套 dict → 递归 compact，保持结构化（而非 stringify）。
    if let Ok(d) = value.extract::<Bound<PyDict>>() {
        return Ok(compact_py_dict(py, &d, max_fields, max_len)?.into_any());
    }
    // 嵌套 list → 递归 compact，截断到 max_fields 项。
    if let Ok(l) = value.extract::<Bound<PyList>>() {
        let out = PyList::empty(py);
        for item in l.iter().take(max_fields) {
            out.append(compact_py_value(py, &item, max_len, max_fields)?)?;
        }
        return Ok(out.into_any());
    }
    // Fallback (unknown types): stringify via Python str() then truncate.
    let s: String = value.str()?.extract().unwrap_or_else(|_| value.to_string());
    Ok(truncate(&s, max_len).into_pyobject(py)?.into_any())
}

/// Convert any Python object into a `serde_json::Value`. Uses Python's
/// `str()` only for objects we don't recognize (mirrors `compact_value`'s
/// `json.dumps(value, default=str)` fallback).
fn py_any_to_json(obj: &Bound<PyAny>) -> PyResult<Value> {
    if obj.is_none() {
        return Ok(Value::Null);
    }
    if let Ok(b) = obj.extract::<bool>() {
        return Ok(Value::Bool(b));
    }
    if let Ok(i) = obj.extract::<i64>() {
        return Ok(Value::Number(i.into()));
    }
    if let Ok(u) = obj.extract::<u64>() {
        // u64 → serde_json::Number only if it fits; fall back to f64 otherwise.
        return Ok(Value::Number(u.into()));
    }
    if let Ok(f) = obj.extract::<f64>() {
        if let Some(n) = serde_json::Number::from_f64(f) {
            return Ok(Value::Number(n));
        }
        return Ok(Value::Null);
    }
    if let Ok(s) = obj.extract::<String>() {
        return Ok(Value::String(s));
    }
    if let Ok(d) = obj.extract::<Bound<PyDict>>() {
        let mut map = Map::with_capacity(d.len());
        for (k, v) in d.iter() {
            let key: String = k.extract().unwrap_or_else(|_| k.to_string());
            map.insert(key, py_any_to_json(&v)?);
        }
        return Ok(Value::Object(map));
    }
    if let Ok(t) = obj.extract::<Bound<PyTuple>>() {
        let mut arr = Vec::with_capacity(t.len());
        for item in t.iter() {
            arr.push(py_any_to_json(&item)?);
        }
        return Ok(Value::Array(arr));
    }
    if let Ok(l) = obj.extract::<Bound<PyList>>() {
        let mut arr = Vec::with_capacity(l.len());
        for item in l.iter() {
            arr.push(py_any_to_json(&item)?);
        }
        return Ok(Value::Array(arr));
    }
    // Fallback: stringify via Python's str() (mirrors `default=str`).
    let s: String = obj
        .str()?
        .extract()
        .unwrap_or_else(|_| obj.to_string());
    Ok(Value::String(s))
}

/// Convert a `serde_json::Value` back into a Python object.
fn json_to_py<'py>(py: Python<'py>, value: &Value) -> PyResult<Bound<'py, PyAny>> {
    match value {
        Value::Null => Ok(py.None().into_bound(py)),
        Value::Bool(b) => {
            // PyBool::new returns Borrowed (singleton True/False); convert to
            // an owned Bound before into_any so the move is valid.
            let py_bool = (*b).into_pyobject(py)?.into_bound();
            Ok(py_bool.into_any())
        }
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Ok(i.into_pyobject(py)?.into_any())
            } else if let Some(u) = n.as_u64() {
                Ok(u.into_pyobject(py)?.into_any())
            } else if let Some(f) = n.as_f64() {
                Ok(f.into_pyobject(py)?.into_any())
            } else {
                Ok(py.None().into_bound(py))
            }
        }
        Value::String(s) => Ok(s.clone().into_pyobject(py)?.into_any()),
        Value::Array(arr) => {
            let list = PyList::empty(py);
            for v in arr {
                list.append(json_to_py(py, v)?)?;
            }
            Ok(list.into_any())
        }
        Value::Object(map) => {
            let dict = PyDict::new(py);
            for (k, v) in map {
                dict.set_item(k, json_to_py(py, v)?)?;
            }
            Ok(dict.into_any())
        }
    }
}

/// Mirror Python `truncate`:
/// - `max_length <= 0` → empty
/// - `len(value) <= max_length` → unchanged
/// - `max_length <= 3` → first max_length chars
/// - else → first (max_length - 3) chars + "..."
pub fn truncate(value: &str, max_length: usize) -> String {
    if max_length == 0 {
        return String::new();
    }
    if value.chars().count() <= max_length {
        return value.to_string();
    }
    if max_length <= 3 {
        return value.chars().take(max_length).collect();
    }
    let take = max_length - 3;
    let mut out: String = value.chars().take(take).collect();
    out.push_str("...");
    out
}

/// Lowercase + collapse all whitespace runs to single space (no leading/trailing).
fn normalize_ws_lower(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut prev_space = true;
    for c in s.chars() {
        let lc = c.to_lowercase().next().unwrap_or(c);
        if lc.is_whitespace() {
            if !prev_space {
                out.push(' ');
                prev_space = true;
            }
        } else {
            out.push(lc);
            prev_space = false;
        }
    }
    if out.ends_with(' ') {
        out.pop();
    }
    out
}

/// Tiny hex encoder (avoids pulling another crate just for 16 bytes).
fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(HEX[(b >> 4) as usize] as char);
        s.push(HEX[(b & 0x0f) as usize] as char);
    }
    s
}

pub fn register(parent: &Bound<PyModule>) -> PyResult<()> {
    parent.add_class::<ObservationRecordCodec>()?;
    Ok(())
}
