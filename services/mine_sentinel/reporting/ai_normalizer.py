"""Normalize AI report JSON back onto deterministic runtime-log facts."""

from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from typing import Any

from .sections import build_report_sections

logger = logging.getLogger(__name__)

# LLM 产出的自由文本字段最大长度。超过截断，防止 LLM 滥用输出导致
# 报告膨胀或借机注入大段文本。
MAX_FREE_TEXT_CHARS = 500
# 不可信内容中常见的指令注入短语，从 LLM 输出文本中剥离，避免注入
# 文本经报告渲染进入管理员群。这并非完备防御（提示注入根本性缓解在
# prompt 隔离层），但能挡掉最直接的"请执行 X"类输出。
_INJECTION_PATTERNS = [
    re.compile(r"忽略(?:以上|前面|先前).{0,20}指令", re.IGNORECASE),
    re.compile(r"ignore (?:the |all |previous )?(?:above |prior )?instructions?", re.IGNORECASE),
    re.compile(r"系统(?:提示|指令|消息)", re.IGNORECASE),
    re.compile(r"system (?:prompt|instruction|message)", re.IGNORECASE),
    re.compile(r"请(?:立即|马上|现在)?(?:执行|运行|调用).{0,30}", re.IGNORECASE),
    re.compile(r"<evidence>|</evidence>", re.IGNORECASE),
]
# 控制字符（保留换行 \n 与制表符 \t）
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_UNSAFE_ACTION_RE = re.compile(
    r"(?:"
    r"(?:自动|立即|立刻|马上|直接)(?:封禁|踢(?:出|人)?|回滚|删(?:除|库)|关服|停服)"
    r"|(?:执行|运行|调用)\s*(?:rcon|/|rm\b|del\b|drop\b|shutdown\b|stop\b)"
    r"|(?:自动\s*rcon|自动回档)"
    r")",
    re.IGNORECASE,
)
_ACTION_DOMAIN_RULES = (
    (("数据库", "mariadb", "mysql", "jdbc", "sql", "连接池"), ("数据库", "mariadb", "mysql", "jdbc", "sql", "hikari")),
    (("经济", "商店", "余额", "扣款", "补偿"), ("经济", "商店", "余额", "扣款", "quickshop", "vault", "物品", "资产")),
    (("权限", "luckperms", "认证"), ("权限", "luckperms", "认证", "登录", "offline", "代理")),
    (("网络", "代理", "velocity", "bungee", "防火墙"), ("网络", "代理", "连接", "velocity", "bungee", "timeout", "超时")),
    (("内存", "heap", "oom", "gc"), ("内存", "heap", "oom", "gc", "memory")),
    (("磁盘", "disk"), ("磁盘", "disk", "存储", "storage")),
    (("封禁", "踢出", "禁言", "处罚"), ("封禁", "踢", "禁言", "处罚", "违规", "举报", "外挂", "作弊", "刷屏", "广告", "辱骂")),
    (("回滚", "回档"), ("回滚", "回档", "资产", "经济", "物品", "数据丢失", "保存失败")),
)


def sanitize_free_text(value: Any, max_chars: int = MAX_FREE_TEXT_CHARS) -> str:
    """对 LLM 产出的自由文本字段做最小清洗：

    - 非 str 强制 str 化；
    - 剥离控制字符；
    - 移除常见指令注入短语；
    - 截断到 max_chars。

    这一层是纵深防御，不能替代 prompt 侧的 <evidence> 隔离。
    """
    text = str(value or "")
    text = _CONTROL_RE.sub("", text)
    for pattern in _INJECTION_PATTERNS:
        text = pattern.sub("", text)
    if len(text) > max_chars:
        text = text[:max_chars]
    return text.strip()


def parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.debug(f"[MineSentinel] parse_json_object 解析失败: {exc}")
        return None


def repair_json_object_text(text: str) -> str:
    # 先尝试非贪婪匹配（适合单层 JSON），失败再回退贪婪匹配（适合嵌套 JSON）。
    # 仅返回能通过 json.loads 校验的候选，避免截断嵌套对象或吞掉多对象文本。
    for pattern in (r"\{.*?\}", r"\{.*\}"):
        match = re.search(pattern, text, flags=re.S)
        if not match:
            continue
        candidate = match.group(0)
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            continue
    return ""


class AIReportNormalizer:
    """Apply AI wording without letting it mutate deterministic report facts."""

    def normalize_report(
        self,
        data: dict[str, Any],
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        # The heuristic report is the factual ledger. AI output is untrusted
        # presentation data: it must not change counts, severity, scope,
        # evidence, categories, anti-cheat totals, or the issue set itself.
        result = deepcopy(fallback)
        self.normalize_issues(result, fallback, data.get("issues"))
        result["report_sections"] = build_report_sections(result)
        return result

    def normalize_issues(
        self,
        result: dict[str, Any],
        fallback: dict[str, Any],
        ai_issues: Any,
    ):
        fallback_issues_raw = fallback.get("issues", [])
        if not isinstance(fallback_issues_raw, list):
            fallback_issues_raw = []
        fallback_issues = [
            issue for issue in fallback_issues_raw if isinstance(issue, dict)
        ]
        if not isinstance(ai_issues, list):
            result["issues"] = deepcopy(fallback_issues_raw)
            return

        used_fallback_indexes: set[int] = set()
        normalized_issues = [deepcopy(issue) for issue in fallback_issues]
        for issue in ai_issues:
            if not isinstance(issue, dict):
                continue
            fallback_index, fallback_issue = self._match_fallback_issue(
                issue,
                fallback_issues,
                used_fallback_indexes,
            )
            if fallback_index < 0:
                continue
            used_fallback_indexes.add(fallback_index)
            normalized = normalized_issues[fallback_index]
            action = sanitize_free_text(issue.get("suggested_action"))
            if (
                action
                and not _UNSAFE_ACTION_RE.search(action)
                and _action_is_grounded(action, fallback_issue)
            ):
                normalized["suggested_action"] = action

        # Keep every reviewed deterministic issue in its original order. The
        # dedicated issue-review stage is the only component allowed to drop
        # candidates; the prose model cannot silently omit or duplicate them.
        result["issues"] = normalized_issues

    @staticmethod
    def _match_fallback_issue(
        issue: dict[str, Any],
        fallback_issues: list[dict[str, Any]],
        used_indexes: set[int],
    ) -> tuple[int, dict[str, Any]]:
        key = (issue.get("category"), issue.get("tag"))
        incident_index = _as_int(issue.get("incident_index"))
        if incident_index is not None:
            for index, fallback_issue in enumerate(fallback_issues):
                if index in used_indexes:
                    continue
                fallback_key = (fallback_issue.get("category"), fallback_issue.get("tag"))
                if (
                    fallback_key == key
                    and _as_int(fallback_issue.get("incident_index")) == incident_index
                ):
                    return index, fallback_issue

        for index, fallback_issue in enumerate(fallback_issues):
            if index in used_indexes:
                continue
            fallback_key = (fallback_issue.get("category"), fallback_issue.get("tag"))
            if fallback_key == key:
                return index, fallback_issue
        return -1, {}


def _action_is_grounded(action: str, issue: dict[str, Any]) -> bool:
    action_text = str(action or "").lower()
    issue_text = json.dumps(issue, ensure_ascii=False, default=str).lower()
    for action_markers, evidence_markers in _ACTION_DOMAIN_RULES:
        if not any(marker in action_text for marker in action_markers):
            continue
        if not any(marker in issue_text for marker in evidence_markers):
            return False
    return True

def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
