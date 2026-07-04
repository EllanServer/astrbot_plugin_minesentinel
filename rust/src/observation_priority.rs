//! Lightweight observation priority scoring used before full report analysis.
//!
//! The `observation_priority_score` runs once per SERVER_LOG record inside
//! `RecentWindowBuilder.add`, so keep it small and allocation-light.

use pyo3::prelude::*;

/// Score records that should survive bounded-memory report sampling.
/// `record` is the Python `ObservationRecord` object. The second argument is
/// kept for ABI compatibility with older Python wrappers and is ignored.
#[pyfunction]
#[pyo3(signature = (record, matcher=None))]
pub fn observation_priority_score(
    _py: Python,
    record: &Bound<PyAny>,
    _matcher: Option<Bound<PyAny>>,
) -> PyResult<f64> {
    let kind: String = record.getattr("kind")?.extract()?;

    match kind.as_str() {
        "SERVER_LOG" => server_log_priority(record),
        _ => Ok(0.0),
    }
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

pub fn register(parent: &Bound<PyModule>) -> PyResult<()> {
    parent.add_function(wrap_pyfunction!(observation_priority_score, parent)?)?;
    Ok(())
}
