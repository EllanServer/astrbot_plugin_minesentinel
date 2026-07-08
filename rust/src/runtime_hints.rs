//! CPU-hot single-line runtime log hints.
//!
//! This module intentionally returns facts, not report decisions. Python still
//! owns category gating, context assembly, and administrator-facing wording.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};
use pyo3::IntoPyObjectExt as _;
use regex::Regex;
use sha1::{Digest, Sha1};
use std::sync::OnceLock;

#[pyfunction]
#[pyo3(signature = (line, max_line_length = 1000))]
pub fn runtime_log_hints<'py>(
    py: Python<'py>,
    line: &str,
    max_line_length: usize,
) -> PyResult<Bound<'py, PyDict>> {
    let truncated = truncate_chars(line, max_line_length);
    let content = sanitize_line(&truncated);
    let mut cleaned = clean_for_llm(&truncated);
    if line.chars().count() > max_line_length {
        cleaned.flags.push("truncated");
    }
    let level = detect_level(&content);
    let fingerprint = fingerprint(&content);
    let out = PyDict::new(py);
    out.set_item("content", &content)?;
    out.set_item("level", level)?;
    out.set_item("fingerprint", fingerprint)?;
    let clean_hash = clean_text_hash(&cleaned.text);
    let quality_score = llm_quality_score(cleaned.redaction_count, &cleaned.flags);
    out.set_item("llmCleanText", cleaned.text)?;
    out.set_item("llmCleanHash", clean_hash)?;
    out.set_item("llmQualityScore", quality_score)?;
    out.set_item("redactionCount", cleaned.redaction_count)?;
    out.set_item("qualityFlags", cleaned.flags)?;
    if let Some((player, message)) = detect_chat_message(&content) {
        let meaningless = detect_meaningless_message(&message);
        out.set_item("chatPlayer", player)?;
        out.set_item("chatMessage", message)?;
        out.set_item("chatMeaningless", meaningless)?;
    }
    if let Some((player, check)) = detect_vulcan_alert(&content) {
        out.set_item("vulcanPlayer", player)?;
        out.set_item("vulcanCheck", check)?;
    }
    if let Some(hint) = detect_ops_hint(&content, level) {
        out.set_item("opsHintCode", hint.code)?;
        out.set_item("opsHintSeverity", hint.severity)?;
        out.set_item("opsHintMarkers", PyList::new(py, hint.markers)?)?;
    }
    Ok(out)
}

#[pyfunction]
#[pyo3(signature = (lines, max_line_length = 1000))]
pub fn runtime_log_hints_batch<'py>(
    py: Python<'py>,
    lines: Vec<String>,
    max_line_length: usize,
) -> PyResult<Bound<'py, PyList>> {
    let out = PyList::empty(py);
    for line in lines {
        out.append(runtime_log_hints(py, &line, max_line_length)?)?;
    }
    Ok(out)
}

#[pyfunction]
pub fn runtime_log_time_parts_batch<'py>(
    py: Python<'py>,
    lines: Vec<String>,
) -> PyResult<Bound<'py, PyList>> {
    let out = PyList::empty(py);
    for line in lines {
        let (date_text, time_text, ms_text) = extract_time_parts(&line);
        out.append(PyTuple::new(
            py,
            [
                date_text.into_bound_py_any(py)?,
                time_text.into_bound_py_any(py)?,
                ms_text.into_bound_py_any(py)?,
            ],
        )?)?;
    }
    Ok(out)
}

pub fn register(parent: &Bound<PyModule>) -> PyResult<()> {
    parent.add_function(wrap_pyfunction!(runtime_log_hints, parent)?)?;
    parent.add_function(wrap_pyfunction!(runtime_log_hints_batch, parent)?)?;
    parent.add_function(wrap_pyfunction!(runtime_log_time_parts_batch, parent)?)?;
    Ok(())
}

fn truncate_chars(value: &str, max_length: usize) -> String {
    if max_length == 0 {
        return String::new();
    }
    if value.chars().count() <= max_length {
        return value.to_string();
    }
    if max_length <= 3 {
        return value.chars().take(max_length).collect();
    }
    let mut out: String = value.chars().take(max_length - 3).collect();
    out.push_str("...");
    out
}

fn sanitize_line(line: &str) -> String {
    let no_ansi = ansi_re().replace_all(line, "");
    let stripped = strip_control_chars(&no_ansi);
    let redacted = ipv4_re().replace_all(&stripped, "<ip>");
    redacted.trim().to_string()
}

fn extract_time_parts(line: &str) -> (Option<String>, Option<String>, Option<String>) {
    let no_ansi = ansi_re().replace_all(line, "");
    let text = no_ansi.trim();
    if let Some(caps) = full_ts_re().captures(text) {
        let date_text = caps.name("date").map(|m| m.as_str().to_string());
        let time_text = caps.name("time").map(|m| m.as_str().to_string());
        let ms_text = caps.name("ms").map(|m| m.as_str().to_string());
        return (date_text, time_text, ms_text);
    }
    if let Some(caps) = time_re().captures(text) {
        let time_text = caps.name("time").map(|m| m.as_str().to_string());
        let ms_text = caps.name("ms").map(|m| m.as_str().to_string());
        return (None, time_text, ms_text);
    }
    (None, None, None)
}

struct CleanedLog {
    text: String,
    redaction_count: usize,
    flags: Vec<&'static str>,
}

fn clean_for_llm(line: &str) -> CleanedLog {
    let no_ansi = ansi_re().replace_all(line, "");
    let mut flags = Vec::new();
    let stripped = strip_control_chars(&no_ansi);
    if stripped != no_ansi {
        flags.push("control_stripped");
    }

    let mut text = stripped;
    let mut redaction_count = 0usize;
    for (regex, replacement, flag) in [
        (url_re(), "<url>", "redacted_url"),
        (email_re(), "<email>", "redacted_email"),
        (uuid_re(), "<uuid>", "redacted_uuid"),
        (ipv4_re(), "<ip>", "redacted_ip"),
        (long_token_re(), "<token>", "redacted_token"),
    ] {
        let count = regex.find_iter(&text).count();
        if count > 0 {
            redaction_count += count;
            if !flags.contains(&flag) {
                flags.push(flag);
            }
            text = regex.replace_all(&text, replacement).to_string();
        }
    }
    let collapsed = collapse_ws(&text);
    if collapsed != text.trim() {
        flags.push("whitespace_collapsed");
    }
    if collapsed.is_empty() {
        flags.push("empty");
    }
    if has_long_repeated_run(&collapsed, 12) {
        flags.push("low_signal_repetition");
    }
    if is_symbol_heavy(&collapsed) {
        flags.push("low_signal_symbols");
    }
    CleanedLog {
        text: collapsed,
        redaction_count,
        flags,
    }
}

fn clean_text_hash(text: &str) -> String {
    let digest = Sha1::digest(text.as_bytes());
    hex_prefix(&digest, 24)
}

fn llm_quality_score(redaction_count: usize, flags: &[&str]) -> i32 {
    let mut score = 100i32;
    score -= (redaction_count.min(8) as i32) * 3;
    for flag in flags {
        score -= match *flag {
            "empty" => 80,
            "low_signal_repetition" => 35,
            "low_signal_symbols" => 35,
            "control_stripped" => 8,
            "truncated" => 8,
            "whitespace_collapsed" => 2,
            _ if flag.starts_with("redacted_") => 0,
            _ => 4,
        };
    }
    score.clamp(0, 100)
}

fn detect_level(line: &str) -> &'static str {
    if let Some(caps) = level_re().captures(line) {
        let level = caps.name("level").map(|m| m.as_str()).unwrap_or("INFO");
        if level.eq_ignore_ascii_case("WARNING") {
            return "WARN";
        }
        return match level.to_ascii_uppercase().as_str() {
            "FATAL" => "FATAL",
            "SEVERE" => "SEVERE",
            "ERROR" => "ERROR",
            "WARN" => "WARN",
            "INFO" => "INFO",
            "DEBUG" => "DEBUG",
            "TRACE" => "TRACE",
            _ => "INFO",
        };
    }
    let lowered = line.to_ascii_lowercase();
    if ["fatal", "severe", "error", "exception"]
        .iter()
        .any(|word| lowered.contains(word))
    {
        return "ERROR";
    }
    if ["warn", "warning", "failed", "timeout"]
        .iter()
        .any(|word| lowered.contains(word))
    {
        return "WARN";
    }
    "INFO"
}

fn fingerprint(line: &str) -> String {
    let mut text = sanitize_line(line).to_lowercase();
    text = prefix_re().replace_all(&text, "").to_string();
    text = full_ts_re().replace_all(&text, "").to_string();
    text = time_re().replace_all(&text, "").to_string();
    text = uuid_re().replace_all(&text, "<uuid>").to_string();
    text = ipv4_re().replace_all(&text, "<ip>").to_string();
    text = hex_re().replace_all(&text, "0x<num>").to_string();
    text = replace_numbers(&text);
    text = collapse_ws(&text);
    if text.is_empty() {
        text = "empty".to_string();
    }
    let digest = Sha1::digest(text.as_bytes());
    hex_prefix(&digest, 24)
}

fn detect_chat_message(content: &str) -> Option<(String, String)> {
    let mut stripped = content.to_string();
    let has_chat_thread = chat_thread_re().is_match(content);
    if has_chat_thread {
        stripped = chat_thread_re().replace_all(content, "").trim().to_string();
    }
    stripped = prefix_re().replace_all(&stripped, "").trim().to_string();
    if let Some(caps) = chat_plugin_re().captures(&stripped) {
        let player = caps.name("player")?.as_str().trim().to_string();
        let message = caps.name("message")?.as_str().trim().to_string();
        if !player.is_empty() && !message.is_empty() {
            return Some((player, message));
        }
    }
    if let Some(caps) = chat_player_prefix_re().captures(&stripped) {
        let player = caps.name("player")?.as_str().trim().to_string();
        let message = caps.name("message")?.as_str().trim().to_string();
        if !player.is_empty() && !message.is_empty() {
            return Some((player, message));
        }
    }
    if has_chat_thread && !stripped.is_empty() {
        return Some((String::new(), stripped));
    }
    None
}

fn detect_vulcan_alert(content: &str) -> Option<(String, String)> {
    let caps = vulcan_player_re().captures(content)?;
    let player = caps.name("player")?.as_str().trim().to_string();
    let check = caps
        .name("check")?
        .as_str()
        .trim_matches(|c: char| c == ' ' || c == ':' || c == ',' || c == '.')
        .to_string();
    Some((player, check))
}

struct OpsHint {
    code: &'static str,
    severity: &'static str,
    markers: Vec<&'static str>,
}

fn detect_ops_hint(content: &str, level: &str) -> Option<OpsHint> {
    let text = content.to_ascii_lowercase();
    let issue_level = matches!(level, "WARN" | "ERROR" | "FATAL" | "SEVERE");
    let issue_text = issue_level || contains_any(&text, OPS_ISSUE_MARKERS);
    if !issue_text {
        return None;
    }
    if let Some(markers) = matched_markers(&text, ECONOMY_SHOP_MARKERS) {
        return Some(OpsHint {
            code: "economy_shop",
            severity: "high",
            markers,
        });
    }
    if !contains_any(&text, DATABASE_TIMEOUT_NEGATIVE_MARKERS) {
        if let Some(markers) = matched_markers(&text, DATABASE_TIMEOUT_MARKERS) {
            return Some(OpsHint {
                code: "database_timeout",
                severity: "high",
                markers,
            });
        }
    }
    if !contains_any(&text, DATABASE_CONNECTION_NEGATIVE_MARKERS) {
        if let Some(markers) = matched_markers(&text, DATABASE_CONNECTION_MARKERS) {
            return Some(OpsHint {
                code: "database_connection",
                severity: "high",
                markers,
            });
        }
    }
    if let Some(markers) = matched_markers(&text, PLUGIN_CONFIG_MARKERS) {
        return Some(OpsHint {
            code: "plugin_config",
            severity: "medium",
            markers,
        });
    }
    if let Some(markers) = matched_markers(&text, PLUGIN_RUNTIME_MARKERS) {
        return Some(OpsHint {
            code: "plugin_runtime",
            severity: "high",
            markers,
        });
    }
    if let Some(markers) = matched_markers(&text, NETWORK_CONNECTION_MARKERS) {
        return Some(OpsHint {
            code: "network_connection",
            severity: "medium",
            markers,
        });
    }
    None
}

fn contains_any(text: &str, markers: &[&'static str]) -> bool {
    markers.iter().any(|marker| text.contains(marker))
}

fn matched_markers(text: &str, markers: &[&'static str]) -> Option<Vec<&'static str>> {
    let hits: Vec<&'static str> = markers
        .iter()
        .copied()
        .filter(|marker| text.contains(marker))
        .take(6)
        .collect();
    if hits.is_empty() {
        None
    } else {
        Some(hits)
    }
}

fn detect_meaningless_message(message: &str) -> bool {
    if message.is_empty() {
        return false;
    }
    let mut previous: Option<char> = None;
    let mut run_len = 0usize;
    for ch in message.chars() {
        if Some(ch) == previous {
            run_len += 1;
        } else {
            previous = Some(ch);
            run_len = 1;
        }
        if run_len >= 8 {
            return true;
        }
    }
    let has_content = message
        .chars()
        .any(|c| c.is_alphanumeric() || ('\u{4e00}'..='\u{9fff}').contains(&c));
    !has_content && message.chars().count() >= 3
}

fn replace_numbers(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    let mut index = 0usize;
    while index < chars.len() {
        let ch = chars[index];
        let starts_number = ch.is_ascii_digit()
            || (ch == '-' && index + 1 < chars.len() && chars[index + 1].is_ascii_digit());
        let prev_blocks =
            index > 0 && (chars[index - 1].is_ascii_alphabetic() || chars[index - 1] == '_');
        if starts_number && !prev_blocks {
            out.push_str("<num>");
            if ch == '-' {
                index += 1;
            }
            while index < chars.len() && chars[index].is_ascii_digit() {
                index += 1;
            }
            if index + 1 < chars.len() && chars[index] == '.' && chars[index + 1].is_ascii_digit() {
                index += 1;
                while index < chars.len() && chars[index].is_ascii_digit() {
                    index += 1;
                }
            }
            continue;
        }
        out.push(ch);
        index += 1;
    }
    out
}

fn collapse_ws(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut prev_space = true;
    for ch in text.chars() {
        if ch.is_whitespace() {
            if !prev_space {
                out.push(' ');
                prev_space = true;
            }
        } else {
            out.push(ch);
            prev_space = false;
        }
    }
    if out.ends_with(' ') {
        out.pop();
    }
    out
}

fn strip_control_chars(text: &str) -> String {
    text.chars()
        .filter(|ch| {
            !((*ch as u32) < 32 || (*ch as u32) == 127) || matches!(*ch, '\t' | '\n' | '\r')
        })
        .collect()
}

fn has_long_repeated_run(value: &str, threshold: usize) -> bool {
    let mut previous: Option<char> = None;
    let mut run_len = 0usize;
    for ch in value.chars() {
        if Some(ch) == previous {
            run_len += 1;
        } else {
            previous = Some(ch);
            run_len = 1;
        }
        if run_len >= threshold {
            return true;
        }
    }
    false
}

fn is_symbol_heavy(value: &str) -> bool {
    let total = value.chars().count();
    if total < 8 {
        return false;
    }
    let meaningful = value
        .chars()
        .filter(|ch| ch.is_alphanumeric() || ('\u{4e00}'..='\u{9fff}').contains(ch))
        .count();
    meaningful * 4 < total
}

fn hex_prefix(bytes: &[u8], len: usize) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(len);
    for byte in bytes {
        if out.len() >= len {
            break;
        }
        out.push(HEX[(byte >> 4) as usize] as char);
        if out.len() >= len {
            break;
        }
        out.push(HEX[(byte & 0x0f) as usize] as char);
    }
    out
}

fn ansi_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\x1b\[[0-9;]*[A-Za-z]").expect("ansi regex"))
}

fn ipv4_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\b(?:\d{1,3}\.){3}\d{1,3}\b").expect("ipv4 regex"))
}

fn url_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r#"(?i)https?://[^\s<>"]+|www\.[^\s<>"]+"#).expect("url regex"))
}

fn email_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b").expect("email regex")
    })
}

fn uuid_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
            .expect("uuid regex")
    })
}

fn long_token_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\b[A-Za-z0-9_-]{32,}\b").expect("long token regex"))
}

fn hex_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?i)\b0x[0-9a-f]+\b").expect("hex regex"))
}

fn prefix_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"(?i)^\[?\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?\]?\s*(?:\[[^\]]+\]\s*)?(?:\[[A-Z]+\]\s*)?",
        )
        .expect("prefix regex")
    })
}

fn full_ts_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"^\[?(?P<date>\d{4}-\d{2}-\d{2})[ T](?P<time>\d{2}:\d{2}:\d{2})(?:[.,](?P<ms>\d{1,6}))?")
            .expect("full timestamp regex")
    })
}

fn time_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"^\[?(?P<time>\d{2}:\d{2}:\d{2})(?:[.,](?P<ms>\d{1,6}))?\]?")
            .expect("time regex")
    })
}

fn level_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?i)(?:^|[\[/\s:])(?P<level>FATAL|SEVERE|ERROR|WARN|WARNING|INFO|DEBUG|TRACE)(?:[\]/\s:]|$)")
            .expect("level regex")
    })
}

fn chat_thread_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?i)\[Async Chat Thread[^\]]*\]\s*:?\s*").expect("chat thread regex")
    })
}

fn chat_player_prefix_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"^\s*<(?P<player>[^>\s]{1,40})>\s*(?P<message>.*)$").expect("chat player regex")
    })
}

fn chat_plugin_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?:\[Not Secure\]\s*)?(?:\[[^\]]{1,30}\]\s*)*(?P<player>[A-Za-z0-9_]{1,16})\s*>>\s*(?P<message>.+)$")
            .expect("chat plugin regex")
    })
}

fn vulcan_player_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"(?i)\[Vulcan\][\]:>\s]*(?P<player>[A-Za-z0-9_]{1,16})\s+failed\s+(?P<check>[A-Za-z]+(?:\s*\([^)]+\))?)")
            .expect("vulcan regex")
    })
}

const OPS_ISSUE_MARKERS: &[&str] = &[
    "error",
    "exception",
    "failed",
    "failure",
    "warn",
    "warning",
    "timeout",
    "timed out",
    "cannot",
    "could not",
    "unable",
];

const ECONOMY_SHOP_MARKERS: &[&str] = &[
    "quickshop",
    "vault",
    "rediseconomy",
    "economy",
    "transaction",
    "balance",
    "shop",
    "auction",
    "market",
];

const DATABASE_TIMEOUT_MARKERS: &[&str] = &[
    "sqltimeoutexception",
    "database timeout",
    "sql timeout",
    "timed out waiting for connection",
    "connection is not available",
    "hikaripool",
    "hikari pool",
];

const DATABASE_TIMEOUT_NEGATIVE_MARKERS: &[&str] = &[
    "added connection",
    "connection added",
    "hikaripool - starting",
    "hikaripool - start completed",
    "hikaripool - shutdown initiated",
    "hikaripool - shutdown completed",
    "hikari pool - starting",
    "hikari pool - start completed",
    "idletimeout is close to or more than maxlifetime",
];

const DATABASE_CONNECTION_MARKERS: &[&str] = &[
    "communications link failure",
    "jdbcconnectionexception",
    "database is locked",
    "too many connections",
    "could not connect to database",
    "failed to connect to database",
    "unknown system variable",
    "mysql",
    "mariadb",
    "sqlite",
    "jdbc",
];

const DATABASE_CONNECTION_NEGATIVE_MARKERS: &[&str] = DATABASE_TIMEOUT_NEGATIVE_MARKERS;

const PLUGIN_CONFIG_MARKERS: &[&str] = &[
    "failed to load config",
    "could not load config",
    "invalid configuration",
    "configuration error",
    "mapping values are not allowed",
    "json parse",
    "yaml",
    "toml",
];

const PLUGIN_RUNTIME_MARKERS: &[&str] = &[
    "could not pass event",
    "eventexception",
    "generated an exception",
    "nullpointerexception",
    "illegalargumentexception",
    "nosuchmethoderror",
    "classnotfoundexception",
    "cannot invoke",
];

const NETWORK_CONNECTION_MARKERS: &[&str] = &[
    "connecttimeoutexception",
    "connection reset",
    "connection refused",
    "connection timed out",
    "read timed out",
    "broken pipe",
    "socketexception",
    "io.netty",
    "netty",
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_carbonchat_message() {
        let parsed = detect_chat_message(
            "[16:34:47] [Async Chat Thread - #1/INFO]: [Not Secure] [生存区] TypeThe0ry >> 1",
        )
        .expect("chat");
        assert_eq!(parsed.0, "TypeThe0ry");
        assert_eq!(parsed.1, "1");
    }

    #[test]
    fn detects_vulcan_alert_but_not_lifecycle() {
        let alert = detect_vulcan_alert("[Vulcan] Steve failed Reach (VL: 5)").expect("alert");
        assert_eq!(alert.0, "Steve");
        assert_eq!(alert.1, "Reach (VL: 5)");
        assert!(detect_vulcan_alert("[Vulcan] Starting Vulcan...").is_none());
    }

    #[test]
    fn detects_ops_hint_for_quickshop_timeout() {
        let hint = detect_ops_hint(
            "[Server thread/WARN]: [QuickShop-Hikari] ConnectTimeoutException: Connect timed out",
            "WARN",
        )
        .expect("ops hint");
        assert_eq!(hint.code, "economy_shop");
        assert_eq!(hint.severity, "high");
        assert!(hint.markers.contains(&"quickshop"));
    }

    #[test]
    fn skips_ops_hint_for_hikari_lifecycle_noise() {
        for line in [
            "[Server thread/WARN]: [CarbonChat] CarbonChat-HikariPool - Starting...",
            "[Server thread/WARN]: [CarbonChat] CarbonChat-HikariPool - Start completed.",
            "[Server thread/WARN]: HikariPool-1 - Added connection com.mysql.cj.jdbc.ConnectionImpl@abc123",
            "[Server thread/WARN]: [HikariConfig] HuskSyncHikariPool - idleTimeout is close to or more than maxLifetime, disabling it.",
        ] {
            assert!(detect_ops_hint(line, "WARN").is_none(), "{line}");
        }
    }

    #[test]
    fn fingerprint_redacts_numbers_and_ips() {
        let a = fingerprint("[16:00:00] [Server thread/ERROR]: failed at 1.2.3.4:25565 id 123");
        let b = fingerprint("[16:00:01] [Server thread/ERROR]: failed at 5.6.7.8:25566 id 456");
        assert_eq!(a, b);
    }

    #[test]
    fn llm_cleaning_redacts_identifiers_and_marks_quality() {
        let cleaned = clean_for_llm(
            "[INFO]: visit https://example.test/a?token=abc user admin@example.test uuid 1070f7bf-1dc0-369a-be53-3d51437c77b3 key abcdefghijklmnopqrstuvwxyzABCDEF",
        );
        assert!(cleaned.text.contains("<url>"));
        assert!(cleaned.text.contains("<email>"));
        assert!(cleaned.text.contains("<uuid>"));
        assert!(cleaned.text.contains("<token>"));
        assert!(cleaned.redaction_count >= 4);
        assert!(cleaned.flags.contains(&"redacted_url"));
        assert!(cleaned.flags.contains(&"redacted_email"));
        assert!(cleaned.flags.contains(&"redacted_uuid"));
        assert!(cleaned.flags.contains(&"redacted_token"));
        assert_eq!(clean_text_hash(&cleaned.text).len(), 24);
        assert!(llm_quality_score(cleaned.redaction_count, &cleaned.flags) < 100);
    }

    #[test]
    fn extracts_full_and_time_only_parts() {
        assert_eq!(
            extract_time_parts("[2024-01-15 14:00:01.123 INFO]: hello"),
            (
                Some("2024-01-15".to_string()),
                Some("14:00:01".to_string()),
                Some("123".to_string())
            )
        );
        assert_eq!(
            extract_time_parts("[14:00:02] [Server thread/INFO]: hello"),
            (None, Some("14:00:02".to_string()), None)
        );
        assert_eq!(extract_time_parts("no timestamp"), (None, None, None));
    }

    #[test]
    fn meaningless_repeat_and_symbols() {
        assert!(detect_meaningless_message("hhhhhhhh"));
        assert!(detect_meaningless_message("!!!???"));
        assert!(!detect_meaningless_message("hello world"));
    }
}
