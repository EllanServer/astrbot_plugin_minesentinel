//! Lightweight observation priority scoring used before full report analysis.
//!
//! The `observation_priority_score` runs once per SERVER_LOG record inside
//! `RecentWindowBuilder.add`, so keep it small and allocation-light.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use pyo3::IntoPyObjectExt as _;

/// Score records that should survive bounded-memory report sampling.
/// `record` is the Python `ObservationRecord` object. The second argument is
/// kept for ABI compatibility with older Python wrappers and is ignored.
#[pyfunction]
#[pyo3(signature = (record, matcher=None))]
pub fn observation_priority_score(
    _py: Python,
    record: &Bound<PyAny>,
    matcher: Option<Bound<PyAny>>,
) -> PyResult<f64> {
    let _ = matcher;
    let kind: String = record.getattr("kind")?.extract()?;

    match kind.as_str() {
        "SERVER_LOG" => server_log_priority(record),
        _ => Ok(0.0),
    }
}

/// Batch feature extraction for AI prompt sampling.
///
/// Python keeps the final sampling policy; Rust performs repeated string
/// normalization and low-value checks once per record in a single native call.
#[pyfunction]
pub fn ai_sampling_features_batch<'py>(
    py: Python<'py>,
    records: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyList>> {
    let out = PyList::empty(py);
    for item in records.try_iter()? {
        let record = item?;
        out.append(ai_sampling_feature_tuple(py, &record)?)?;
    }
    Ok(out)
}

fn server_log_priority(record: &Bound<PyAny>) -> PyResult<f64> {
    let content: String = record.getattr("content")?.extract()?;
    let tags: Vec<String> = record.getattr("tags")?.extract()?;
    let mut text = String::with_capacity(
        content.len() + tags.iter().map(String::len).sum::<usize>() + tags.len() + 1,
    );
    text.push_str(&content.to_ascii_lowercase());
    text.push(' ');
    for tag in tags {
        text.push_str(&tag.to_ascii_lowercase());
        text.push(' ');
    }
    let mut score = 1.0_f64;
    for marker in [
        "loop_suppressed",
        "fatal",
        "severe",
        "error",
        "exception",
        "failed",
        "timeout",
        "warn",
        "warning",
        "ban",
        "kick",
        "mute",
        "report",
        "spam",
        "grief",
        "cheat",
    ] {
        if text.contains(marker) {
            score += 4.0;
            break;
        }
    }
    Ok(score)
}

fn ai_sampling_feature_tuple<'py>(
    py: Python<'py>,
    record: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let kind = attr_string(record, "kind")?;
    let content = attr_string(record, "content")?;
    let timestamp: i64 = record.getattr("timestamp")?.extract().unwrap_or(0);
    let server_id = attr_string(record, "server_id")?;
    let backend_server = attr_string(record, "backend_server")?;
    let player_name = attr_string(record, "player_name")?;
    let tags: Vec<String> = record.getattr("tags")?.extract().unwrap_or_default();
    let context_obj = record.getattr("context")?;
    let context: Bound<PyDict> = context_obj.extract()?;

    let clean_hash = dict_string(&context, "llmCleanHash")?.trim().to_string();
    let clean_text = dict_string(&context, "llmCleanText")?.trim().to_string();
    let clean_key = if !clean_hash.is_empty() {
        clean_hash
    } else if !clean_text.is_empty() {
        norm(&clean_text)
    } else {
        norm(&content)
    };

    let context_terms = sampling_context_terms(&context)?;
    let text = norm(&format!(
        "{} {} {}",
        content,
        tags.join(" "),
        context_terms.join(" ")
    ));
    let source = if backend_server.is_empty() {
        server_id
    } else {
        backend_server
    };
    let evidence_raw = if player_name.is_empty() {
        format!("[{}] {}", source, content)
    } else {
        format!("[{}] {}: {}", source, player_name, content)
    };
    let evidence_text = norm(evidence_raw.trim());
    let content_text = norm(&content);
    let daily_noise = tags.iter().any(|tag| tag == "daily_noise");
    let anomaly_spike = tags.iter().any(|tag| tag == "anomaly_spike");
    let new_template = tags.iter().any(|tag| tag == "new_template");
    let low_value =
        daily_noise || low_value_ops_classification(&context)? || low_value_sampling_text(&text);
    let quality = dict_i64(&context, "llmQualityScore")?.unwrap_or(50);
    let is_server_log = kind == "SERVER_LOG";

    PyTuple::new(
        py,
        vec![
            clean_key.into_bound_py_any(py)?,
            text.into_bound_py_any(py)?,
            evidence_text.into_bound_py_any(py)?,
            content_text.into_bound_py_any(py)?,
            low_value.into_bound_py_any(py)?,
            quality.into_bound_py_any(py)?,
            timestamp.into_bound_py_any(py)?,
            is_server_log.into_bound_py_any(py)?,
            anomaly_spike.into_bound_py_any(py)?,
            new_template.into_bound_py_any(py)?,
            daily_noise.into_bound_py_any(py)?,
        ],
    )
}

fn sampling_context_terms(context: &Bound<PyDict>) -> PyResult<Vec<String>> {
    let mut terms = Vec::new();
    for key in ["opsHintCode", "opsHintSeverity"] {
        let value = dict_string(context, key)?.trim().to_string();
        if !value.is_empty() {
            terms.push(value);
        }
    }
    if let Some(ops_any) = context.get_item("opsClassification")? {
        if let Ok(ops) = ops_any.extract::<Bound<PyDict>>() {
            for key in ["category", "subtype", "severity"] {
                let value = dict_string(&ops, key)?.trim().to_string();
                if !value.is_empty() {
                    terms.push(value);
                }
            }
            if dict_bool(&ops, "needs_admin")?.unwrap_or(false) {
                terms.push("needs_admin".to_string());
            }
        }
    }
    Ok(terms)
}

fn low_value_ops_classification(context: &Bound<PyDict>) -> PyResult<bool> {
    let Some(ops_any) = context.get_item("opsClassification")? else {
        return Ok(false);
    };
    let Ok(ops) = ops_any.extract::<Bound<PyDict>>() else {
        return Ok(false);
    };
    let category = dict_string(&ops, "category")?;
    if category != "启动与关闭" && category != "指标观察" {
        return Ok(false);
    }
    if dict_bool(&ops, "needs_admin")?.unwrap_or(false) {
        return Ok(false);
    }
    let severity = dict_string(&ops, "severity")?.to_lowercase();
    Ok(matches!(severity.as_str(), "" | "info" | "low"))
}

fn low_value_sampling_text(text: &str) -> bool {
    text.contains("repair of failed migration")
        || text.contains("no failed migration detected")
        || text.contains("unknown or incomplete command")
}

fn attr_string(record: &Bound<PyAny>, name: &str) -> PyResult<String> {
    py_to_string(&record.getattr(name)?)
}

fn dict_string(dict: &Bound<PyDict>, key: &str) -> PyResult<String> {
    if let Some(value) = dict.get_item(key)? {
        return py_to_string(&value);
    }
    Ok(String::new())
}

fn dict_i64(dict: &Bound<PyDict>, key: &str) -> PyResult<Option<i64>> {
    let Some(value) = dict.get_item(key)? else {
        return Ok(None);
    };
    if value.is_none() {
        return Ok(None);
    }
    if let Ok(number) = value.extract::<i64>() {
        return Ok(Some(number));
    }
    Ok(py_to_string(&value)?.trim().parse::<i64>().ok())
}

fn dict_bool(dict: &Bound<PyDict>, key: &str) -> PyResult<Option<bool>> {
    let Some(value) = dict.get_item(key)? else {
        return Ok(None);
    };
    if value.is_none() {
        return Ok(None);
    }
    if let Ok(flag) = value.extract::<bool>() {
        return Ok(Some(flag));
    }
    let text = py_to_string(&value)?.trim().to_ascii_lowercase();
    Ok(match text.as_str() {
        "1" | "true" | "yes" | "on" => Some(true),
        "0" | "false" | "no" | "off" => Some(false),
        _ => None,
    })
}

fn py_to_string(value: &Bound<PyAny>) -> PyResult<String> {
    if value.is_none() {
        return Ok(String::new());
    }
    if let Ok(text) = value.extract::<String>() {
        return Ok(text);
    }
    Ok(value.str()?.to_str()?.to_string())
}

fn norm(value: &str) -> String {
    value
        .to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

pub fn register(parent: &Bound<PyModule>) -> PyResult<()> {
    parent.add_function(wrap_pyfunction!(observation_priority_score, parent)?)?;
    parent.add_function(wrap_pyfunction!(ai_sampling_features_batch, parent)?)?;
    Ok(())
}
