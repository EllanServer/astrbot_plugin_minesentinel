"""AstrBot session target parsing and resolution helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


GROUP_MESSAGE = "GroupMessage"
FRIEND_MESSAGE = "FriendMessage"

QQ_TARGET_TYPES = {
    "group": GROUP_MESSAGE,
    "qq_group": GROUP_MESSAGE,
    "qqgroup": GROUP_MESSAGE,
    "friend": FRIEND_MESSAGE,
    "private": FRIEND_MESSAGE,
    "qq": FRIEND_MESSAGE,
    "user": FRIEND_MESSAGE,
    "qq_user": FRIEND_MESSAGE,
}

_QQ_PLATFORM_NAME_PRIORITY = (
    "aiocqhttp",
    "qq_official",
    "qq_official_webhook",
)
_QQ_PLATFORM_ID_HINTS = (
    "napcat",
    "aiocqhttp",
    "onebot",
    "qq_official",
    "qqofficial",
    "qq-official",
)


def normalize_session_target(target: Any) -> str:
    """Normalize target shorthand without guessing an AstrBot platform id.

    Full UMO strings are preserved. QQ shorthand stays shorthand until runtime,
    where the active AstrBot platform id can be resolved from Context.
    """
    if isinstance(target, dict):
        raw_id = target.get("id") or target.get("target") or target.get("qq")
        target_type = _message_type(
            target.get("type") or target.get("message_type") or target.get("kind")
        )
        platform = str(
            target.get("platform") or target.get("platform_id") or ""
        ).strip()
        if not raw_id:
            return ""
        raw_id_text = str(raw_id).strip()
        if not raw_id_text:
            return ""
        if target_type and platform:
            return f"{platform}:{target_type}:{raw_id_text}"
        if target_type == GROUP_MESSAGE:
            return f"group:{raw_id_text}"
        if target_type == FRIEND_MESSAGE:
            return f"qq:{raw_id_text}"
        return raw_id_text

    text = str(target or "").strip()
    if not text:
        return ""
    if _parse_full_umo(text):
        return text
    shorthand = _parse_shorthand(text)
    if shorthand:
        message_type, session_id = shorthand
        return f"{_prefix_for_message_type(message_type)}:{session_id}"
    if text.isdigit():
        return f"group:{text}"
    return text


def normalize_session_targets(targets: Iterable[Any]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for target in targets:
        session = normalize_session_target(target)
        if session and session not in seen:
            seen.add(session)
            normalized.append(session)
    return normalized


def resolve_astrbot_session(context: Any, target: Any) -> str:
    """Resolve shorthand or adapter-name sessions to AstrBot's active platform id."""
    session = normalize_session_target(target)
    if not session:
        return ""

    parsed = _parse_full_umo(session)
    if parsed:
        platform, message_type, session_id = parsed
        resolved_platform = resolve_platform_id(context, platform)
        if resolved_platform and resolved_platform != platform:
            return f"{resolved_platform}:{message_type}:{session_id}"
        return session

    shorthand = _parse_shorthand(session)
    if not shorthand:
        return session

    message_type, session_id = shorthand
    platform_id = default_qq_platform_id(context)
    if not platform_id:
        return session
    return f"{platform_id}:{message_type}:{session_id}"


def resolve_astrbot_sessions(context: Any, targets: Iterable[Any]) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()
    for target in targets:
        session = resolve_astrbot_session(context, target)
        if session and session not in seen:
            seen.add(session)
            resolved.append(session)
    return resolved


def session_matches(context: Any, configured_session: Any, actual_umo: str) -> bool:
    if not configured_session or not actual_umo:
        return False
    configured = normalize_session_target(configured_session)
    if configured == actual_umo:
        return True
    return resolve_astrbot_session(context, configured) == actual_umo


def resolve_platform_id(context: Any, platform: str) -> str:
    platform = str(platform or "").strip()
    if not platform:
        return ""

    platform_insts = _platform_insts(context)
    for inst in platform_insts:
        meta = _platform_meta(inst)
        if str(getattr(meta, "id", "") or "") == platform:
            return platform

    for inst in platform_insts:
        meta = _platform_meta(inst)
        if str(getattr(meta, "name", "") or "") == platform:
            resolved = str(getattr(meta, "id", "") or "").strip()
            if resolved:
                return resolved

    return platform


def default_qq_platform_id(context: Any) -> str:
    candidates = []
    for inst in _platform_insts(context):
        meta = _platform_meta(inst)
        platform_id = str(getattr(meta, "id", "") or "").strip()
        platform_name = str(getattr(meta, "name", "") or "").strip()
        if not platform_id:
            continue
        rank = _qq_platform_rank(platform_id, platform_name)
        if rank is None:
            continue
        running_bonus = 0 if _platform_running(inst) else 10
        candidates.append((rank + running_bonus, platform_id))

    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _message_type(value: Any) -> str:
    text = str(value or "").strip()
    if text in {GROUP_MESSAGE, FRIEND_MESSAGE}:
        return text
    return QQ_TARGET_TYPES.get(text.lower(), "")


def _parse_full_umo(text: str) -> tuple[str, str, str] | None:
    parts = text.split(":", 2)
    if len(parts) != 3:
        return None
    platform, message_type, session_id = (part.strip() for part in parts)
    if not platform or message_type not in {GROUP_MESSAGE, FRIEND_MESSAGE} or not session_id:
        return None
    return platform, message_type, session_id


def _parse_shorthand(text: str) -> tuple[str, str] | None:
    if ":" not in text:
        return None
    prefix, session_id = text.split(":", 1)
    message_type = QQ_TARGET_TYPES.get(prefix.strip().lower())
    session_id = session_id.strip()
    if not message_type or not session_id:
        return None
    return message_type, session_id


def _prefix_for_message_type(message_type: str) -> str:
    return "group" if message_type == GROUP_MESSAGE else "qq"


def _platform_insts(context: Any) -> list[Any]:
    manager = getattr(context, "platform_manager", None)
    if manager is None:
        return []
    return list(getattr(manager, "platform_insts", []) or [])


def _platform_meta(platform_inst: Any) -> Any:
    meta = getattr(platform_inst, "meta", None)
    if callable(meta):
        try:
            return meta()
        except Exception:
            return None
    return None


def _platform_running(platform_inst: Any) -> bool:
    status = getattr(platform_inst, "status", None)
    value = str(getattr(status, "value", status) or "").lower()
    return value == "running"


def _qq_platform_rank(platform_id: str, platform_name: str) -> int | None:
    name = platform_name.lower()
    pid = platform_id.lower()
    if name in _QQ_PLATFORM_NAME_PRIORITY:
        return _QQ_PLATFORM_NAME_PRIORITY.index(name)
    if any(hint in pid for hint in _QQ_PLATFORM_ID_HINTS):
        return len(_QQ_PLATFORM_NAME_PRIORITY) + 1
    if "qq" in pid and "official" not in name:
        return len(_QQ_PLATFORM_NAME_PRIORITY) + 2
    return None
