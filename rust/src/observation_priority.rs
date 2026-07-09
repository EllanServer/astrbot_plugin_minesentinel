//! Lightweight observation priority scoring used before full report analysis.
//!
//! The `observation_priority_score` runs once per SERVER_LOG record inside
//! `RecentWindowBuilder.add`, so keep it small and allocation-light.

use aho_corasick::{AhoCorasick, BuildError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use pyo3::IntoPyObjectExt as _;
use std::collections::HashMap;

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

/// Batch category-key matching for heuristic report classification.
///
/// Python owns category definitions and decision priority. Rust receives the
/// current `(bit, keys)` groups and returns one bitmask per record, avoiding
/// tens of thousands of repeated Python substring scans without duplicating
/// business rules in the extension.
#[pyfunction]
pub fn report_category_features_batch(
    records: &Bound<PyAny>,
    groups: Vec<(u64, Vec<String>)>,
) -> PyResult<Vec<u64>> {
    let matcher = CategoryMatcher::new(&groups)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
    let mut masks = Vec::new();
    for item in records.try_iter()? {
        let record = item?;
        let content = attr_string(&record, "content")?;
        let tags: Vec<String> = record.getattr("tags")?.extract().unwrap_or_default();
        let mut text = String::with_capacity(
            content.len() + tags.iter().map(String::len).sum::<usize>() + tags.len() + 1,
        );
        text.push_str(&content.to_lowercase());
        text.push(' ');
        for tag in tags {
            text.push_str(&tag.to_lowercase());
            text.push(' ');
        }
        masks.push(matcher.mask(&text));
    }
    Ok(masks)
}

struct CategoryMatcher {
    word_bits: HashMap<String, u64>,
    substring_matcher: Option<AhoCorasick>,
    substring_bits: Vec<u64>,
}

impl CategoryMatcher {
    fn new(groups: &[(u64, Vec<String>)]) -> Result<Self, BuildError> {
        let mut word_bits = HashMap::new();
        let mut substring_masks: HashMap<String, u64> = HashMap::new();
        for (bit, keys) in groups {
            for key in keys {
                if is_short_ascii_word(key) {
                    *word_bits.entry(key.clone()).or_insert(0) |= bit;
                } else {
                    *substring_masks.entry(key.clone()).or_insert(0) |= bit;
                }
            }
        }
        let (substring_patterns, substring_bits): (Vec<String>, Vec<u64>) =
            substring_masks.into_iter().unzip();
        let substring_matcher = if substring_patterns.is_empty() {
            None
        } else {
            Some(AhoCorasick::new(&substring_patterns)?)
        };
        Ok(Self {
            word_bits,
            substring_matcher,
            substring_bits,
        })
    }

    fn mask(&self, text: &str) -> u64 {
        let mut mask = 0_u64;
        let bytes = text.as_bytes();
        let mut index = 0;
        while index < bytes.len() {
            while index < bytes.len() && !is_ascii_token_byte(bytes[index]) {
                index += 1;
            }
            let start = index;
            while index < bytes.len() && is_ascii_token_byte(bytes[index]) {
                index += 1;
            }
            if start < index {
                if let Some(bits) = self.word_bits.get(&text[start..index]) {
                    mask |= bits;
                }
            }
        }
        if let Some(matcher) = &self.substring_matcher {
            for matched in matcher.find_overlapping_iter(text) {
                mask |= self.substring_bits[matched.pattern().as_usize()];
            }
        }
        mask
    }
}

fn is_short_ascii_word(value: &str) -> bool {
    !value.is_empty() && value.len() <= 6 && value.bytes().all(|byte| byte.is_ascii_alphabetic())
}

fn is_ascii_token_byte(value: u8) -> bool {
    value.is_ascii_lowercase() || value.is_ascii_digit() || value == b'_'
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
            if dict_bool(&ops, "opsObservation")?.unwrap_or(false) {
                terms.push("ops_observation".to_string());
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
        let severity = dict_string(&ops, "severity")?.to_lowercase();
        let needs_admin = dict_bool(&ops, "needs_admin")?.unwrap_or(false);
        let ops_observation = dict_bool(&ops, "opsObservation")?.unwrap_or(false);
        return Ok(ops_observation
            && !needs_admin
            && matches!(severity.as_str(), "" | "info" | "low"));
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
    parent.add_function(wrap_pyfunction!(report_category_features_batch, parent)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::CategoryMatcher;

    #[test]
    fn short_words_require_ascii_token_boundaries() {
        let matcher =
            CategoryMatcher::new(&[(1, vec!["fly".to_string(), "vl".to_string()])]).unwrap();
        assert_eq!(matcher.mask("player can fly now"), 1);
        assert_eq!(matcher.mask("butterfly effect"), 0);
        assert_eq!(matcher.mask("玩家fly玩家"), 1);
        assert_eq!(matcher.mask("vl=12"), 1);
        assert_eq!(matcher.mask("level=12"), 0);
    }

    #[test]
    fn category_groups_mix_words_phrases_and_unicode() {
        let matcher = CategoryMatcher::new(&[
            (
                1,
                vec!["lag".to_string(), "connection timed out".to_string()],
            ),
            (2, vec!["连接超时".to_string(), "timed out".to_string()]),
        ])
        .unwrap();
        assert_eq!(matcher.mask("server lag detected"), 1);
        assert_eq!(matcher.mask("flag plugin enabled"), 0);
        assert_eq!(matcher.mask("proxy connection timed out"), 3);
        assert_eq!(matcher.mask("后端连接超时"), 2);
    }
}
