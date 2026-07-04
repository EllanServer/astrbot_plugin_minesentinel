//! Lightweight observation priority scoring used before full report analysis.
//!
//! Rust port of `services/mine_sentinel/observation_priority.py`. The
//! `observation_priority_score` runs once per record (up to 50k records per
//! report window) inside `RecentWindowBuilder.add`, so moving it off the
//! Python interpreter is high-value.

use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3::PyRef;

/// Score records that should survive bounded-memory report sampling.
/// Mirrors `observation_priority.observation_priority_score`.
///
/// `record` is the Python `ObservationRecord` object; `matcher` is an
/// optional Python-side `RuleTermMatcher` (default ruleset). We accept the
/// pre-built matcher so callers can reuse a process-wide cache (mirrors
/// `_DEFAULT_MATCHER`); the Python wrapper handles caching.
#[pyfunction]
#[pyo3(signature = (record, matcher=None))]
pub fn observation_priority_score(
    _py: Python,
    record: &Bound<PyAny>,
    matcher: Option<Bound<PyAny>>,
) -> PyResult<f64> {
    let kind: String = record.getattr("kind")?.extract()?;

    match kind.as_str() {
        "CHAT" => {
            let mut score = 1.0_f64;
            if let Some(m) = matcher.as_ref() {
                let content: String = record.getattr("content")?.extract()?;
                let tags: Vec<String> = record.getattr("tags")?.extract()?;
                let text = if tags.is_empty() {
                    crate::dialogue_terms::normalize_text(&content)
                } else {
                    let tags_len: usize = tags.iter().map(String::len).sum();
                    let mut combined =
                        String::with_capacity(content.len() + tags_len + tags.len() + 1);
                    combined.push_str(&content);
                    combined.push(' ');
                    for (idx, tag) in tags.iter().enumerate() {
                        if idx > 0 {
                            combined.push(' ');
                        }
                        combined.push_str(tag);
                    }
                    crate::dialogue_terms::normalize_text(&combined)
                };
                let matcher_ref =
                    m.extract::<PyRef<'_, crate::dialogue_terms::RuleTermMatcher>>()?;
                score += matcher_ref.chat_priority_score(text.as_str());
            }
            Ok(score)
        }
        "PLUGIN_ERROR" => Ok(5.0),
        "SERVER_SWITCH" => Ok(2.0),
        "SERVER_METRICS" => metrics_priority(record),
        _ => Ok(0.0),
    }
}

/// Mirror `_metrics_priority`: tps/memory based scoring.
fn metrics_priority(record: &Bound<PyAny>) -> PyResult<f64> {
    let metrics_binding = record.getattr("metrics")?;
    let metrics: Bound<PyDict> = metrics_binding.extract()?;
    const TPS_KEYS: &[&str] = &["tps1m", "tps", "tps_1m", "oneMinuteTps", "one_minute_tps"];
    let mut tps: f64 = 20.0;
    let mut found = false;
    for key in TPS_KEYS {
        if let Some(v) = metrics.get_item(*key)? {
            if let Ok(p) = v.extract::<f64>() {
                tps = p;
                found = true;
                break;
            }
        }
    }
    if !found {
        tps = 20.0;
    }
    let memory = memory_usage_percent(&metrics)?;
    let mut score = 0.0_f64;
    if tps < 18.0 {
        score += 3.0;
    }
    if tps < 15.0 {
        score += 2.0;
    }
    if memory >= 90.0 {
        score += 2.0;
    }
    Ok(score)
}

/// Mirror `metrics_context.memory_usage_percent(metrics)`:
/// returns the percentage (0-100) or 0.0 if unknown. Mirrors the full
/// MEMORY_PERCENT_KEYS + MEMORY_PAIR_KEYS tables from metrics_context.py so
/// behavior stays in sync with the Python implementation.
fn memory_usage_percent(metrics: &Bound<PyDict>) -> PyResult<f64> {
    // Percent keys (any one suffices). Python normalizes 0..1 to 0..100.
    const PERCENT_KEYS: &[&str] = &[
        "memoryUsagePercent",
        "memory_usage_percent",
        "memoryPercent",
        "memory_percent",
        "heapUsagePercent",
        "heap_usage_percent",
        "usedMemoryPercent",
        "used_memory_percent",
        "ramUsagePercent",
        "ram_usage_percent",
    ];
    for key in PERCENT_KEYS {
        if let Some(v) = metrics.get_item(key)? {
            if let Ok(p) = v.extract::<f64>() {
                let normalized = if (0.0..=1.0).contains(&p) { p * 100.0 } else { p };
                return Ok(normalized);
            }
        }
    }
    // Pair keys: (used, max). First matching pair wins.
    const PAIR_KEYS: &[(&str, &str)] = &[
        ("memoryUsed", "memoryMax"),
        ("memoryUsedMb", "memoryMaxMb"),
        ("memoryUsedMB", "memoryMaxMB"),
        ("memory_used_mb", "memory_max_mb"),
        ("memory_used", "memory_max"),
        ("heapUsed", "heapMax"),
        ("heapUsedMb", "heapMaxMb"),
        ("heap_used_mb", "heap_max_mb"),
        ("heap_used", "heap_max"),
        ("usedMemory", "maxMemory"),
        ("usedMemoryMb", "maxMemoryMb"),
        ("used_memory_mb", "max_memory_mb"),
        ("used_memory", "max_memory"),
    ];
    for (used_key, max_key) in PAIR_KEYS {
        let used = metrics
            .get_item(*used_key)?
            .and_then(|v| v.extract::<f64>().ok());
        let max = metrics
            .get_item(*max_key)?
            .and_then(|v| v.extract::<f64>().ok());
        if let (Some(u), Some(m)) = (used, max) {
            if m > 0.0 {
                let pct = (u / m) * 100.0;
                return Ok(pct.max(0.0).min(100.0));
            }
        }
    }
    Ok(0.0)
}

pub fn register(parent: &Bound<PyModule>) -> PyResult<()> {
    parent.add_function(wrap_pyfunction!(observation_priority_score, parent)?)?;
    Ok(())
}
