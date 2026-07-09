"""Rule-based analysis for Minecraft runtime logs.

分类优先级（先命中先返回）：
    community > chat_review > player_feedback > community_ops
    > complaint > network > plugin > cross_server > moderation > bug > economy > daily

严重级别：
    critical: 崩溃/OOM/watchdog/服务停止/代理大面积不可用
    high:     循环刷屏、多条 ERROR、插件加载失败、多服务器受影响、性能问题重复
              chat_review 出现威胁/隐私泄露/严重骚扰、community_ops 活动事故
    medium:   单条 ERROR、多条 WARN、单次性能警告、权限/登录/网络异常
              单次聊天违规/广告/可疑链接、活动奖励争议
    low:      单条 WARN、日常 join/quit/start/stop、普通玩家建议、普通活动公告

告警策略：
    critical 直告；high 默认 evidence_count >= min_evidence_count；
    medium 仅在多服务器/多后端、证据数较多或命中敏感词时告警；low 不告警。
    chat_review 默认不告警，除非 severity>=high / evidence_count>=5 / 命中威胁/开盒；
    player_feedback 通常不告警；community_ops 仅活动事故/奖励异常/大范围不满才告警。
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from functools import lru_cache
from typing import Any

from ..models import MineSentinelConfig, ObservationRecord
from .common import SEVERITY_RANK, format_locations, location_list
from .sections import build_report_sections

try:
    from mine_sentinel_rs import (
        report_category_features_batch as _rs_report_category_features_batch,
    )
except ImportError:  # pragma: no cover - optional native acceleration
    _rs_report_category_features_batch = None


logger = logging.getLogger(__name__)


# --- 分类关键词表 ---------------------------------------------------------
CATEGORY_KEYS = {
    "daily": (
        "info",
        "started",
        "stopped",
        "done",
        "join",
        "joined",
        "quit",
        "left the game",
        "connected",
        "disconnected",
    ),
    "complaint": (
        "can't keep up",
        "overloaded",
        "lag",
        "lagging",
        "timeout",
        "timed out",
        "tps",
        "mspt",
        "server is overloaded",
        "moved too quickly",
        "moved wrongly",
        "卡顿",
        "延迟",
        "掉线",
        "超时",
        "服务器卡",
    ),
    "bug": (
        "error",
        "exception",
        "failed",
        "failure",
        "fatal",
        "severe",
        "crash",
        "warn",
        "warning",
        "stacktrace",
        "traceback",
        "nullpointerexception",
        "illegalargumentexception",
        "classnotfoundexception",
        "nosuchmethoderror",
        "unsupportedoperationexception",
        "cannot invoke",
        "报错",
        "异常",
        "失败",
        "警告",
        "崩溃",
    ),
    "network": (
        "connection reset",
        "connection refused",
        "connection timed out",
        "read timed out",
        "broken pipe",
        "socket",
        "netty",
        "io.netty",
        "disconnect",
        "disconnected",
        "lost connection",
        "网络",
        "连接失败",
        "连接超时",
        "断开连接",
    ),
    "plugin": (
        "plugin",
        "plugins",
        "enabled plugin",
        "disabling plugin",
        "could not load",
        "could not enable",
        "depend",
        "dependency",
        "softdepend",
        "插件",
        "依赖",
        "加载失败",
        "启用失败",
    ),
    "economy": (
        "economy",
        "vault",
        "shop",
        "money",
        "coin",
        "balance",
        "pay",
        "sell",
        "buy",
        "auction",
        "market",
        "trade",
        "商店",
        "经济",
        "金币",
        "余额",
        "交易",
        "拍卖",
    ),
    "community": (
        "ban",
        "banned",
        "kick",
        "kicked",
        "mute",
        "muted",
        "grief",
        "cheat",
        "cheating",
        "anticheat",
        "anti-cheat",
        "xray",
        "fly",
        "speed",
        "reach",
        "kill aura",
        "killaura",
        "violation",
        "vl",
        # PR10: Vulcan 反作弊插件专用关键词，使 [Vulcan] 日志归入 community
        "vulcan",
        "封禁",
        "禁言",
        "踢出",
        "作弊",
        "外挂",
    ),
    "chat_review": (
        # 仅保留高置信度违规信号词；generic chat/message/said/tell/msg/whisper/pm
        # 会命中所有 [Async Chat Thread] 日志，导致 player_feedback 永远不可达，
        # 且与"辱骂/广告/骚扰/刷屏归 chat_review"的设计意图不符。
        # 真实日志验证：'ad' 子串会误判 dadada/already，'link'/'url' 误判正常技术讨论，
        # '私聊' 是正常常用词——均已移除。
        "swear",
        "profanity",
        "insult",
        "abuse",
        "harassment",
        "threat",
        "toxic",
        "advertising",
        # URL/外链信号（高置信度广告/引流指标）
        "discord.gg",
        "discord.com/invite",
        "http://",
        "https://",
        "www.",
        ".com/",
        ".cn/",
        # 中文辱骂/骚扰/广告信号
        "辱骂",
        "骂人",
        "脏话",
        "骚扰",
        "威胁",
        "开盒",
        "人肉",
        "刷屏",
        "代练",
        "代打",
        "出售账号",
        "卖号",
        "买号",
        "加群",
        "加微信",
        "加qq",
        "举报聊天",
    ),
    "player_feedback": (
        "suggest",
        "suggestion",
        "feedback",
        "idea",
        "request",
        "feature request",
        "proposal",
        "wish",
        "hope",
        "建议",
        "反馈",
        "想法",
        "希望",
        "能不能",
        "可不可以",
        "加个",
        "新增",
        "优化",
        "改进",
    ),
    "community_ops": (
        "event",
        "activity",
        "announcement",
        "notice",
        "reward",
        "vote",
        "poll",
        "rank",
        "season",
        "competition",
        "discord",
        "qq group",
        "community",
        "运营",
        "活动",
        "公告",
        "通知",
        "奖励",
        "投票",
        "赛季",
        "比赛",
        "招募",
        "群",
        "社区",
    ),
    "moderation": (
        "whitelist",
        "permission",
        "permissions",
        "auth",
        "login",
        "logged in",
        "logged out",
        "premium",
        "offline mode",
        "offline/insecure mode",
        "online mode",
        "online-mode",
        "authenticate usernames",
        "uuid",
        "session",
        "白名单",
        "权限",
        "登录",
        "认证",
        "正版验证",
    ),
    "cross_server": (
        "velocity",
        "bungeecord",
        "bungee",
        "proxy",
        "backend",
        "server switch",
        "forwarding",
        "modern forwarding",
        "player forwarding",
        "ip forwarding",
        "connection request",
        "转发",
        "后端",
        "代理",
        "跨服",
    ),
    "suggestion": (),
}

# 分类匹配优先级（先匹配先返回）。daily 永远兜底。
CLASSIFY_PRIORITY = (
    "community",
    "chat_review",
    "player_feedback",
    "community_ops",
    "complaint",
    "network",
    "plugin",
    "cross_server",
    "moderation",
    "bug",
    "economy",
    "daily",
)

# --- Marker 常量 ---------------------------------------------------------
ERROR_MARKERS = (
    "error",
    "exception",
    "failed",
    "failure",
    "fatal",
    "severe",
    "crash",
    "报错",
    "异常",
    "失败",
)
WARN_MARKERS = ("warn", "warning", "警告")
PERFORMANCE_MARKERS = (
    "can't keep up",
    "overloaded",
    "lag",
    "timeout",
    "timed out",
    "tps",
    "mspt",
    "卡顿",
    "延迟",
    "超时",
)
NETWORK_MARKERS = CATEGORY_KEYS["network"]
PLUGIN_MARKERS = CATEGORY_KEYS["plugin"]
COMMUNITY_MARKERS = CATEGORY_KEYS["community"]
CHAT_REVIEW_MARKERS = CATEGORY_KEYS["chat_review"]
PLAYER_FEEDBACK_MARKERS = CATEGORY_KEYS["player_feedback"]
COMMUNITY_OPS_MARKERS = CATEGORY_KEYS["community_ops"]
COUNTER_KEY_BY_CATEGORY = {
    "complaint": "performance",
    "network": "network",
    "plugin": "plugin",
    "chat_review": "chat_review",
    "player_feedback": "player_feedback",
    "community_ops": "community_ops",
}

CATEGORY_FEATURE_BITS = {
    category: 1 << index
    for index, (category, keys) in enumerate(CATEGORY_KEYS.items())
    if keys
}
CATEGORY_FEATURE_GROUPS = tuple(
    (CATEGORY_FEATURE_BITS[category], keys)
    for category, keys in CATEGORY_KEYS.items()
    if keys
)
NATIVE_CATEGORY_BATCH_MIN_RECORDS = 8000
NATIVE_CATEGORY_CANDIDATE_MIN_RECORDS = 1024
# URL/外链信号仅在 chat_message 标签的记录上触发 chat_review。
# 真实日志验证：QuickShop-Hikari 等插件的更新检查日志
# （[QuickShop-Hikari] Update here: https://modrinth.com/...）
# 会被 https:// 信号误判为 chat_review；插件更新日志不是聊天内容。
# 辱骂/代练/交易等中文信号对任何记录都适用（罕见误判）。
CHAT_REVIEW_URL_MARKERS = (
    "discord.gg",
    "discord.com/invite",
    "http://",
    "https://",
    "www.",
    ".com/",
    ".cn/",
)
CHAT_REVIEW_GENERAL_MARKERS = tuple(
    k for k in CHAT_REVIEW_MARKERS if k not in CHAT_REVIEW_URL_MARKERS
)
CRITICAL_MARKERS = (
    "fatal",
    "severe",
    "crash",
    "outofmemoryerror",
    "out of memory",
    "watchdog",
    "server stopped",
    "tick took",
    "can't keep up! is the server overloaded",
    "崩溃",
    "内存溢出",
)
# INFO is useful as timeline context, but a keyword-only INFO line must not inflate
# an incident. These markers retain mislabeled failures emitted at INFO level.
ISSUE_ACTIONABLE_INFO_MARKERS = (
    *CRITICAL_MARKERS,
    *ERROR_MARKERS,
    *WARN_MARKERS,
    "could not",
    "cannot",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "unavailable",
    "denied",
    "invalid",
    "mismatch",
    "无法",
    "拒绝",
    "不可用",
    "不一致",
)
# chat_review 中的敏感词：命中即提级 high 并强制告警
CHAT_SENSITIVE_MARKERS = (
    "threat",
    "dox",
    "privacy",
    "威胁",
    "开盒",
    "人肉",
    "隐私",
)
# community_ops 中的事故关键词：命中即提级 high
COMMUNITY_OPS_SEVERE_MARKERS = (
    "奖励发放异常",
    "活动配置错误",
    "活动事故",
    "大范围玩家不满",
    "玩家不满",
    "事故",
)


# --- 关键词匹配辅助 -----------------------------------------------------
@lru_cache(maxsize=2048)
def _is_word_key(key: str) -> bool:
    """纯 ASCII 单字关键词（如 ad/pm/vl/fly）需要词边界匹配，避免误伤 load/road 等。"""
    return (
        bool(key)
        and key.isascii()
        and key.isalpha()
        and " " not in key
        and len(key) <= 6
    )


@lru_cache(maxsize=256)
def _word_boundary_regex(keys: tuple[str, ...]) -> "re.Pattern[str] | None":
    """把短英文词编译成单个词边界正则。"""
    word_keys = [k for k in keys if _is_word_key(k)]
    if not word_keys:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(k) for k in word_keys) + r")\b")


@lru_cache(maxsize=512)
def _non_word_keys(keys: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(key for key in keys if not _is_word_key(key))


@lru_cache(maxsize=512)
def _word_keys(keys: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(key for key in keys if _is_word_key(key))


_KEY_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_PLUGIN_INVENTORY_ITEM_RE = re.compile(
    r"^-?\s*[a-z0-9_.+-]+(?:\s*\([^)]+\))?"
)


def _keys_match(text: str, keys: tuple[str, ...]) -> bool:
    """关键词匹配：短 ASCII 单词用词边界，其余（短语/中文）用子串。"""
    word_re = _word_boundary_regex(keys)
    if word_re is not None and word_re.search(text) is not None:
        return True
    non_word_keys = _non_word_keys(keys)
    if not non_word_keys:
        return False
    if len(non_word_keys) == 1:
        return non_word_keys[0] in text
    return any(key in text for key in non_word_keys)


def _is_plugin_inventory_text(raw_content: str) -> bool:
    text = raw_content.strip()
    body = text.rsplit("]:", 1)[-1].strip()
    return (
        (
            body.startswith("- ")
            and body.count(", ") >= 8
            and body.count("(") >= 8
            and body.count(")") >= 8
        )
        or (
            "[server thread/info]:" in text
            and body.count(", ") >= 5
            and _PLUGIN_INVENTORY_ITEM_RE.match(body) is not None
        )
    )


def _is_warning_banner_decoration(raw_content: str) -> bool:
    body = raw_content.rsplit("]:", 1)[-1].strip()
    while body.startswith("[") and "]" in body:
        body = body.split("]", 1)[1].strip()
    if not body:
        return True
    ascii_words = re.findall(r"[a-z]+", body.lower())
    if ascii_words and set(ascii_words) <= {"warn", "warning"}:
        return True
    compact = "".join(body.split())
    return not ascii_words and len(compact) >= 2 and len(set(compact)) <= 2


def _is_benign_mechanical_record(raw_content: str, text: str, level: str) -> bool:
    is_info = level in {"info", ""}
    is_warn = level in {"warn", "warning"}
    if is_info and _is_plugin_inventory_text(raw_content):
        return True
    if not is_info and not is_warn:
        return False
    info_markers = (
        "environment: environment[sessionhost=",
        "loaded library ",
        "loading dependency ",
        "loaded dependency ",
        "successfully registered internal expansion",
        "registered channel:",
        "got request to register class ",
        "request_create_item",
        "premium version found",
        "thanks for your support",
        "loading server plugin ",
        "enabling server plugin ",
        "enabling vulcan",
        "starting vulcan",
        "registering vulcan hook",
        "bstats enabled",
        "found. enabling hook",
        "registered mythicmobs listener",
        "registered listener",
        "fake commands is disable",
        "has been successfully loaded",
        "successfully loaded request manager",
        "storing sessions that were preserved before previous shutdown",
        "session store / pubsub factory used:",
        "registering integration ",
        "event action commands into memory",
        "playtime reward records into memory",
        "playtime rewards into memory",
        "loading rewards",
        "rewards have been loaded",
        "total rewards file successfully loaded",
        "start completed",
        "repair of failed migration",
        "no failed migration detected",
        "unknown or incomplete command",
        "paper: using java compression from velocity",
        "paper: using java cipher from velocity",
        "network season sync enabled",
        "synchronized customcrops season",
        "this plugin is running in proxy mode",
        "you have to put the same config.yml",
        "|     proxy mode    |",
        "initializing bungeecord",
        "proxy server detected in the database",
        "(<proxy>/plugins/",
        "server permissions file permissions.yml is empty, ignoring it",
        "registered vault permission & chat hook",
        "wepif: vault detected! using vault for permissions",
        "registered permissions provider:",
        "vaultpermissions found. (loaded: true)",
        "registered extension: permission groups (vault)",
        "[ok] permission manager test",
        "selected permission provider:",
        "log actions is enabled. actions will be logged",
        "lp user ${player} permission",
        "place the premium jar downloaded",
        "found 1 duplicates in database. same name, different uuid",
        "[vault] [permission] superpermissions loaded as backup permission system",
        "loading internal permission managers",
        "初始化经济与权限支持",
        "找到权限插件",
        "no materials whitelisted",
        "permission plugin: luckperms",
        "integrated luckperms for offline permission lookups",
        "enabling anticheatobfuscator",
        "anticheatobfuscator is enabling",
        "anticheatobfuscator enabled",
        "registered fake commands:",
        "issued server command: /redpacket session create",
        "connecting to redis server ",
        "认证管理器已初始化",
        "认证管理器已重载",
        "通知服务已初始化",
        "通信组件初始化完成 (代理模式)",
        "消息转发服务已初始化",
        "代理模式下不启动ws/rest服务器",
        "plugin messaging channel已注册",
        "后端服务器已进入代理模式",
        "代理模式已启用，跳过ws/rest服务器初始化",
        "已向代理端发送认证请求",
        "代理端认证成功",
        "cmi proxy plugin detected",
        "cmi detected velocity network",
    )
    if is_info and "using " in text and " based io" in text:
        return True
    if is_info and "loaded (" in text and " into memory" in text:
        return True
    if is_info and "keepalive response" in text and "out-of-order" in text:
        return True
    if is_info and ("hikaripool" in text or "hikari pool" in text):
        if (
            " - starting" in text
            or " - start completed" in text
            or " - shutdown initiated" in text
            or " - shutdown completed" in text
            or "added connection" in text
        ):
            return True
    if is_info and any(marker in text for marker in info_markers):
        return True
    warn_markers = (
        "****************************",
        "you are running this server as an administrative or root user",
        "you are opening yourself up to potential risks when doing this",
        "madelinemiller.dev/blog/root-minecraft-server",
        "duplicated blocked protocol version",
        "whilst this makes it possible to use velocity",
        "docs.papermc.io/velocity/security",
        "update available",
        "a new version of ",
        "no rcon password set in server.properties, rcon disabled",
    )
    if is_warn and "legacy plugin " in text and " does not specify an api-version" in text:
        return True
    if is_warn and "hikariconfig" in text and "idletimeout is close to or more than maxlifetime" in text:
        return True
    if is_warn and _is_warning_banner_decoration(raw_content):
        return True
    return is_warn and any(marker in text for marker in warn_markers)


CHAT_CATEGORY_PRIORITY = {
    "普通交流": 10,
    "建设协作": 20,
    "管理请求": 30,
    "性能与连接异常": 50,
    "数据与插件异常": 55,
    "经济与物品": 60,
    "违规与安全风险": 70,
}

CHAT_LABEL_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("普通交流", "问候", ("你好", "早安", "早上好", "晚上好", "晚安", "hello", "hi", "哈喽")),
    ("普通交流", "玩笑", ("哈哈", "hhh", "笑死", "绷", "乐", "草")),
    ("普通交流", "组队", ("组队", "一起", "谁来", "带我", "下矿", "打副本", "一起玩")),
    ("普通交流", "路线/坐标交流", ("坐标", "路线", "在哪", "哪里", "怎么去", "新手村", "主城", "/home", "/spawn")),
    ("普通交流", "新手提问", ("新手", "萌新", "怎么", "如何", "谁能带", "不会", "教程")),
    ("普通交流", "规则询问", ("规则", "允许", "可以吗", "能不能", "可不可以")),
    ("普通交流", "交易讨论", ("交易", "换", "卖", "买", "收购")),

    ("建设协作", "建筑规划", ("建筑", "规划", "设计", "地基", "房子", "扩建")),
    ("建设协作", "新手村扩建", ("新手村扩建", "扩建新手村")),
    ("建设协作", "主城建设", ("主城", "城墙", "广场")),
    ("建设协作", "道路/灯光/告示牌", ("道路", "路灯", "灯光", "告示牌", "路牌")),
    ("建设协作", "公共设施", ("公共设施", "公共农场", "公共矿洞", "公共箱子")),
    ("建设协作", "红石机器", ("红石", "机器", "自动机")),
    ("建设协作", "刷怪塔", ("刷怪塔", "刷怪场")),

    (
        "管理请求",
        "管理员求助",
        (
            "管理员在吗",
            "管理在吗",
            "管理员来一下",
            "管理来一下",
            "管理员帮",
            "管理帮",
            "找管理处理",
            "找管理员处理",
            "问管理",
            "找服主",
            "服主在吗",
            "查一下",
            "处理一下",
            "帮忙看",
            "求助",
            "admin help",
        ),
    ),
    ("管理请求", "权限请求", ("给权限", "开权限", "申请权限", "权限请求")),
    ("管理请求", "投诉", ("投诉", "申诉", "不公平")),
    ("管理请求", "举报", ("举报", "疑似外挂", "疑似飞行", "疑似透视", "有人开挂", "有人破坏", "有人偷")),
    ("管理请求", "处罚申诉", ("解封", "申诉", "误封", "处罚")),
    ("管理请求", "证据提交", ("视频", "录像", "录屏", "证据", "截图")),
    ("管理请求", "物品找回", ("找回", "补偿", "返还")),

    ("性能与连接异常", "卡顿/延迟", ("卡顿", "延迟", "好卡", "很卡", "卡死", "lag", "lagging")),
    ("性能与连接异常", "TPS/MSPT 反馈", ("tps", "mspt")),
    ("性能与连接异常", "掉线", ("掉线", "又掉线", "老掉线", "断开连接", "连接断开", "disconnect")),
    ("性能与连接异常", "进服异常", ("进不去", "进服", "连不上", "连接失败")),
    ("性能与连接异常", "跨服异常", ("跨服", "切服", "转服", "后端")),
    ("性能与连接异常", "传送异常", ("传送", "/tp", "tp ", "home 后", "/home 后", "回城失败")),
    ("性能与连接异常", "虚空/卡位置", ("虚空", "卡位置", "卡住", "卡在原地")),
    ("性能与连接异常", "回档", ("回档", "回滚", "rollback")),

    ("数据与插件异常", "背包不同步", ("背包不同步", "背包没同步", "背包不对", "物品不同步")),
    ("数据与插件异常", "血量/状态不同步", ("血量", "状态不同步", "状态没同步", "等级不同步")),
    ("数据与插件异常", "世界切换异常", ("切换世界", "换世界", "世界切换")),
    ("数据与插件异常", "命令异常", ("命令用不了", "指令用不了", "命令异常", "指令异常")),
    ("数据与插件异常", "权限异常", ("没权限", "没有权限", "权限不够", "权限异常")),
    ("数据与插件异常", "插件功能异常", ("插件异常", "插件坏", "插件用不了", "功能坏了", "功能异常")),
    ("数据与插件异常", "区块加载异常", ("区块", "加载不出来", "区块加载")),

    ("经济与物品", "商店异常", ("商店异常", "商店坏", "商店用不了", "商店扣", "shop error", "quickshop")),
    ("经济与物品", "金币异常", ("金币异常", "金币没了", "余额不对", "扣了金币", "money bug", "balance bug")),
    ("经济与物品", "扣款未到账", ("扣钱", "扣款", "扣了", "没到账", "未到账")),
    ("经济与物品", "物品未发放", ("没给物品", "没有给我物品", "物品未发放", "没发", "未发放")),
    ("经济与物品", "交易纠纷", ("交易纠纷", "交易没给", "骗交易", "被骗")),
    ("经济与物品", "公共箱子争议", ("公共箱子", "箱子被动", "公共矿洞")),
    ("经济与物品", "物品丢失", ("物品丢了", "东西没了", "物品丢失")),
    ("经济与物品", "疑似偷窃", ("偷东西", "偷了", "偷窃")),
    ("经济与物品", "复制物品", ("复制物品", "刷物品", "dupe")),
    ("经济与物品", "经济漏洞", ("经济漏洞", "刷钱", "无限金币")),

    ("违规与安全风险", "辱骂", ("辱骂", "骂人", "脏话", "profanity", "insult")),
    ("违规与安全风险", "人身攻击", ("人身攻击", "攻击别人")),
    ("违规与安全风险", "引战", ("引战", "带节奏")),
    ("违规与安全风险", "刷屏", ("刷屏",)),
    ("违规与安全风险", "广告", ("广告", "discord.gg", "加群", "加微信", "加qq")),
    ("违规与安全风险", "诈骗", ("诈骗", "被骗", "骗钱")),
    ("违规与安全风险", "恶意拉人", ("拉人", "拉去别的服")),
    ("违规与安全风险", "外挂举报", ("外挂", "开挂", "cheat", "hack")),
    ("违规与安全风险", "飞行举报", ("飞行", "fly", "飞天")),
    ("违规与安全风险", "透视/Xray", ("透视", "xray", "矿透")),
    ("违规与安全风险", "自动挖矿", ("自动挖矿", "矿机")),
    ("违规与安全风险", "恶意破坏", ("恶意破坏", "破坏建筑", "拆家")),
    ("违规与安全风险", "熊服", ("熊服", "熊孩子")),
    ("违规与安全风险", "利用漏洞", ("利用 bug", "利用bug", "漏洞")),
    ("违规与安全风险", "权限滥用", ("权限滥用", "op滥用")),
    ("违规与安全风险", "威胁服务器", ("威胁服务器", "炸服", "打服")),
)
CHAT_LABEL_CATEGORIES = {
    label: category for category, label, _ in CHAT_LABEL_RULES
}
CHAT_LABEL_CATEGORIES["闲聊"] = "普通交流"

CHAT_CRITICAL_LABELS = {
    "复制物品",
    "经济漏洞",
    "恶意破坏",
    "熊服",
    "利用漏洞",
    "权限滥用",
    "威胁服务器",
}
CHAT_HIGH_LABELS = {
    "掉线",
    "传送异常",
    "虚空/卡位置",
    "回档",
    "背包不同步",
    "血量/状态不同步",
    "世界切换异常",
    "商店异常",
    "金币异常",
    "扣款未到账",
    "物品未发放",
    "物品丢失",
    "疑似偷窃",
    "诈骗",
    "外挂举报",
    "飞行举报",
    "透视/Xray",
    "自动挖矿",
}
FLIGHT_REPORT_INTENT_MARKERS = (
    "举报",
    "疑似",
    "怀疑",
    "开挂",
    "外挂",
    "作弊",
    "违规",
    "警告",
    "停止",
    "别开",
    "抓到",
    "处理",
    "处罚",
    "封禁",
    "管理",
    "report",
    "cheat",
    "hack",
    "ban",
)
CHAT_MEDIUM_CATEGORIES = {"管理请求", "性能与连接异常", "数据与插件异常", "经济与物品", "违规与安全风险"}
CHAT_CHAT_REVIEW_LABELS = {"辱骂", "人身攻击", "引战", "刷屏", "广告", "诈骗", "恶意拉人"}
CHAT_COMMUNITY_LABELS = {
    "外挂举报",
    "飞行举报",
    "透视/Xray",
    "自动挖矿",
    "恶意破坏",
    "熊服",
    "利用漏洞",
    "复制物品",
    "权限滥用",
    "威胁服务器",
    "疑似偷窃",
}

OPS_ISSUE_LEVELS = {"warn", "warning", "error", "severe", "fatal"}
OPS_SEVERITY_RANK = {"info": 0, **SEVERITY_RANK}
ISSUE_CLUSTER_GAP_MS = 5 * 60 * 1000
PLAYER_FEEDBACK_CLUSTER_GAP_MS = 15 * 60 * 1000
OPS_DEFAULT_IMPACT = "需要结合聊天反馈、服务器指标和同时间日志判断影响范围。"
OPS_LOG_RULES: tuple[dict[str, Any], ...] = (
    {
        "category": "启动与关闭",
        "subtype": "服务崩溃/看门狗",
        "markers": (
            "watchdog",
            "server stopped responding",
            "a single server tick took",
            "crash report",
            "crash-reports",
            "exception in server tick loop",
            "崩溃",
        ),
        "severity": "critical",
        "impact": "可能导致服务器不可用或被强制终止。",
        "report_categories": ("bug",),
    },
    {
        "category": "性能与资源",
        "subtype": "内存/GC 风险",
        "markers": (
            "outofmemoryerror",
            "out of memory",
            "java heap space",
            "gc overhead",
            "内存溢出",
        ),
        "severity": "critical",
        "impact": "可能导致卡顿、掉线、数据写入失败或服务崩溃。",
        "report_categories": ("complaint", "bug"),
    },
    {
        "category": "数据库与存储",
        "subtype": "磁盘空间不足",
        "markers": (
            "no space left on device",
            "disk full",
            "not enough space",
            "磁盘空间不足",
            "磁盘满",
        ),
        "severity": "critical",
        "impact": "可能导致存档、玩家数据、经济流水或插件数据写入失败。",
        "report_categories": ("bug",),
    },
    {
        "category": "数据库与存储",
        "subtype": "玩家/世界数据保存失败",
        "markers": (
            "failed to save player data",
            "could not save player data",
            "failed to save chunk",
            "could not save chunk",
            "failed to write player",
            "save failed",
            "保存失败",
        ),
        "severity": "critical",
        "impact": "可能造成玩家资产、背包、位置或世界区块数据丢失。",
        "report_categories": ("bug", "economy"),
    },
    {
        "category": "数据库与存储",
        "subtype": "数据库超时",
        "markers": (
            "sqltimeoutexception",
            "database timeout",
            "sql timeout",
            "timed out waiting for connection",
            "connection is not available",
            "hikaripool",
            "hikari pool",
        ),
        "negative_markers": (
            "added connection",
            "connection added",
            "hikaripool - starting",
            "hikaripool - start completed",
            "hikaripool - shutdown initiated",
            "hikaripool - shutdown completed",
            "hikari pool - starting",
            "hikari pool - start completed",
            " - starting",
            " - start completed",
            " - shutdown initiated",
            " - shutdown completed",
            "no failed migration detected",
            "repair of failed migration",
            "idleTimeout is close to or more than maxLifetime",
        ),
        "severity": "high",
        "impact": "可能影响插件状态、玩家数据、商店交易或经济流水同步。",
        "report_categories": ("bug", "economy"),
    },
    {
        "category": "数据库与存储",
        "subtype": "数据库连接异常",
        "markers": (
            "communications link failure",
            "jdbcconnectionexception",
            "database is locked",
            "too many connections",
            "could not connect to database",
            "failed to connect to database",
            "mysql",
            "mariadb",
            "sqlite",
            "jdbc",
        ),
        "negative_markers": (
            "added connection",
            "connection added",
            "hikaripool - starting",
            "hikaripool - start completed",
            "hikaripool - shutdown initiated",
            "hikaripool - shutdown completed",
            "hikari pool - starting",
            "hikari pool - start completed",
            " - starting",
            " - start completed",
            " - shutdown initiated",
            " - shutdown completed",
            "no failed migration detected",
            "repair of failed migration",
            "idleTimeout is close to or more than maxLifetime",
        ),
        "requires_issue_level": True,
        "severity": "high",
        "impact": "可能导致依赖数据库的插件读写失败或状态不同步。",
        "report_categories": ("bug", "economy"),
    },
    {
        "category": "认证与接入安全",
        "subtype": "离线模式/认证绕过风险",
        "markers": (
            "offline/insecure mode",
            "authenticate usernames",
            "online-mode",
        ),
        "requires_issue_level": True,
        "severity": "high",
        "impact": "服务器离线或未验证用户名时，需确认是否只允许受控代理接入，否则可能存在冒名登录风险。",
        "report_categories": ("moderation",),
    },
    {
        "category": "插件与模组",
        "subtype": "技能/内容定义错误",
        "markers": (
            "configuration error in mechanic",
            "configuration error: particle",
            "mechanic line:",
            "must be a valid mythicmob",
        ),
        "requires_issue_level": True,
        "severity": "medium",
        "impact": "插件技能、实体或内容定义无效，可能导致对应玩法、技能或自定义实体不可用。",
        "report_categories": ("plugin",),
    },
    {
        "category": "插件与模组",
        "subtype": "外部 API 凭据缺失",
        "markers": (
            "empty api key",
            "without api key",
            "missing api key",
            "api key is missing",
            "invalid api key",
        ),
        "requires_issue_level": True,
        "severity": "medium",
        "impact": "插件外部 API 未配置或凭据无效，相关皮肤、资源生成或同步功能可能被禁用。",
        "report_categories": ("plugin",),
    },
    {
        "category": "插件与模组",
        "subtype": "依赖缺失/功能降级",
        "markers": (
            "missing dependency",
            "dependency not found",
            "required dependency",
            "worldguard not found",
        ),
        "requires_issue_level": True,
        "severity": "medium",
        "impact": "插件依赖未安装或未加载，相关模块已关闭或只能以降级模式运行。",
        "report_categories": ("plugin",),
    },
    {
        "category": "插件与模组",
        "subtype": "插件不安全模式",
        "markers": (
            "enabled \"unsafe mode\"",
            "this is not supported! use this at your own risk",
            "websocket server is enabled. this is not recommended for production servers",
        ),
        "requires_issue_level": True,
        "severity": "medium",
        "impact": "插件正在不受支持的模式下运行，升级、重载或异常恢复时可能出现数据或功能风险。",
        "report_categories": ("plugin",),
    },
    {
        "category": "插件与模组",
        "subtype": "外部资源获取失败",
        "markers": (
            "could not fetch skin",
            "unable to fetch skin",
            "failed to find image from url",
            "mineskinrequestexception",
        ),
        "severity": "medium",
        "impact": "插件无法访问外部资源服务，相关皮肤、图片或远程资源功能可能失败。",
        "report_categories": ("plugin",),
    },
    {
        "category": "插件与模组",
        "subtype": "插件更新检查失败",
        "markers": (
            "failed to check for updates",
            "checkforupdate(",
            "checkforupdatesandmetrics(",
        ),
        "severity": "low",
        "impact": "插件自动更新检查未完成，通常不影响当前玩法，仅需在维护窗口人工核对版本。",
        "needs_admin": False,
        "report_categories": ("plugin",),
        "ops_observation": True,
    },
    {
        "category": "插件与模组",
        "subtype": "兼容性/弃用提示",
        "markers": (
            "cannot interact with paper-plugins",
            "running on paper",
            "deprecated mythicmobs api",
            "legacy placeholder detected",
            "notify the plugin author to update",
            "join my discord",
            "create an issue on github",
            "already exists. using alternative",
        ),
        "severity": "low",
        "impact": "插件报告兼容性限制或弃用接口，当前通常可运行，但后续升级前应核对替代方案。",
        "needs_admin": False,
        "report_categories": ("plugin",),
        "ops_observation": True,
    },
    {
        "category": "插件与模组",
        "subtype": "插件加载/启用失败",
        "markers": (
            "could not load plugin",
            "could not enable",
            "failed to load plugin",
            "failed to enable",
            "error occurred while enabling",
            "unknown dependency",
            "missing dependency",
            "invalid plugin.yml",
            "加载失败",
            "启用失败",
            "依赖缺失",
        ),
        "severity": "high",
        "impact": "可能导致相关玩法、权限、经济或同步功能不可用。",
        "report_categories": ("plugin", "bug"),
    },
    {
        "category": "插件与模组",
        "subtype": "配置解析异常",
        "markers": (
            "failed to load config",
            "could not load config",
            "invalid configuration",
            "invalidconfigurationexception",
            "configuration error",
            "mapping values are not allowed",
            "yaml",
            "toml",
            "json parse",
            "jsonsyntaxexception",
            "jsonparseexception",
            "malformed json",
            "jsonreader.syntaxerror",
            "failed to convert json to nbt",
            "配置解析",
            "配置错误",
        ),
        "requires_issue_level": True,
        "severity": "medium",
        "impact": "可能导致插件以默认配置运行、功能关闭或部分指令异常。",
        "report_categories": ("plugin", "bug"),
    },
    {
        "category": "插件与模组",
        "subtype": "本地化/资源键缺失",
        "markers": (
            "no translation for key:",
            "missing translation",
            "translation key",
            "lang file",
            "locale does not exist",
        ),
        "severity": "low",
        "impact": "插件本地化或资源文件缺失，通常不影响核心玩法，但会导致提示文本缺失。",
        "needs_admin": False,
        "report_categories": ("plugin",),
        "ops_observation": True,
    },
    {
        "category": "传送与位置",
        "subtype": "传送/位置异常",
        "markers": (
            "playerteleportevent",
            "entityteleportevent",
            "teleportcause",
            "moved too quickly",
            "moved wrongly",
            "fell out of the world",
            "illegal position",
            "传送",
            "虚空",
            "卡位置",
        ),
        "requires_issue_level": True,
        "severity": "high",
        "impact": "可能导致玩家卡位、掉入虚空、传送失败或跨世界状态不同步。",
        "report_categories": ("complaint", "plugin", "bug"),
    },
    {
        "category": "插件与模组",
        "subtype": "插件运行异常",
        "markers": (
            "could not pass event",
            "eventexception",
            "generated an exception",
            "task",
            "nullpointerexception",
            "illegalargumentexception",
            "nosuchelementexception",
            "nosuchmethoderror",
            "classnotfoundexception",
            "cannot invoke",
        ),
        "requires_issue_level": True,
        "severity": "high",
        "impact": "可能导致对应插件功能失败，并触发玩家侧传送、经济、权限或同步异常。",
        "report_categories": ("bug",),
    },
    {
        "category": "性能与资源",
        "subtype": "主线程卡顿/MSPT 异常",
        "markers": (
            "can't keep up",
            "server is overloaded",
            "tick took",
            "mspt",
            "tps",
            "overloaded",
            "卡顿",
            "延迟",
        ),
        "severity": "medium",
        "impact": "可能造成玩家延迟、传送迟滞、区块加载慢或超时掉线。",
        "report_categories": ("complaint",),
    },
    {
        "category": "性能与资源",
        "subtype": "插件任务调度延迟",
        "markers": (
            "session ticker",
        ),
        "all_markers": (
            "running behind",
        ),
        "severity": "low",
        "impact": "单个插件后台任务出现轻微调度延迟，通常只作为性能旁证，不等同于网络掉线。",
        "needs_admin": False,
        "report_categories": ("complaint",),
        "ops_observation": True,
    },
    {
        "category": "网络与代理",
        "subtype": "网络连接异常",
        "markers": (
            "connection reset",
            "connection refused",
            "connection timed out",
            "sockettimeoutexception",
            "read timed out",
            "broken pipe",
            "socketexception",
            "io.netty",
            "netty",
            "连接超时",
            "连接失败",
            "断开连接",
        ),
        "requires_issue_level": True,
        "severity": "medium",
        "impact": "可能导致玩家掉线、进服失败或代理到后端通信不稳定。",
        "report_categories": ("network",),
    },
    {
        "category": "网络与代理",
        "subtype": "代理/后端转发异常",
        "markers": (
            "velocity",
            "bungeecord",
            "forwarding secret",
            "player-info-forwarding",
            "ip forwarding",
            "backend server",
            "server switch",
            "转发",
            "后端",
            "跨服",
        ),
        "requires_issue_level": True,
        "severity": "high",
        "impact": "可能导致跨服失败、身份转发错误、登录异常或后端不可达。",
        "report_categories": ("cross_server", "network"),
    },
    {
        "category": "玩家会话与登录",
        "subtype": "登录/认证异常",
        "markers": (
            "authentication servers are down",
            "failed to verify username",
            "profile lookup failed",
            "login failed",
            "not authenticated",
            "whitelist",
            "session",
            "认证失败",
            "登录失败",
            "白名单",
        ),
        "requires_issue_level": True,
        "severity": "medium",
        "impact": "可能导致玩家无法进服或身份、UUID、权限状态异常。",
        "report_categories": ("moderation", "network"),
    },
    {
        "category": "世界与区块",
        "subtype": "世界/区块异常",
        "markers": (
            "failed to load chunk",
            "could not load chunk",
            "chunk error",
            "corrupt chunk",
            "regionfile",
            "region file",
            "world save",
            "entity ticking",
            "区块",
            "世界保存",
        ),
        "requires_issue_level": True,
        "severity": "high",
        "impact": "可能导致区块加载失败、实体异常、回档或局部世界数据损坏。",
        "report_categories": ("bug", "complaint"),
    },
    {
        "category": "经济与资产",
        "subtype": "经济/商店异常",
        "markers": (
            "vault",
            "quickshop",
            "economy",
            "transaction",
            "balance",
            "money",
            "shop",
            "auction",
            "market",
            "商店",
            "经济",
            "金币",
            "余额",
        ),
        "requires_issue_level": True,
        "severity": "high",
        "impact": "可能影响扣款、发货、余额、交易流水或玩家资产一致性。",
        "report_categories": ("economy",),
    },
    {
        "category": "经济与资产",
        "subtype": "复制物品/经济漏洞",
        "markers": (
            "dupe",
            "duplication",
            "duplicate item",
            "economy exploit",
            "刷物品",
            "复制物品",
            "经济漏洞",
            "刷钱",
        ),
        "severity": "critical",
        "impact": "可能影响全服经济、公平性和玩家资产，需要立即复核证据。",
        "report_categories": ("economy", "community"),
    },
    {
        "category": "权限与命令",
        "subtype": "权限/命令异常",
        "markers": (
            "permission denied",
            "no permission",
            "lacks permission",
            "unknown command",
            "command exception",
            "issued server command",
            "没有权限",
            "权限不足",
            "命令异常",
            "指令异常",
        ),
        "requires_issue_level": True,
        "severity": "medium",
        "impact": "可能导致玩家或管理员无法执行关键命令，或权限组状态不一致。",
        "report_categories": ("moderation", "plugin"),
    },
    {
        "category": "安全与反作弊",
        "subtype": "反作弊/违规风险",
        "markers": (
            "anticheat",
            "anti-cheat",
            "vulcan",
            "failed fly",
            "failed speed",
            "xray",
            "killaura",
            "cheat",
            "hack",
            "反作弊",
            "外挂",
            "飞行",
            "透视",
        ),
        "requires_issue_level": True,
        "severity": "medium",
        "impact": "需要人工复核玩家、触发规则、VL/证据和上下文，避免误判。",
        "report_categories": ("community",),
    },
    {
        "category": "备份与恢复",
        "subtype": "备份/恢复异常",
        "markers": (
            "backup failed",
            "restore failed",
            "rollback failed",
            "failed to backup",
            "failed to restore",
            "备份失败",
            "恢复失败",
            "回滚失败",
        ),
        "severity": "high",
        "impact": "可能影响事故恢复能力或导致回档、数据恢复不可用。",
        "report_categories": ("bug",),
    },
)


def _lower_marker_tuple(markers: Any) -> tuple[str, ...]:
    return tuple(str(marker).lower() for marker in markers or ())


def _compile_ops_rule(rule: dict[str, Any]) -> dict[str, Any]:
    compiled = dict(rule)
    compiled["markers"] = _lower_marker_tuple(rule.get("markers"))
    compiled["all_markers"] = _lower_marker_tuple(rule.get("all_markers"))
    compiled["negative_markers"] = _lower_marker_tuple(rule.get("negative_markers"))
    compiled["report_categories"] = tuple(rule.get("report_categories") or ())
    return compiled


COMPILED_OPS_LOG_RULES: tuple[dict[str, Any], ...] = tuple(
    _compile_ops_rule(rule) for rule in OPS_LOG_RULES
)
COMPILED_NON_ISSUE_OPS_LOG_RULES: tuple[dict[str, Any], ...] = tuple(
    rule for rule in COMPILED_OPS_LOG_RULES if not rule.get("requires_issue_level")
)


def _ops_rule_marker_union(rules: tuple[dict[str, Any], ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            marker
            for rule in rules
            for marker in (rule.get("markers") or ())
        )
    )


OPS_LOG_RULE_MARKERS: tuple[str, ...] = _ops_rule_marker_union(COMPILED_OPS_LOG_RULES)
NON_ISSUE_OPS_LOG_RULE_MARKERS: tuple[str, ...] = _ops_rule_marker_union(
    COMPILED_NON_ISSUE_OPS_LOG_RULES
)
OPS_CATEGORY_REPORT_MAP: dict[str, tuple[str, ...]] = {
    "启动与关闭": ("bug",),
    "性能与资源": ("complaint",),
    "插件与模组": ("plugin", "bug"),
    "玩家会话与登录": ("network", "moderation"),
    "网络与代理": ("network", "cross_server"),
    "世界与区块": ("bug", "complaint"),
    "传送与位置": ("complaint", "plugin", "bug"),
    "数据库与存储": ("bug", "economy"),
    "经济与资产": ("economy",),
    "权限与命令": ("moderation", "plugin"),
    "认证与接入安全": ("moderation",),
    "安全与反作弊": ("community",),
    "备份与恢复": ("bug",),
    "指标观察": (),
}
OPS_HINT_CLASSIFICATIONS: dict[str, dict[str, Any]] = {
    "economy_shop": {
        "category": "经济与资产",
        "subtype": "经济/商店异常",
        "severity": "high",
        "impact": "可能影响扣款、发货、余额、交易流水或玩家资产一致性。",
        "report_categories": ("economy",),
    },
    "database_timeout": {
        "category": "数据库与存储",
        "subtype": "数据库超时",
        "severity": "high",
        "impact": "可能影响插件状态、玩家数据、商店交易或经济流水同步。",
        "report_categories": ("bug", "economy"),
    },
    "database_connection": {
        "category": "数据库与存储",
        "subtype": "数据库连接异常",
        "severity": "high",
        "impact": "可能导致依赖数据库的插件读写失败或状态不同步。",
        "report_categories": ("bug", "economy"),
    },
    "server_security": {
        "category": "认证与接入安全",
        "subtype": "离线模式/认证绕过风险",
        "severity": "high",
        "impact": "服务器离线或未验证用户名时，需确认是否只允许受控代理接入，否则可能存在冒名登录风险。",
        "report_categories": ("moderation",),
    },
    "plugin_content_definition": {
        "category": "插件与模组",
        "subtype": "技能/内容定义错误",
        "severity": "medium",
        "impact": "插件技能、实体或内容定义无效，可能导致对应玩法、技能或自定义实体不可用。",
        "report_categories": ("plugin",),
    },
    "plugin_api_credentials": {
        "category": "插件与模组",
        "subtype": "外部 API 凭据缺失",
        "severity": "medium",
        "impact": "插件外部 API 未配置或凭据无效，相关皮肤、资源生成或同步功能可能被禁用。",
        "report_categories": ("plugin",),
    },
    "plugin_dependency": {
        "category": "插件与模组",
        "subtype": "依赖缺失/功能降级",
        "severity": "medium",
        "impact": "插件依赖未安装或未加载，相关模块已关闭或只能以降级模式运行。",
        "report_categories": ("plugin",),
    },
    "plugin_unsafe_mode": {
        "category": "插件与模组",
        "subtype": "插件不安全模式",
        "severity": "medium",
        "impact": "插件正在不受支持的模式下运行，升级、重载或异常恢复时可能出现数据或功能风险。",
        "report_categories": ("plugin",),
    },
    "plugin_external_fetch": {
        "category": "插件与模组",
        "subtype": "外部资源获取失败",
        "severity": "medium",
        "impact": "插件无法访问外部资源服务，相关皮肤、图片或远程资源功能可能失败。",
        "report_categories": ("plugin",),
    },
    "plugin_update_check": {
        "category": "插件与模组",
        "subtype": "插件更新检查失败",
        "severity": "low",
        "impact": "插件自动更新检查未完成，通常不影响当前玩法，仅需在维护窗口人工核对版本。",
        "report_categories": ("plugin",),
        "ops_observation": True,
    },
    "plugin_compatibility": {
        "category": "插件与模组",
        "subtype": "兼容性/弃用提示",
        "severity": "low",
        "impact": "插件报告兼容性限制或弃用接口，当前通常可运行，但后续升级前应核对替代方案。",
        "report_categories": ("plugin",),
        "ops_observation": True,
    },
    "plugin_config": {
        "category": "插件与模组",
        "subtype": "配置解析异常",
        "severity": "medium",
        "impact": "可能导致插件以默认配置运行、功能关闭或部分指令异常。",
        "report_categories": ("plugin", "bug"),
    },
    "plugin_translation": {
        "category": "插件与模组",
        "subtype": "本地化/资源键缺失",
        "severity": "low",
        "impact": "插件本地化或资源文件缺失，通常不影响核心玩法，但会导致提示文本缺失。",
        "report_categories": ("plugin",),
        "ops_observation": True,
    },
    "plugin_scheduler_delay": {
        "category": "性能与资源",
        "subtype": "插件任务调度延迟",
        "severity": "low",
        "impact": "单个插件后台任务出现轻微调度延迟，通常只作为性能旁证，不等同于网络掉线。",
        "report_categories": ("complaint",),
        "ops_observation": True,
    },
    "plugin_runtime": {
        "category": "插件与模组",
        "subtype": "插件运行异常",
        "severity": "high",
        "impact": "可能导致对应插件功能失败，并触发玩家侧传送、经济、权限或同步异常。",
        "report_categories": ("bug",),
    },
    "network_connection": {
        "category": "网络与代理",
        "subtype": "网络连接异常",
        "severity": "medium",
        "impact": "可能导致玩家掉线、进服失败或代理到后端通信不稳定。",
        "report_categories": ("network",),
    },
}


def _format_timestamp(ts_ms: int) -> str:
    """把毫秒时间戳格式化为 HH:MM:SS（本地时间），用于 Vulcan 告警呈现。"""
    if not ts_ms:
        return ""
    import time as _time

    return _time.strftime("%H:%M:%S", _time.localtime(ts_ms / 1000))


class HeuristicReportBuilder:
    """Build deterministic fallback facts from SERVER_LOG records."""

    def __init__(self, config: MineSentinelConfig):
        self.config = config
        # 预计算当前生效的分类优先级列表（应用 category_enabled / category_whitelist）。
        # daily 始终兜底，永远保留在末尾。
        self._active_priority: tuple[str, ...] = self._compute_active_priority()
        self._all_categories_active = self._active_priority == CLASSIFY_PRIORITY
        self._reset_runtime_caches()

    def _reset_runtime_caches(self):
        self._record_text_cache: dict[int, str] = {}
        self._record_raw_content_cache: dict[int, str] = {}
        self._chat_classification_cache: dict[int, dict[str, Any]] = {}
        self._ops_classification_cache: dict[int, dict[str, Any]] = {}
        self._benign_mechanical_cache: dict[int, bool] = {}
        self._classification_cache: dict[int, str] = {}
        self._gate_classification_cache: dict[int, str] = {}
        self._category_match_cache: dict[tuple[int, str], bool] = {}
        self._ops_report_category_cache: dict[int, frozenset[str]] = {}
        self._record_word_cache: dict[int, tuple[str, frozenset[str]]] = {}
        self._native_category_feature_cache: dict[int, int] = {}

    @staticmethod
    def _append_unique(values: list[str], value: str):
        value = str(value or "").strip()
        if value and value not in values:
            values.append(value)

    def _annotate_chat_classification(self, record: ObservationRecord) -> dict[str, Any]:
        key = id(record)
        cached = self._chat_classification_cache.get(key)
        if cached is not None:
            return cached
        if "chat_message" not in (record.tags or []):
            self._chat_classification_cache[key] = {}
            return {}
        ctx = record.context or {}
        existing = ctx.get("chatClassification")
        if isinstance(existing, dict) and existing.get("primary_category"):
            self._chat_classification_cache[key] = existing
            return existing
        classification = self._classify_chat_message(record)
        ctx["chatClassification"] = classification
        record.context = ctx
        self._chat_classification_cache[key] = classification
        return classification

    def _classify_chat_message(self, record: ObservationRecord) -> dict[str, Any]:
        ctx = record.context or {}
        message = str(ctx.get("chatMessage") or record.content or "").strip()
        text = message.lower()
        labels: list[str] = []
        categories: list[str] = []

        for category, label, markers in CHAT_LABEL_RULES:
            if _keys_match(text, markers):
                if label == "飞行举报" and not any(
                    marker in text for marker in FLIGHT_REPORT_INTENT_MARKERS
                ):
                    continue
                self._append_unique(labels, label)
                self._append_unique(categories, category)

        if not labels:
            labels.append("闲聊")
            categories.append("普通交流")

        primary_category = max(
            categories,
            key=lambda category: CHAT_CATEGORY_PRIORITY.get(category, 0),
        )
        severity = self._chat_severity(primary_category, labels, text)
        needs_admin = (
            severity in {"medium", "high", "critical"}
            or primary_category in {"管理请求", "性能与连接异常", "数据与插件异常", "经济与物品", "违规与安全风险"}
        )
        return {
            "primary_category": primary_category,
            "labels": labels,
            "severity": severity,
            "needs_admin": needs_admin,
        }

    def _annotate_ops_classification(self, record: ObservationRecord) -> dict[str, Any]:
        key = id(record)
        cached = self._ops_classification_cache.get(key)
        if cached is not None:
            return cached
        if record.kind != "SERVER_LOG" or "chat_message" in (record.tags or []):
            self._ops_classification_cache[key] = {}
            return {}
        ctx = record.context or {}
        existing = ctx.get("opsClassification")
        if isinstance(existing, dict) and existing.get("category"):
            self._ops_classification_cache[key] = existing
            return existing
        classification = self._classify_ops_log(record)
        if classification:
            ctx["opsClassification"] = classification
            record.context = ctx
        self._ops_classification_cache[key] = classification
        return classification

    def _classify_ops_log(self, record: ObservationRecord) -> dict[str, Any]:
        ctx = record.context or {}
        level = self._normalized_level(str(ctx.get("level") or ""))
        text = self._record_text(record)
        raw_content = self._record_raw_content(record)

        if self._is_benign_mechanical_record(record, raw_content, text, level):
            return self._ops_classification(
                level=level or "info",
                category="启动与关闭",
                subtype="机械/生命周期噪声",
                severity="info" if level not in {"warn", "warning"} else "low",
                impact="已识别为常规生命周期、插件更新或配置提示日志，仅作上下文。",
                needs_admin=False,
                report_categories=(),
            )

        hinted_classification = self._ops_classification_from_hint(ctx, level)
        if hinted_classification:
            return hinted_classification

        if "server_metrics" in (record.tags or []) or self._record_keys_match(
            record,
            text,
            ("server_metrics", "metrics", "tps", "mspt", "memory", "heap", "gc", "在线人数"),
        ):
            return self._ops_classification(
                level=level,
                category="指标观察",
                subtype="服务器指标观察",
                severity="info",
                impact="只能作为同时间事故的指标旁证，不单独构成聊天或运行事件。",
                needs_admin=False,
                report_categories=(),
            )

        rules = (
            COMPILED_OPS_LOG_RULES
            if level in OPS_ISSUE_LEVELS
            else COMPILED_NON_ISSUE_OPS_LOG_RULES
        )
        rule_markers = (
            OPS_LOG_RULE_MARKERS
            if level in OPS_ISSUE_LEVELS
            else NON_ISSUE_OPS_LOG_RULE_MARKERS
        )
        if self._record_keys_match(record, text, rule_markers):
            for rule in rules:
                if not self._ops_rule_matches(record, rule, text, level):
                    continue
                severity = str(rule.get("severity") or self._ops_default_severity(level))
                if level == "fatal":
                    severity = "critical"
                elif level == "severe" and OPS_SEVERITY_RANK.get(severity, 0) < OPS_SEVERITY_RANK["high"]:
                    severity = "high"
                needs_admin = bool(
                    rule.get(
                        "needs_admin",
                        OPS_SEVERITY_RANK.get(severity, 0) >= OPS_SEVERITY_RANK["medium"]
                        or level in {"error", "severe", "fatal"},
                    )
                )
                return self._ops_classification(
                    level=level,
                    category=str(rule.get("category") or ""),
                    subtype=str(rule.get("subtype") or ""),
                    severity=severity,
                    impact=str(rule.get("impact") or OPS_DEFAULT_IMPACT),
                    needs_admin=needs_admin,
                    report_categories=tuple(rule.get("report_categories") or ()),
                    ops_observation=bool(rule.get("ops_observation")),
                )

        if level in {"fatal", "severe"}:
            return self._ops_classification(
                level=level,
                category="启动与关闭",
                subtype="严重运行错误",
                severity="critical" if level == "fatal" else "high",
                impact="日志级别显示严重运行错误，但仍需结合堆栈和上下文确认真实事件类型。",
                needs_admin=True,
                report_categories=("bug",),
                ops_first=False,
            )
        if level == "error":
            return self._ops_classification(
                level=level,
                category="插件与模组",
                subtype="未归因运行错误",
                severity="medium",
                impact="ERROR 仅说明该条日志严重度偏高，根因仍需结合堆栈、插件名和玩家反馈确认。",
                needs_admin=True,
                report_categories=("bug",),
                ops_first=False,
            )
        if level in {"warn", "warning"}:
            return self._ops_classification(
                level=level,
                category="插件与模组",
                subtype="未归因运行警告",
                severity="low",
                impact="WARN 是候选线索；未命中具体运维类型时只作上下文观察。",
                needs_admin=False,
                report_categories=("bug",),
                ops_first=False,
            )
        return self._ops_classification(
            level=level or "info",
            category="启动与关闭",
            subtype="常规运行信息",
            severity="info",
            impact="INFO 日志默认仅作为上下文，不单独构成事件。",
            needs_admin=False,
            report_categories=(),
            ops_first=False,
        )

    def _ops_classification_from_hint(self, ctx: dict[str, Any], level: str) -> dict[str, Any]:
        code = str(ctx.get("opsHintCode") or "").strip()
        if not code:
            return {}
        spec = OPS_HINT_CLASSIFICATIONS.get(code)
        if not spec:
            return {}
        severity = str(ctx.get("opsHintSeverity") or spec.get("severity") or "medium").lower()
        if level == "fatal":
            severity = "critical"
        elif level == "severe" and OPS_SEVERITY_RANK.get(severity, 0) < OPS_SEVERITY_RANK["high"]:
            severity = "high"
        needs_admin = OPS_SEVERITY_RANK.get(severity, 0) >= OPS_SEVERITY_RANK["medium"] or level in {
            "error",
            "severe",
            "fatal",
        }
        return self._ops_classification(
            level=level or "info",
            category=str(spec.get("category") or ""),
            subtype=str(spec.get("subtype") or ""),
            severity=severity,
            impact=str(spec.get("impact") or OPS_DEFAULT_IMPACT),
            needs_admin=needs_admin,
            report_categories=tuple(spec.get("report_categories") or ()),
            ops_observation=bool(spec.get("ops_observation")),
        )

    def _ops_rule_matches(
        self,
        record: ObservationRecord,
        rule: dict[str, Any],
        text: str,
        level: str,
    ) -> bool:
        if rule.get("requires_issue_level") and level not in OPS_ISSUE_LEVELS:
            return False
        negative_markers = rule.get("negative_markers") or ()
        if negative_markers and any(marker in text for marker in negative_markers):
            return False
        all_markers = rule.get("all_markers") or ()
        if all_markers and not all(marker in text for marker in all_markers):
            return False
        markers = rule.get("markers") or ()
        return bool(markers) and self._record_keys_match(record, text, markers)

    @staticmethod
    def _ops_classification(
        *,
        level: str,
        category: str,
        subtype: str,
        severity: str,
        impact: str,
        needs_admin: bool,
        report_categories: tuple[str, ...],
        ops_first: bool = True,
        ops_observation: bool = False,
    ) -> dict[str, Any]:
        data = {
            "level": (level or "info").upper(),
            "category": category,
            "subtype": subtype,
            "severity": severity,
            "impact": impact,
            "needs_admin": needs_admin,
            "report_categories": list(report_categories),
            "opsFirst": ops_first,
        }
        if ops_observation:
            data["opsObservation"] = True
        return data

    @staticmethod
    def _ops_default_severity(level: str) -> str:
        if level == "fatal":
            return "critical"
        if level == "severe":
            return "high"
        if level == "error":
            return "medium"
        if level in {"warn", "warning"}:
            return "low"
        return "info"

    @staticmethod
    def _normalized_level(level: str) -> str:
        normalized = str(level or "").strip().lower()
        if normalized == "warning":
            return "warn"
        return normalized

    @staticmethod
    def _chat_severity(primary_category: str, labels: list[str], text: str) -> str:
        label_set = set(labels)
        if label_set & CHAT_CRITICAL_LABELS:
            return "critical"
        if (
            ("全服" in text or "所有人" in text or "大规模" in text)
            and label_set & {"掉线", "卡顿/延迟", "进服异常", "跨服异常"}
        ):
            return "critical"
        if label_set & CHAT_HIGH_LABELS:
            return "high"
        if primary_category in CHAT_MEDIUM_CATEGORIES:
            return "medium"
        if primary_category == "建设协作" or labels != ["闲聊"]:
            return "low"
        return "info"

    @staticmethod
    def _chat_category_matches(chat_info: dict[str, Any], category: str) -> bool:
        if not chat_info:
            return False
        primary = str(chat_info.get("primary_category") or "")
        labels = set(str(label) for label in (chat_info.get("labels") or []))
        if category == "chat_review":
            # Single-message chat-review labels are evidence hints only. Actual
            # chat_review issues still require player-level behavior tags
            # (chat_flood/chat_abuse) so one-off links/jokes are not overjudged.
            return False
        if category == "community":
            return bool(labels & CHAT_COMMUNITY_LABELS)
        if category == "community_ops":
            return False
        if category == "player_feedback":
            return primary == "管理请求"
        if category == "economy":
            return primary == "经济与物品" and not (labels & CHAT_COMMUNITY_LABELS)
        if category == "network":
            return bool(labels & {"掉线", "进服异常"})
        if category == "cross_server":
            return "跨服异常" in labels
        if category == "complaint":
            return bool(labels & {"卡顿/延迟", "TPS/MSPT 反馈", "传送异常", "虚空/卡位置", "回档"})
        if category == "moderation":
            return "权限异常" in labels
        if category == "plugin":
            return "插件功能异常" in labels
        if category == "bug":
            return bool(labels & {"背包不同步", "血量/状态不同步", "世界切换异常", "命令异常", "区块加载异常"})
        return False

    @staticmethod
    def _ops_category_matches_uncached(ops_info: dict[str, Any], category: str) -> bool:
        if not ops_info:
            return False
        if str(ops_info.get("category") or "") == "指标观察":
            return False
        severity = str(ops_info.get("severity") or "info").lower()
        needs_admin = bool(ops_info.get("needs_admin"))
        ops_observation = bool(ops_info.get("opsObservation"))
        if (
            not needs_admin
            and not ops_observation
            and OPS_SEVERITY_RANK.get(severity, 0) < OPS_SEVERITY_RANK["medium"]
        ):
            return False
        report_categories = tuple(
            str(item)
            for item in (ops_info.get("report_categories") or ())
            if str(item)
        )
        if not report_categories:
            report_categories = OPS_CATEGORY_REPORT_MAP.get(
                str(ops_info.get("category") or ""),
                (),
            )
        return category in report_categories

    def _ops_report_categories(self, ops_info: dict[str, Any]) -> frozenset[str]:
        cache_key = id(ops_info)
        cached = self._ops_report_category_cache.get(cache_key)
        if cached is not None:
            return cached
        report_categories: frozenset[str] = frozenset()
        if ops_info:
            ops_category = str(ops_info.get("category") or "")
            if ops_category in OPS_CATEGORY_REPORT_MAP and not OPS_CATEGORY_REPORT_MAP[ops_category]:
                self._ops_report_category_cache[cache_key] = report_categories
                return report_categories
        if ops_info and str(ops_info.get("category") or "") != "指标观察":
            severity = str(ops_info.get("severity") or "info").lower()
            needs_admin = bool(ops_info.get("needs_admin"))
            ops_observation = bool(ops_info.get("opsObservation"))
            if (
                needs_admin
                or ops_observation
                or OPS_SEVERITY_RANK.get(severity, 0) >= OPS_SEVERITY_RANK["medium"]
            ):
                categories = tuple(
                    str(item)
                    for item in (ops_info.get("report_categories") or ())
                    if str(item)
                )
                if not categories:
                    categories = OPS_CATEGORY_REPORT_MAP.get(
                        str(ops_info.get("category") or ""),
                        (),
                    )
                report_categories = frozenset(categories)
        self._ops_report_category_cache[cache_key] = report_categories
        return report_categories

    def _ops_category_matches(self, ops_info: dict[str, Any], category: str) -> bool:
        if not ops_info:
            return False
        return category in self._ops_report_categories(ops_info)

    def _first_ops_report_category(
        self,
        ops_info: dict[str, Any],
        priority: tuple[str, ...],
    ) -> str:
        if not ops_info or not bool(ops_info.get("opsFirst", True)):
            return ""
        report_categories = self._ops_report_categories(ops_info)
        if not report_categories:
            return ""
        return next(
            (
                category
                for category in priority
                if category != "daily" and category in report_categories
            ),
            "",
        )

    def _compute_active_priority(self) -> tuple[str, ...]:
        """根据 runtime_log.category_enabled / category_whitelist 计算生效分类。"""
        runtime = self.config.runtime_log
        whitelist = set(runtime.category_whitelist or ())
        disabled = set(
            cat for cat, enabled in (runtime.category_enabled or {}).items()
            if enabled is False
        )
        active = [
            cat
            for cat in CLASSIFY_PRIORITY
            if cat != "daily"
            and cat not in disabled
            and (not whitelist or cat in whitelist)
        ]
        # daily 永远兜底
        active.append("daily")
        return tuple(active)

    def _category_active(self, category: str) -> bool:
        """Return whether a category-specific analysis surface is enabled."""
        if category == "daily":
            return True
        return category in self._active_priority

    def filter_records_for_report(
        self,
        records: list[ObservationRecord],
    ) -> list[ObservationRecord]:
        """Return SERVER_LOG records that pass category gating."""
        self._reset_runtime_caches()
        log_records, _, _ = self._prepare_log_records(records)
        return log_records

    def record_allowed_for_report(self, record: ObservationRecord) -> bool:
        """Return whether one record is admitted by category gating."""
        if record.kind != "SERVER_LOG":
            return False
        return self._category_active(self._classify_for_gate(record))

    def _prepare_log_records(
        self,
        records: list[ObservationRecord],
    ) -> tuple[
        list[ObservationRecord],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        log_records = [record for record in records if record.kind == "SERVER_LOG"]

        # Aggregate chat-review behavior is a category-specific detector. Run it
        # only while that category is admitted, then gate records before any
        # report surfaces, counters, or AI evidence are built.
        if self._category_active("chat_review"):
            flood_events = self._detect_and_tag_floods(log_records)
            abuse_events = self._detect_and_tag_abuse(log_records)
        else:
            flood_events = []
            abuse_events = []

        self._prepare_native_category_features(log_records)

        if self._all_categories_active:
            admitted = log_records
        else:
            category_active = self._category_active
            classify_for_gate = self._classify_for_gate
            admitted = [
                record
                for record in log_records
                if category_active(classify_for_gate(record))
            ]
        return admitted, flood_events, abuse_events

    def _prepare_native_category_features(
        self,
        records: list[ObservationRecord],
    ) -> None:
        if (
            len(records) < NATIVE_CATEGORY_BATCH_MIN_RECORDS
            or _rs_report_category_features_batch is None
        ):
            return
        candidates = [
            record
            for record in records
            if self._needs_native_category_features(record)
        ]
        candidate_min = min(
            NATIVE_CATEGORY_CANDIDATE_MIN_RECORDS,
            NATIVE_CATEGORY_BATCH_MIN_RECORDS,
        )
        if len(candidates) < candidate_min:
            return
        try:
            masks = _rs_report_category_features_batch(
                candidates,
                CATEGORY_FEATURE_GROUPS,
            )
            if len(masks) != len(candidates):
                return
            self._native_category_feature_cache = {
                id(record): int(mask)
                for record, mask in zip(candidates, masks, strict=True)
            }
        except Exception as exc:
            logger.debug(
                "[MineSentinel] Rust report category features failed; "
                "falling back to Python matching: %s",
                exc,
            )

    @staticmethod
    def _needs_native_category_features(record: ObservationRecord) -> bool:
        tags = record.tags or ()
        if (
            "daily_noise" in tags
            or "anticheat_vulcan" in tags
            or "chat_flood" in tags
            or "chat_abuse" in tags
        ):
            return False
        ctx = record.context or {}
        if str(ctx.get("logLineKind") or "") in {
            "stacktrace_frame",
            "diagnostic_detail",
        }:
            return False
        hint_code = str(ctx.get("opsHintCode") or "")
        if hint_code and hint_code in OPS_HINT_CLASSIFICATIONS:
            return False
        ops = ctx.get("opsClassification")
        if not isinstance(ops, dict):
            return True
        severity = str(ops.get("severity") or "info").lower()
        report_categories = tuple(ops.get("report_categories") or ())
        if not report_categories:
            report_categories = OPS_CATEGORY_REPORT_MAP.get(
                str(ops.get("category") or ""),
                (),
            )
        return not (
            report_categories
            or bool(ops.get("needs_admin"))
            or bool(ops.get("opsObservation"))
            or OPS_SEVERITY_RANK.get(severity, 0) >= OPS_SEVERITY_RANK["medium"]
        )

    def _classify_for_gate(self, record: ObservationRecord) -> str:
        """Classify with the full priority list before category filtering."""
        cache_key = id(record)
        cached = self._gate_classification_cache.get(cache_key)
        if cached is not None:
            return cached
        if "daily_noise" in record.tags:
            self._gate_classification_cache[cache_key] = "daily"
            return "daily"
        if "chat_flood" in record.tags or "chat_abuse" in record.tags:
            self._annotate_chat_classification(record)
            self._gate_classification_cache[cache_key] = "chat_review"
            return "chat_review"

        text = self._record_text(record)
        raw_content = self._record_raw_content(record)
        level = str((record.context or {}).get("level") or "").lower()
        if self._is_benign_mechanical_record(record, raw_content, text, level):
            self._annotate_ops_classification(record)
            self._gate_classification_cache[cache_key] = "daily"
            return "daily"

        chat_info = self._annotate_chat_classification(record)
        ops_info = self._annotate_ops_classification(record)
        ops_category = self._first_ops_report_category(ops_info, CLASSIFY_PRIORITY)
        if ops_category:
            self._gate_classification_cache[cache_key] = ops_category
            return ops_category
        ops_report_categories = (
            self._ops_report_categories(ops_info) if ops_info else frozenset()
        )
        for category in CLASSIFY_PRIORITY:
            if category == "daily":
                continue
            if category == "chat_review":
                if self._chat_category_matches(chat_info, "chat_review") or self._detect_chat_review_hits(record):
                    self._gate_classification_cache[cache_key] = "chat_review"
                    return "chat_review"
                continue
            if self._chat_category_matches(chat_info, category):
                self._gate_classification_cache[cache_key] = category
                return category
            if category in ops_report_categories:
                self._gate_classification_cache[cache_key] = category
                return category
            if self._category_matches(
                record,
                category,
                text,
                raw_content=raw_content,
                level=level,
                assume_not_benign=True,
            ):
                self._gate_classification_cache[cache_key] = category
                return category

        # Chat records are their own summary surface. If chat_review is closed,
        # ordinary chat should not leak into daily summaries or AI samples.
        if "chat_message" in (record.tags or []):
            self._gate_classification_cache[cache_key] = "chat_review"
            return "chat_review"
        self._gate_classification_cache[cache_key] = "daily"
        return "daily"

    def build(
        self,
        records: list[ObservationRecord],
        window_minutes: int,
        server_id: str | None = None,
    ) -> dict[str, Any]:
        self._reset_runtime_caches()
        log_records, flood_events, abuse_events = self._prepare_log_records(records)
        servers = sorted({record.server_id for record in log_records if record.server_id})
        server_names = sorted(
            {
                record.server_name or record.server_id
                for record in log_records
                if record.server_name or record.server_id
            }
        )
        proxy_ids = sorted({record.proxy_id for record in log_records if record.proxy_id})
        categories: dict[str, list[str]] = {key: [] for key in CATEGORY_KEYS}
        buckets: dict[tuple[str, str], list[ObservationRecord]] = defaultdict(list)
        counters: dict[str, int] = {
            "error": 0,
            "warn": 0,
            "performance": 0,
            "network": 0,
            "plugin": 0,
            "chat_review": 0,
            "player_feedback": 0,
            "community_ops": 0,
            "loop_suppressed": 0,
            "affected_servers": 0,
            "affected_backends": 0,
        }

        for record in log_records:
            category = self.classify(record)
            tag = self.tag(record)
            buckets[(category, tag)].append(record)
            level = self._normalized_level(
                str((record.context or {}).get("level") or "")
            )
            record_tags = record.tags or ()
            if level in {"error", "fatal", "severe"} or (
                not level and "error" in record_tags
            ):
                counters["error"] += 1
            elif (
                level == "warn"
                or (
                    not level
                    and ("warn" in record_tags or "warning" in record_tags)
                )
            ):
                counters["warn"] += 1
            counter_key = COUNTER_KEY_BY_CATEGORY.get(category)
            if counter_key:
                counters[counter_key] += 1
            if "loop_suppressed" in record_tags:
                counters["loop_suppressed"] += int(
                    (record.context or {}).get("loopSuppressed") or 0
                )

        for (category, tag), group in buckets.items():
            categories.setdefault(category, [])
            categories[category].append(self._category_line(tag, group))

        issue_buckets: list[tuple[tuple[str, str], list[ObservationRecord]]] = []
        for key, group in buckets.items():
            category, _ = key
            issue_evidence = [
                record
                for record in group
                if self._is_issue_evidence_candidate(record, category)
            ]
            for partition in self._partition_issue_evidence(issue_evidence, category):
                for segment in self._split_issue_group(
                    partition,
                    cluster_gap_ms=(
                        PLAYER_FEEDBACK_CLUSTER_GAP_MS
                        if category == "player_feedback"
                        else ISSUE_CLUSTER_GAP_MS
                    ),
                ):
                    issue_buckets.append((key, segment))

        chat_topics = self._build_chat_topics(log_records, flood_events, abuse_events)
        vulcan_alerts = self._build_vulcan_alerts(log_records)

        issues = []
        max_severity_rank = 0
        for (category, tag), group in sorted(
            issue_buckets, key=lambda item: len(item[1]), reverse=True
        ):
            severity = self._severity(group)
            chat_infos = [
                (record.context or {}).get("chatClassification")
                for record in group
                if isinstance((record.context or {}).get("chatClassification"), dict)
            ]
            ops_infos = [
                (record.context or {}).get("opsClassification")
                for record in group
                if isinstance((record.context or {}).get("opsClassification"), dict)
            ]
            chat_labels: list[str] = []
            chat_primary_categories: list[str] = []
            chat_severity = ""
            ops_categories: list[str] = []
            ops_subtypes: list[str] = []
            ops_subtype_counts: Counter[str] = Counter()
            ops_impacts: list[str] = []
            ops_severity = ""
            for info in chat_infos:
                for label in info.get("labels") or []:
                    label = str(label)
                    label_category = CHAT_LABEL_CATEGORIES.get(label, "")
                    if label_category in {"普通交流", "建设协作"}:
                        continue
                    if category == "chat_review" and label not in CHAT_CHAT_REVIEW_LABELS:
                        continue
                    self._append_unique(chat_labels, label)
                self._append_unique(
                    chat_primary_categories,
                    str(info.get("primary_category") or ""),
                )
                info_severity = str(info.get("severity") or "")
                if SEVERITY_RANK.get(info_severity, 0) > SEVERITY_RANK.get(chat_severity, 0):
                    chat_severity = info_severity
            for info in ops_infos:
                info_severity = str(info.get("severity") or "")
                if (
                    str(info.get("category") or "") == "指标观察"
                    or (
                        not bool(info.get("needs_admin"))
                        and OPS_SEVERITY_RANK.get(info_severity, 0) < OPS_SEVERITY_RANK["medium"]
                    )
                ):
                    continue
                self._append_unique(ops_categories, str(info.get("category") or ""))
                subtype = str(info.get("subtype") or "")
                self._append_unique(ops_subtypes, subtype)
                if subtype:
                    ops_subtype_counts[subtype] += 1
                self._append_unique(ops_impacts, str(info.get("impact") or ""))
                if OPS_SEVERITY_RANK.get(info_severity, 0) > OPS_SEVERITY_RANK.get(ops_severity, 0):
                    ops_severity = info_severity
            ops_subtypes.sort(key=lambda subtype: -ops_subtype_counts[subtype])
            if SEVERITY_RANK.get(chat_severity, 0) > SEVERITY_RANK.get(severity, 0):
                severity = chat_severity
            if SEVERITY_RANK.get(ops_severity, 0) > SEVERITY_RANK.get(severity, 0):
                severity = ops_severity
            if severity == "low" and category in {
                "chat_review",
                "community",
                "community_ops",
                "moderation",
            }:
                severity = "medium"
            if category == "daily" or severity == "low":
                continue
            severity_rank = SEVERITY_RANK.get(severity, 0)
            if severity_rank > max_severity_rank:
                max_severity_rank = severity_rank
            affected = sorted({record.server_id for record in group if record.server_id})
            backends = sorted(
                {record.backend_server for record in group if record.backend_server}
            )
            locations = location_list(group)
            samples = self._issue_evidence_samples(
                group,
                category,
                self.config.report.max_evidence_samples,
            )
            players: list[str] = []
            if category == "chat_review":
                chat_samples = self._chat_issue_evidence_samples(
                    chat_topics,
                    self.config.report.max_evidence_samples,
                )
                if chat_samples:
                    samples = chat_samples
                players = self._chat_issue_players(chat_topics)
                if any("chat_flood" in (record.tags or []) for record in group):
                    self._append_unique(chat_labels, "刷屏")
                if any("chat_abuse" in (record.tags or []) for record in group):
                    for info in chat_infos:
                        for label in info.get("labels") or []:
                            if str(label) in CHAT_CHAT_REVIEW_LABELS:
                                self._append_unique(chat_labels, str(label))
            else:
                players = sorted(
                    {
                        str((record.context or {}).get("chatPlayer") or "").strip()
                        for record in group
                        if (record.context or {}).get("chatPlayer")
                    }
                )
            timestamps = [record.timestamp for record in group if record.timestamp]
            should_alert = self._should_alert(
                severity, len(group), affected, backends, category, group
            )
            suggested_action = self._suggest_action(category, tag, severity)
            if category == "chat_review":
                suggested_action = self._chat_issue_action(chat_topics)
            ops_action = self._ops_issue_action(ops_categories, ops_subtypes, category, tag, severity)
            if ops_action:
                suggested_action = ops_action
            issue_terms = self._issue_terms(group)
            for label in chat_labels:
                self._append_unique(issue_terms, label)
            for subtype in ops_subtypes:
                self._append_unique(issue_terms, subtype)
            issues.append(
                {
                    "category": category,
                    "tag": tag,
                    "severity": severity,
                    "confidence": min(0.98, 0.5 + len(group) * 0.08),
                    "affected_servers": affected,
                    "affected_backends": backends,
                    "affected_locations": locations,
                    "affected_locations_text": format_locations(locations),
                    "evidence_count": len(group),
                    "unique_players": len(players),
                    "players": players,
                    "players_text": ", ".join(players) if players else "无",
                    "first_seen_ts": min(timestamps) if timestamps else 0,
                    "last_seen_ts": max(timestamps) if timestamps else 0,
                    "evidence_samples": (
                        samples if self.config.report.include_evidence_samples else []
                    ),
                    "signal_count": len(group),
                    "issue_terms": issue_terms,
                    "chat_labels": chat_labels,
                    "chat_primary_categories": chat_primary_categories,
                    "chat_severity": chat_severity,
                    "ops_categories": ops_categories,
                    "ops_subtypes": ops_subtypes,
                    "ops_impacts": ops_impacts,
                    "ops_severity": ops_severity,
                    "suggested_action": suggested_action,
                    "should_alert": should_alert,
                }
            )

        if not categories["daily"]:
            categories["daily"].append(
                f"窗口内收到 {len(log_records)} 条 Minecraft 运行日志观察。"
            )

        max_severity = next(
            (name for name, rank in SEVERITY_RANK.items() if rank == max_severity_rank),
            "low",
        ) if max_severity_rank else "low"
        any_alert = any(issue["should_alert"] for issue in issues)
        ops_notes, counters = self._ops_notes(counters, issues, max_severity, any_alert)
        window_timestamps = [record.timestamp for record in log_records if record.timestamp]
        window_start_ts = min(window_timestamps) if window_timestamps else 0
        window_end_ts = max(window_timestamps) if window_timestamps else 0
        actual_span_ms = max(0, window_end_ts - window_start_ts)
        requested_span_ms = max(1, int(window_minutes)) * 60 * 1000
        if actual_span_ms > requested_span_ms:
            effective_window_minutes = max(1, (actual_span_ms + 59_999) // 60_000)
            window_label = (
                f"实际覆盖约 {effective_window_minutes} 分钟"
                f"（请求窗口 {window_minutes} 分钟）"
            )
        else:
            effective_window_minutes = window_minutes
            window_label = f"最近 {window_minutes} 分钟"

        # PR10: 聊天热点总结 + Vulcan 反作弊结构化告警
        report = {
            "summary": (
                f"{window_label}，收到 {len(log_records)} 条 "
                "Minecraft 运行日志观察。"
            ),
            "time_window": window_label,
            "window_start_ts": window_start_ts,
            "window_end_ts": window_end_ts,
            "_window_minutes": effective_window_minutes,
            "servers": servers if not server_id else [server_id],
            "server_names": server_names,
            "proxy_ids": proxy_ids,
            "log_count": len(log_records),
            "incident_findings": [],
            "categories": self._categories_dict(categories),
            "issues": issues,
            "ops_notes": ops_notes,
            "max_severity": max_severity,
            "any_alert": any_alert,
            "counters": counters,
            "chat_topics": chat_topics,
            "vulcan_alerts": vulcan_alerts,
        }
        report["report_sections"] = build_report_sections(report)
        return report

    # --- 分类 -------------------------------------------------------------
    def classify(self, record: ObservationRecord) -> str:
        cache_key = id(record)
        cached = self._classification_cache.get(cache_key)
        if cached is not None:
            return cached
        # PR10: daily_noise 标签优先级最高，强制归入 daily，避免正常 login/disconnect
        # 等被 moderation/network 关键词误判为异常事件。
        if "daily_noise" in record.tags:
            self._classification_cache[cache_key] = "daily"
            return "daily"
        # PR10 v3: 行为标签强制归入 chat_review（若该分类开启）。
        # 行为=刷屏(chat_flood) 或 重复违规(chat_abuse)，均由 build() 阶段
        # 基于玩家上下文检测后回填标签。单条关键词命中不强制归入 chat_review——
        # 单次命中只是"线索"，需要结合玩家上下文（同玩家是否重复发送同类内容）
        # 才能判定为"行为"。
        if "chat_review" in self._active_priority:
            if "chat_flood" in record.tags or "chat_abuse" in record.tags:
                self._annotate_chat_classification(record)
                self._classification_cache[cache_key] = "chat_review"
                return "chat_review"
        text = self._record_text(record)
        raw_content = self._record_raw_content(record)
        level = str((record.context or {}).get("level") or "").lower()
        if self._is_benign_mechanical_record(record, raw_content, text, level):
            self._annotate_ops_classification(record)
            self._classification_cache[cache_key] = "daily"
            return "daily"

        chat_info = self._annotate_chat_classification(record)
        ops_info = self._annotate_ops_classification(record)
        # 按当前生效的优先级列表匹配（已应用 category_enabled / category_whitelist），
        # daily 兜底。真正的关闭/白名单过滤在 _prepare_log_records 入口完成；
        # 这里仅对已准入记录按当前启用优先级做后备分类。
        # 注意：chat_review 不再靠单条关键词命中触发——避免"玩家偶尔发一条链接"
        # 被误判为聊天审查违规。关键词命中只在 review_evidence 里作为"线索"呈现。
        ops_category = self._first_ops_report_category(ops_info, self._active_priority)
        if ops_category:
            self._classification_cache[cache_key] = ops_category
            return ops_category
        ops_report_categories = (
            self._ops_report_categories(ops_info) if ops_info else frozenset()
        )
        for category in self._active_priority:
            if category == "daily":
                continue
            if category == "chat_review":
                if self._chat_category_matches(chat_info, "chat_review"):
                    self._classification_cache[cache_key] = category
                    return category
                continue  # 行为标签已在上面处理；单条关键词不再触发
            if self._chat_category_matches(chat_info, category):
                self._classification_cache[cache_key] = category
                return category
            if category in ops_report_categories:
                self._classification_cache[cache_key] = category
                return category
            if self._category_matches(
                record,
                category,
                text,
                raw_content=raw_content,
                level=level,
                assume_not_benign=True,
            ):
                self._classification_cache[cache_key] = category
                return category
        self._classification_cache[cache_key] = "daily"
        return "daily"

    def _category_matches(
        self,
        record: ObservationRecord,
        category: str,
        text: str | None = None,
        *,
        raw_content: str | None = None,
        level: str | None = None,
        assume_not_benign: bool = False,
    ) -> bool:
        text = text if text is not None else self._record_text(record)
        cache_key = (id(record), category)
        if cache_key in self._category_match_cache:
            return self._category_match_cache[cache_key]
        if assume_not_benign:
            matched = self._category_matches_uncached(
                record,
                category,
                text,
                raw_content=raw_content,
                level=level,
                assume_not_benign=True,
            )
        else:
            matched = self._category_matches_uncached(
                record,
                category,
                text,
                raw_content=raw_content,
                level=level,
            )
        self._category_match_cache[cache_key] = matched
        return matched

    def _category_matches_uncached(
        self,
        record: ObservationRecord,
        category: str,
        text: str,
        *,
        raw_content: str | None = None,
        level: str | None = None,
        assume_not_benign: bool = False,
    ) -> bool:
        tags = record.tags or []
        keys = CATEGORY_KEYS.get(category, ())
        raw_content = (
            raw_content
            if raw_content is not None
            else self._record_raw_content(record)
        )
        stack_trace_line = raw_content.startswith("at ") or raw_content.startswith(
            "caused by:"
        )
        level = (
            level
            if level is not None
            else str((record.context or {}).get("level") or "").lower()
        )
        record_words: frozenset[str] | None = None

        def keys_match(candidate_keys: tuple[str, ...]) -> bool:
            nonlocal record_words
            if record_words is None:
                record_words = self._record_words(record, text)
            return self._record_keys_match(record, text, candidate_keys, record_words)

        if (
            not assume_not_benign
            and category != "daily"
            and self._is_benign_mechanical_record(record, raw_content, text, level)
        ):
            return False

        ops_info = (record.context or {}).get("opsClassification")
        if isinstance(ops_info, dict) and ops_info.get("subtype"):
            ops_category = str(ops_info.get("category") or "")
            if ops_category == "数据库与存储" and category in {"complaint", "network", "cross_server", "player_feedback"}:
                return False
            if (
                category == "community_ops"
                and ops_category in {"插件与模组", "传送与位置", "数据库与存储", "网络与代理", "世界与区块"}
                and keys_match(
                    ("could not pass event", "eventexception", "playerteleportevent"),
                )
            ):
                return False

        if stack_trace_line and category in {
            "complaint",
            "network",
            "cross_server",
            "moderation",
            "economy",
            "community",
            "community_ops",
            "player_feedback",
        }:
            return False

        if category == "community":
            if "anticheat_vulcan" in tags:
                return True
            if "issued server command: /fly" in text:
                return False
            strong_markers = (
                "ban", "banned", "kick", "kicked", "mute", "muted",
                "grief", "cheat", "cheating", "anticheat", "anti-cheat",
                "xray", "kill aura", "killaura", "vulcan",
                "封禁", "禁言", "踢出", "作弊", "外挂",
            )
            if keys_match(strong_markers):
                return True
            soft_markers = ("fly", "speed", "reach", "violation", "vl")
            anti_cheat_context = (
                "anticheat" in text
                or "anti-cheat" in text
                or "vulcan" in text
                or "flagged" in text
                or "failed" in text
            )
            return anti_cheat_context and keys_match(soft_markers)

        if category == "community_ops":
            prism_maintenance = (
                "prism" in text
                and (
                    "activityquery" in text
                    or "purge" in text
                    or "purged" in text
                    or "activity records" in text
                )
            )
            if prism_maintenance:
                return False
            ops_markers = (
                "event", "activity", "announcement", "notice", "reward",
                "vote", "poll", "rank", "season", "competition",
                "运营", "活动", "公告", "通知", "奖励", "投票",
                "赛季", "比赛", "招募",
            )
            if keys_match(ops_markers):
                return True
            broad_social_markers = ("discord", "qq group", "community", "群", "社区")
            has_social_surface = keys_match(broad_social_markers)
            has_ops_context = keys_match(
                (
                    "announcement", "notice", "event", "reward", "vote",
                    "poll", "season", "competition", "rank",
                    "公告", "通知", "活动", "奖励", "投票", "赛季", "比赛",
                ),
            )
            return has_social_surface and has_ops_context

        if category == "network":
            if "lost connection: flying is not enabled" in text:
                return False

        if category == "complaint":
            if "if this lead to server tps drop" in text:
                return False
            hikari_connection_pool = (
                ("hikari" in text or "poolbase" in text)
                and "failed to validate connection" in text
            )
            if hikari_connection_pool:
                return False

        if category == "moderation":
            if ("uuid='" in text or "uuid=" in text) and not keys_match(
                (
                    "uuid of player", "permission", "permissions", "auth",
                    "login", "logged in", "logged out", "session",
                    "premium", "whitelist", "没有权限", "权限",
                ),
            ):
                return False

        if category == "bug":
            if level in {"info", ""}:
                info_safe_noise = ("失败", "警告", "failed", "failure", "warn", "warning")
                strong_info_bug = (
                    "error", "exception", "stacktrace", "traceback",
                    "nullpointerexception", "illegalargumentexception",
                    "classnotfoundexception", "nosuchmethoderror",
                    "unsupportedoperationexception", "cannot invoke",
                )
                if any(marker in text for marker in info_safe_noise) and not any(
                    marker in text for marker in strong_info_bug
                ):
                    return False

        if category == "plugin":
            if "update here:" in text or "update available" in text:
                return False
            if level in {"info", ""} and not any(
                marker in text
                for marker in (
                    "could not load", "could not enable", "failed", "failure",
                    "error", "exception", "depend", "dependency",
                    "softdepend", "加载失败", "启用失败", "依赖",
                )
            ):
                return False

        if category == "player_feedback":
            if "chat_message" in tags and keys_match(
                ("没权限", "没有权限", "权限不够", "进不去", "过不去"),
            ):
                return True
            strong_feedback = (
                "suggest", "suggestion", "feedback", "feature request",
                "proposal", "建议", "反馈", "加个", "新增", "优化", "改进",
            )
            if keys_match(strong_feedback):
                return True
            soft_feedback = (
                "idea", "request", "wish", "hope",
                "想法", "希望", "能不能", "可不可以",
            )
            product_context = (
                "feature", "server", "plugin", "shop", "map", "玩法",
                "功能", "服务器", "插件", "商店", "地图", "副本",
                "加", "改", "优化",
            )
            return keys_match(soft_feedback) and keys_match(product_context)

        category_bit = CATEGORY_FEATURE_BITS.get(category)
        native_mask = self._native_category_feature_cache.get(id(record))
        if category_bit is not None and native_mask is not None:
            return bool(native_mask & category_bit)
        return keys_match(keys)

    # --- Tag --------------------------------------------------------------
    def tag(self, record: ObservationRecord) -> str:
        text = self._record_text(record)
        level = str((record.context or {}).get("level") or "").lower()
        if "loop_suppressed" in record.tags:
            return f"server_log_loop_{level or 'warn'}"
        # PR10: Vulcan 反作弊告警单独打 tag，便于报告里按反作弊维度聚合呈现
        if "anticheat_vulcan" in record.tags:
            return "server_log_anticheat_vulcan"
        # 按分类优先级给 tag
        category = self.classify(record)
        tag_map = {
            "community": "server_log_community",
            "chat_review": "server_log_chat_review",
            "player_feedback": "server_log_player_feedback",
            "community_ops": "server_log_community_ops",
            "complaint": "server_log_performance",
            "network": "server_log_network",
            "plugin": "server_log_plugin",
            "cross_server": "server_log_cross_server",
            "moderation": "server_log_auth",
            "economy": "server_log_economy",
        }
        if category in tag_map:
            ops_info = (record.context or {}).get("opsClassification")
            if isinstance(ops_info, dict) and bool(ops_info.get("opsObservation")):
                return f"{tag_map[category]}_observation"
            if category in {"community_ops", "player_feedback"} and level in {
                "",
                "info",
            }:
                chat_info = (record.context or {}).get("chatClassification")
                chat_needs_admin = isinstance(chat_info, dict) and bool(
                    chat_info.get("needs_admin")
                )
                ops_needs_admin = isinstance(ops_info, dict) and bool(
                    ops_info.get("needs_admin")
                )
                if not chat_needs_admin and not ops_needs_admin:
                    return f"{tag_map[category]}_observation"
            return tag_map[category]
        return f"server_log_{level or 'info'}"

    def _category_line(self, tag: str, group: list[ObservationRecord]) -> str:
        servers = ", ".join(sorted({record.server_id for record in group if record.server_id}))
        levels = sorted(
            {
                str((record.context or {}).get("level") or "INFO").upper()
                for record in group
            }
        )
        return (
            f"{tag}: {len(group)} 条运行日志，级别 {', '.join(levels)}，"
            f"服务器 {servers or '未知'}。"
        )

    @staticmethod
    def _partition_issue_evidence(
        group: list[ObservationRecord],
        category: str,
    ) -> list[list[ObservationRecord]]:
        if not group:
            return []
        has_chat = any("chat_message" in (record.tags or ()) for record in group)
        has_server_log = any("chat_message" not in (record.tags or ()) for record in group)
        if category != "player_feedback" and not (has_chat and has_server_log):
            return [group] if group else []
        partitions: dict[str, list[ObservationRecord]] = {}
        for record in group:
            is_chat = "chat_message" in (record.tags or ())
            chat = (record.context or {}).get("chatClassification")
            primary = (
                str(chat.get("primary_category") or "").strip()
                if isinstance(chat, dict)
                else ""
            )
            key = (
                f"chat:{primary or '未结构化反馈'}"
                if is_chat
                else "server"
            )
            partitions.setdefault(key, []).append(record)
        return list(partitions.values())

    @staticmethod
    def _split_issue_group(
        group: list[ObservationRecord],
        *,
        cluster_gap_ms: int = ISSUE_CLUSTER_GAP_MS,
    ) -> list[list[ObservationRecord]]:
        """Split tag buckets whenever evidence is quiet beyond the category gap."""
        if not group:
            return []
        timestamps = sorted(record.timestamp for record in group if record.timestamp)
        if len(timestamps) < 2:
            return [group]

        ordered = sorted(
            group,
            key=lambda record: record.timestamp if record.timestamp else 2**63 - 1,
        )
        segments: list[list[ObservationRecord]] = []
        current: list[ObservationRecord] = []
        last_timestamp = 0
        for record in ordered:
            timestamp = int(record.timestamp or 0)
            if (
                current
                and timestamp
                and last_timestamp
                and timestamp - last_timestamp > cluster_gap_ms
            ):
                segments.append(current)
                current = []
            current.append(record)
            if timestamp:
                last_timestamp = timestamp
        if current:
            segments.append(current)
        return segments or [group]

    def _is_issue_evidence_candidate(
        self,
        record: ObservationRecord,
        category: str,
    ) -> bool:
        """Keep actionable evidence while leaving healthy INFO as report context."""
        if category == "daily":
            return False

        tags = record.tags or ()
        if (
            "chat_flood" in tags
            or "chat_abuse" in tags
            or "anticheat_vulcan" in tags
            or "loop_suppressed" in tags
        ):
            return True

        ctx = record.context or {}
        if "chat_message" in tags:
            chat = ctx.get("chatClassification")
            if isinstance(chat, dict):
                chat_severity = str(chat.get("severity") or "").lower()
                if bool(chat.get("needs_admin")) or SEVERITY_RANK.get(
                    chat_severity,
                    0,
                ) >= SEVERITY_RANK["medium"]:
                    return True
            if category in {
                "community",
                "chat_review",
                "player_feedback",
                "community_ops",
            }:
                return True

        ops = ctx.get("opsClassification")
        if isinstance(ops, dict):
            if bool(ops.get("opsObservation")) and not bool(ops.get("needs_admin")):
                return False
            ops_severity = str(ops.get("severity") or "info").lower()
            if bool(ops.get("needs_admin")) or OPS_SEVERITY_RANK.get(
                ops_severity,
                0,
            ) >= OPS_SEVERITY_RANK["medium"]:
                return True

        level = self._normalized_level(str(ctx.get("level") or ""))
        if level in {"warn", "warning", "error", "severe", "fatal"}:
            return True
        if not level and (
            "warn" in tags or "warning" in tags or "error" in tags
        ):
            return True
        if level not in {"", "info"}:
            return False

        text = self._record_text(record)
        return any(marker in text for marker in ISSUE_ACTIONABLE_INFO_MARKERS)

    # --- 严重级别 ---------------------------------------------------------
    def _severity(self, group: list[ObservationRecord]) -> str:
        # PR10: 全员 daily_noise 的 group 强制 low，避免被 EWMA 突增/网络关键词
        # 提级，保证正常 login/disconnect/UUID 等绝不形成事件。
        if group and all("daily_noise" in record.tags for record in group):
            return "low"
        if group and all(self._low_value_ops_observation(record) for record in group):
            return "low"
        text = " ".join(self._record_text(record) for record in group)
        n = len(group)
        base_severity = self._severity_by_rules(text, n)
        if self._low_value_info_group(group):
            if n <= 2:
                return "low"
            return (
                "medium"
                if SEVERITY_RANK.get(base_severity, 0) > SEVERITY_RANK["medium"]
                else base_severity
            )
        # 异常分数提级：模板计数突增（EWMA + 分位数）只能作为 high 旁证。
        max_anomaly = 0.0
        for record in group:
            ctx = record.context or {}
            try:
                score = float(ctx.get("anomalyScore") or 0)
            except (TypeError, ValueError):
                score = 0.0
            if score > max_anomaly:
                max_anomaly = score
        if max_anomaly >= 0.6:
            # 统计突增只证明频率偏离基线，不能单独证明崩溃或数据事故。
            # critical 必须来自上方的确定性语义规则或结构化运维分类。
            return (
                base_severity
                if SEVERITY_RANK.get(base_severity, 0) >= SEVERITY_RANK["high"]
                else "high"
            )
        return base_severity

    @staticmethod
    def _low_value_ops_observation(record: ObservationRecord) -> bool:
        ops = (record.context or {}).get("opsClassification")
        if not isinstance(ops, dict) or not bool(ops.get("opsObservation")):
            return False
        if bool(ops.get("needs_admin")):
            return False
        severity = str(ops.get("severity") or "info").lower()
        return OPS_SEVERITY_RANK.get(severity, 0) < OPS_SEVERITY_RANK["medium"]

    @staticmethod
    def _low_value_info_group(group: list[ObservationRecord]) -> bool:
        if not group:
            return False
        for record in group:
            ctx = record.context or {}
            level = str(ctx.get("level") or "").lower()
            if level not in {"", "info"}:
                return False
            tags = set(record.tags or [])
            if tags & {"anticheat_vulcan", "chat_flood", "chat_abuse", "loop_suppressed"}:
                return False
            ops = ctx.get("opsClassification")
            if isinstance(ops, dict):
                ops_severity = str(ops.get("severity") or "info").lower()
                if bool(ops.get("needs_admin")) or OPS_SEVERITY_RANK.get(
                    ops_severity,
                    0,
                ) >= OPS_SEVERITY_RANK["medium"]:
                    return False
            chat = ctx.get("chatClassification")
            if isinstance(chat, dict):
                chat_severity = str(chat.get("severity") or "info").lower()
                if bool(chat.get("needs_admin")) or SEVERITY_RANK.get(
                    chat_severity,
                    0,
                ) >= SEVERITY_RANK["medium"]:
                    return False
        return True

    def _severity_by_rules(self, text: str, n: int) -> str:
        """关键词 + 计数驱动的 severity 判定（不含异常分数提级）。"""
        # critical: 崩溃 / OOM / watchdog / 服务停止 / 代理大面积不可用
        if any(marker in text for marker in CRITICAL_MARKERS):
            return "critical"
        # high: 循环刷屏
        if "loop_suppressed" in text:
            return "high"
        # high: 插件加载/启用失败
        if any(
            marker in text
            for marker in ("could not load", "could not enable", "加载失败", "启用失败")
        ):
            return "high" if n >= 1 else "medium"
        # high: chat_review 出现威胁/隐私泄露/严重骚扰
        if any(marker in text for marker in CHAT_SENSITIVE_MARKERS):
            return "high"
        # high: community_ops 活动事故/奖励异常/大范围玩家不满
        if any(marker in text for marker in COMMUNITY_OPS_SEVERE_MARKERS):
            return "high"
        # high: 多条 ERROR（substring，便于匹配 errors/failed 等）
        if any(marker in text for marker in ERROR_MARKERS):
            return "high" if n >= 2 else "medium"
        # high: 性能问题重复出现 >=3
        if _keys_match(text, PERFORMANCE_MARKERS):
            if n >= 3:
                return "high"
            return "medium" if n >= 1 else "low"
        # high: 网络错误 >=5
        if _keys_match(text, NETWORK_MARKERS):
            if n >= 5:
                return "high"
            return "medium" if n >= 2 else "low"
        # medium: chat_review 单次违规/广告/可疑链接/私聊举报
        if _keys_match(text, CHAT_REVIEW_MARKERS):
            return "medium" if n >= 1 else "low"
        # medium: community_ops 活动/奖励争议
        if _keys_match(text, COMMUNITY_OPS_MARKERS):
            return "medium" if n >= 1 else "low"
        # medium: player_feedback 多名玩家反复提出同类建议
        if _keys_match(text, PLAYER_FEEDBACK_MARKERS):
            return "medium" if n >= 3 else "low"
        # medium: 多条 WARN
        if any(marker in text for marker in WARN_MARKERS):
            return "medium" if n >= 2 else "low"
        # medium: 权限/登录/网络异常
        entity_uuid_only = (
            ("uuid='" in text or "uuid=" in text)
            and not _keys_match(
                text,
                (
                    "uuid of player", "permission", "permissions", "auth",
                    "login", "logged in", "logged out", "session",
                    "premium", "whitelist", "没有权限", "权限",
                ),
            )
        )
        if _keys_match(text, CATEGORY_KEYS["moderation"]) and not entity_uuid_only:
            return "medium" if n >= 1 else "low"
        return "low"

    # --- 告警判定 ---------------------------------------------------------
    def _should_alert(
        self,
        severity: str,
        evidence_count: int,
        affected_servers: list[str],
        affected_backends: list[str],
        category: str,
        group: list[ObservationRecord],
    ) -> bool:
        alert = self.config.alert
        if not alert.enabled:
            return False
        # critical 直告，不受 evidence_count 限制
        if severity == "critical":
            return True
        text = " ".join(self._record_text(record) for record in group)
        # 循环刷屏 + high/critical 强制告警
        if "loop_suppressed" in text and severity in {"high", "critical"}:
            return True
        # 多服务器/多后端 + medium/high 强制告警
        multi_scope = len(affected_servers) >= 2 or len(affected_backends) >= 2
        if multi_scope and severity in {"medium", "high"}:
            return True
        # chat_review 特殊规则：默认不告警，除非 severity>=high / evidence_count>=5 / 命中敏感词
        # / 命中 chat_flood/chat_abuse 行为标签（玩家级刷屏已强制 chat_review，需单独触发告警，否则审核漏报）
        if category == "chat_review":
            if severity in {"high", "critical"}:
                return True
            if any(marker in text for marker in CHAT_SENSITIVE_MARKERS):
                return True
            # 行为标签（刷屏/重复违规）强制告警——这些是基于玩家上下文判定的真行为
            if any("chat_flood" in record.tags or "chat_abuse" in record.tags for record in group):
                return True
            return evidence_count >= 5
        # player_feedback 通常不告警
        if category == "player_feedback":
            return False
        # community_ops 仅活动事故/奖励异常/大范围不满才告警（已由 severity=high 覆盖）
        if category == "community_ops":
            return severity in {"high", "critical"}
        # low 不告警
        if SEVERITY_RANK.get(severity, 0) < SEVERITY_RANK.get(alert.min_severity, 3):
            return False
        # 标准：severity >= min_severity 且 evidence_count >= min_evidence_count
        return evidence_count >= alert.min_evidence_count

    # --- 推荐动作（按分类细化）--------------------------------------------
    def _suggest_action(self, category: str, tag: str, severity: str) -> str:
        if severity == "critical":
            return (
                "优先处理：检查 latest.log、崩溃报告（crash-reports/）、压缩历史日志、"
                "最近部署/重启/插件更新记录，并确认是否需要临时回滚。"
            )
        if tag.startswith("server_log_loop_"):
            return "优先查看首条样本对应的插件或服务端模块，避免重复报错继续刷屏。"
        if category == "community":
            return (
                "交由社区管理流程复核；确认处罚来源、玩家 UUID、触发规则、证据样本，"
                "避免误封。"
            )
        if category == "chat_review":
            return (
                "交由聊天审查流程复核；检查聊天原文、上下文、玩家 UUID、时间点、频道/私聊来源，"
                "并确认是否涉及辱骂、骚扰、广告、刷屏、威胁或隐私泄露。"
            )
        if category == "player_feedback":
            return (
                "整理为玩家反馈工单；记录玩家诉求、出现频率、影响范围和可执行性，"
                "交由社区运营或产品负责人评估。"
            )
        if category == "community_ops":
            return (
                "交由社区运营跟进；确认活动、公告、奖励、投票、赛季或玩家关系相关上下文，"
                "评估是否需要发布公告、回复玩家、调整活动规则或同步管理组。"
            )
        if category == "complaint" or tag == "server_log_performance":
            return (
                "检查 TPS、MSPT、内存、实体数量、区块加载、红石机器、定时任务和插件耗时；"
                "优先对照 spark/timings 与 latest.log。"
            )
        if category == "network" or tag == "server_log_network":
            return (
                "检查代理到后端的连通性、端口、防火墙、Velocity/Bungee 转发配置、"
                "后端在线状态，以及玩家来源网络是否集中异常。"
            )
        if category == "plugin" or tag == "server_log_plugin":
            return (
                "检查报错首条堆栈对应插件、插件版本、服务端核心版本、依赖插件是否缺失，"
                "以及最近是否更新过插件或配置。"
            )
        if category == "cross_server" or tag == "server_log_cross_server":
            return (
                "检查 Velocity/Bungee 配置、player-info-forwarding-mode、forwarding secret、"
                "后端服务器地址、端口、转发协议和防火墙。"
            )
        if category == "moderation" or tag == "server_log_auth":
            return (
                "检查权限组、白名单、登录插件、正版验证、UUID 模式，"
                "以及代理和后端的转发配置是否一致。"
            )
        if category == "economy" or tag == "server_log_economy":
            return (
                "检查 Vault、经济插件、商店插件、数据库连接、玩家余额数据和最近交易记录。"
            )
        if severity in {"high", "critical"}:
            return (
                "优先检查 latest.log、压缩历史日志、崩溃报告、最近部署/重启/插件更新记录，"
                "并评估是否需要回滚。"
            )
        if severity == "medium":
            return "继续观察同类 WARN/ERROR 是否扩大，并保留样本用于后续排查。"
        return "持续观察，无需立即处理。"

    @staticmethod
    def _ops_issue_action(
        ops_categories: list[str],
        ops_subtypes: list[str],
        category: str,
        tag: str,
        severity: str,
    ) -> str:
        categories = set(ops_categories)
        subtypes = set(ops_subtypes)
        if "数据库与存储" in categories:
            if "磁盘空间不足" in subtypes:
                return "立即检查磁盘剩余空间、日志/备份占用和数据库目录；确认玩家数据、世界存档、经济流水是否写入成功。"
            if "玩家/世界数据保存失败" in subtypes:
                return "按证据时间核对玩家 data、world/region 存档、备份状态和插件写入日志，必要时先冻结补偿或回档判断。"
            return "检查 Hikari/JDBC 连接池、数据库可用性、慢查询、最大连接数和磁盘 I/O；若涉及玩家资产，同步核对经济流水和背包数据。"
        if "经济与资产" in categories:
            return "按玩家和时间核对 Vault/经济插件、商店插件、数据库交易流水、余额变更和物品发放记录，再决定是否补偿。"
        if "传送与位置" in categories:
            return "核对传送插件、跨世界加载、后端转发和玩家位置保存；重点复现证据时间点的 /home、/tp、换世界流程。"
        if "网络与代理" in categories:
            return "检查代理到后端的连通性、端口、防火墙、Velocity/Bungee 转发配置、forwarding secret 和后端在线状态。"
        if "性能与资源" in categories:
            return "优先检查 TPS/MSPT、GC、内存、实体/红石压力、区块加载和插件耗时，并对照玩家卡顿/掉线反馈的时间点。"
        if "插件与模组" in categories:
            return "定位证据中的插件名、事件名或任务名，检查插件版本、依赖、配置和最近更新；先处理影响玩家流程的报错插件。"
        if "世界与区块" in categories:
            return "检查对应世界、region/chunk 文件、实体 ticking 日志和最近保存/回档记录；必要时从备份恢复局部区块。"
        if "权限与命令" in categories:
            return "核对权限组、命令来源、登录/UUID 模式和代理转发配置，确认是否只影响个别玩家或管理命令。"
        if "玩家会话与登录" in categories:
            return "核对认证服务、白名单、登录插件、UUID 模式和代理转发；统计是否集中影响同一时间段或同一后端。"
        if "安全与反作弊" in categories:
            return "人工复核玩家 UUID、VL/检查类型、触发时间和原始上下文；只处理证据能对应到的玩家与行为。"
        if "备份与恢复" in categories:
            return "检查备份任务日志、目标目录、可用空间和最近一次可恢复快照，确认事故恢复能力是否受影响。"
        if "启动与关闭" in categories and severity in {"high", "critical"}:
            return "检查崩溃报告、watchdog 首条堆栈、最近插件/配置变更和重启记录，先确认是否需要回滚或临时下线问题插件。"
        return ""

    # --- 运维备注（增强版）------------------------------------------------
    def _ops_notes(
        self,
        counters: dict[str, int],
        issues: list[dict[str, Any]],
        max_severity: str,
        any_alert: bool,
    ) -> tuple[list[str], dict[str, int]]:
        notes: list[str] = []
        suppressed = counters["loop_suppressed"]
        if suppressed:
            notes.append(
                f"已过滤 {suppressed} 条重复服务器报错循环日志，建议优先查看首条原始样本。"
            )

        affected_servers_set: set[str] = set()
        affected_backends_set: set[str] = set()
        for issue in issues:
            affected_servers_set.update(issue.get("affected_servers") or [])
            affected_backends_set.update(issue.get("affected_backends") or [])
        counters["affected_servers"] = len(affected_servers_set)
        counters["affected_backends"] = len(affected_backends_set)

        counter_parts = []
        if counters["error"]:
            counter_parts.append(f"ERROR {counters['error']} 条")
        if counters["warn"]:
            counter_parts.append(f"WARN {counters['warn']} 条")
        if counters["performance"]:
            counter_parts.append(f"PERFORMANCE {counters['performance']} 条")
        if counters["network"]:
            counter_parts.append(f"NETWORK {counters['network']} 条")
        if counters["plugin"]:
            counter_parts.append(f"PLUGIN {counters['plugin']} 条")
        if counter_parts:
            notes.append("窗口内 " + "，".join(counter_parts) + "。")

        ops_parts = []
        if counters["chat_review"]:
            ops_parts.append(f"聊天审查 {counters['chat_review']} 条")
        if counters["player_feedback"]:
            ops_parts.append(f"玩家建议 {counters['player_feedback']} 条")
        if counters["community_ops"]:
            ops_parts.append(f"社区运营 {counters['community_ops']} 条")
        if ops_parts:
            notes.append("窗口内 " + "，".join(ops_parts) + "。")

        scope_parts = []
        if counters["affected_servers"]:
            scope_parts.append(f"{counters['affected_servers']} 个服务器")
        if counters["affected_backends"]:
            scope_parts.append(f"{counters['affected_backends']} 个后端")
        if scope_parts:
            notes.append(
                f"影响 {'、'.join(scope_parts)}，最高严重级别 {max_severity}。"
            )

        if any_alert:
            triggered = next(
                (issue for issue in issues if issue.get("should_alert")),
                None,
            )
            if triggered:
                notes.append(
                    f"已达到告警条件：severity={triggered['severity']}，"
                    f"evidence_count={triggered['evidence_count']}。"
                )
            else:
                notes.append("已达到告警条件。")

        return notes, counters

    # --- 辅助 -------------------------------------------------------------
    def _categories_dict(self, categories: dict[str, list[str]]) -> dict[str, list[str]]:
        """按固定顺序输出 categories，包含 network/plugin/chat_review/player_feedback/community_ops。"""
        return {
            "daily": categories.get("daily", []),
            "complaint": categories.get("complaint", []),
            "bug": categories.get("bug", []),
            "network": categories.get("network", []),
            "plugin": categories.get("plugin", []),
            "economy": categories.get("economy", []),
            "community": categories.get("community", []),
            "chat_review": categories.get("chat_review", []),
            "player_feedback": categories.get("player_feedback", []),
            "community_ops": categories.get("community_ops", []),
            "moderation": categories.get("moderation", []),
            "cross_server": categories.get("cross_server", []),
            "suggestion": categories.get("suggestion", []),
        }

    # --- 玩家级刷屏检测（PR10 v2）-----------------------------------------
    def _detect_and_tag_floods(
        self, records: list[ObservationRecord]
    ) -> list[dict[str, Any]]:
        """检测玩家级刷屏行为，给参与刷屏的记录打 chat_flood 标签。

        刷屏定义（百度百科+社区规则）：
        同一ID短时间集中发送大量重复或高度相似的信息。
        - 连续 5 条以上重复/相似 = 轻微刷屏
        - 连续 10 条以上 = 恶意刷屏

        三类刷屏：
        1. high_frequency: 30 秒内同一玩家发送 >=8 条消息
        2. repeat_content: 5 分钟内同一玩家发送 >=5 条相同/高度相似消息
        3. meaningless: 5 分钟内同一玩家发送 >=3 条无意义符号消息

        返回 flood_events 列表（用于 chat_topics.flood_players 呈现给 LLM）。
        """
        # 延迟导入避免循环依赖
        from ..runtime_log import _detect_chat_flood

        chat_records = [r for r in records if "chat_message" in r.tags]
        if not chat_records:
            return []
        floods_by_player = _detect_chat_flood(chat_records)
        if not floods_by_player:
            return []

        flood_events: list[dict[str, Any]] = []
        for player, events in floods_by_player.items():
            for event in events:
                flood_events.append(event)
        # 重新扫描记录，给参与刷屏窗口的记录打标签
        # 通过 player + 时间窗口匹配
        for record in chat_records:
            ctx = record.context or {}
            player = str(ctx.get("chatPlayer") or "").strip()
            if not player or player not in floods_by_player:
                continue
            ts = record.timestamp or 0
            for event in floods_by_player[player]:
                if (event["window_start_ms"] <= ts <= event["window_end_ms"]
                    and "chat_flood" not in record.tags):
                    record.tags.append("chat_flood")
                    # 失效依赖 tags 的缓存，避免后续匹配用到不含新标签的旧文本。
                    self._invalidate_record_text_caches(record)
                    # 记录刷屏类型到 context 供 LLM 呈现
                    ctx.setdefault("floodTypes", [])
                    if event["flood_type"] not in ctx["floodTypes"]:
                        ctx["floodTypes"].append(event["flood_type"])
                    break
        return flood_events

    # --- 玩家级重复违规检测（PR10 v3）-------------------------------------
    def _detect_and_tag_abuse(
        self, records: list[ObservationRecord]
    ) -> list[dict[str, Any]]:
        """检测玩家级重复违规行为，给重复命中的记录打 chat_abuse 标签。

        PR10 v3: 行为判断必须有上下文。单条关键词命中只是"线索"，
        同一玩家在窗口内多次命中"同类"关键词才构成"行为"：
        - 同玩家 >=2 条命中 URL 类（discord.gg/http/https） → 链接广告行为
        - 同玩家 >=2 条命中 交易广告类（代练/卖号/加群） → 交易广告行为
        - 同玩家 >=2 条命中 辱骂类 → 辱骂行为
        - 同玩家 >=1 条命中 敏感词（威胁/开盒/人肉） → 直接敏感行为

        返回 abuse_events 列表（用于 review_evidence 上下文呈现）。
        """
        chat_records = [r for r in records if "chat_message" in r.tags]
        if not chat_records:
            return []
        # 按玩家聚合
        player_records: dict[str, list[ObservationRecord]] = defaultdict(list)
        for record in chat_records:
            ctx = record.context or {}
            player = str(ctx.get("chatPlayer") or "").strip()
            if player:
                player_records[player].append(record)

        abuse_events: list[dict[str, Any]] = []
        abuse_record_ids: set[str] = set()
        for player, records_sorted in player_records.items():
            # 按类别统计命中
            hits_by_category: dict[str, list[tuple[ObservationRecord, list[str]]]] = defaultdict(list)
            for record in records_sorted:
                hit_keys = self._detect_chat_review_hits(record)
                if hit_keys:
                    category = self._classify_hit_keys(hit_keys)
                    hits_by_category[category].append((record, hit_keys))

            for category, hits in hits_by_category.items():
                # 敏感词：1 条即行为；其他类：>=2 条为行为
                is_behavior = (category == "sensitive") or (len(hits) >= 2)
                if not is_behavior:
                    continue
                # 给这些记录打 chat_abuse 标签
                for record, hit_keys in hits:
                    if "chat_abuse" not in (record.tags or []):
                        record.tags.append("chat_abuse")
                        # 失效依赖 tags 的缓存，避免后续匹配用到不含新标签的旧文本。
                        self._invalidate_record_text_caches(record)
                    ctx = record.context or {}
                    ctx.setdefault("abuseCategories", [])
                    if category not in ctx["abuseCategories"]:
                        ctx["abuseCategories"].append(category)
                    abuse_record_ids.add(record.event_id)
                abuse_events.append({
                    "player": player,
                    "category": category,
                    "hit_count": len(hits),
                    "samples": [
                        str((r.context or {}).get("chatMessage") or r.content).strip()[:150]
                        for r, _ in hits[:3]
                    ],
                })
        return abuse_events

    # --- 聊天热点总结（PR10）---------------------------------------------
    def _build_chat_topics(
        self,
        records: list[ObservationRecord],
        flood_events: list[dict[str, Any]] | None = None,
        abuse_events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """从 chat_message 标签记录中聚合聊天热点。

        返回结构：
        - total_messages: 聊天消息总数
        - unique_players: 不同玩家数
        - top_players: 按消息数排序的活跃玩家，含样本消息
        - top_keywords: 高频关键词（去除停用词后）
        - sample_messages: 时间序样本消息
        - flood_players: 刷屏玩家列表（PR10 v2，玩家级时间窗口聚合检测）
        - abuse_players: 重复违规玩家列表（PR10 v3，基于玩家上下文的行为判断）
        - review_evidence: 需审查的聊天证据（行为 + 线索，含玩家上下文）

        chat_summary_enabled=false 时返回空字典。
        """
        if not self.config.runtime_log.chat_summary_enabled:
            return {}
        chat_records = [
            record for record in records if "chat_message" in record.tags
        ]
        if not chat_records:
            return {
                "total_messages": 0,
                "unique_players": 0,
                "top_players": [],
                "top_keywords": [],
                "sample_messages": [],
                "category_counts": {},
                "label_counts": {},
                "severity_counts": {},
                "classified_messages": [],
                "admin_messages": [],
                "flood_players": [],
                "abuse_players": [],
                "review_evidence": [],
            }
        max_topics = max(1, self.config.runtime_log.chat_summary_max_topics)
        max_samples = max(1, self.config.runtime_log.chat_summary_max_samples)

        # 按玩家聚合
        player_messages: dict[str, list[ObservationRecord]] = defaultdict(list)
        all_messages: list[str] = []
        for record in chat_records:
            ctx = record.context or {}
            player = str(ctx.get("chatPlayer") or "").strip()
            message = str(ctx.get("chatMessage") or record.content).strip()
            player_messages[player].append(record)
            if message:
                all_messages.append(message)

        top_players_sorted = sorted(
            player_messages.items(),
            key=lambda item: len(item[1]),
            reverse=True,
        )[:max_topics]
        top_players = []
        for player, group in top_players_sorted:
            samples = [
                str((r.context or {}).get("chatMessage") or r.content).strip()
                for r in group[:max_samples]
            ]
            top_players.append(
                {
                    "player": player or "(unknown)",
                    "message_count": len(group),
                    "samples": [s for s in samples if s],
                }
            )

        # 高频关键词（简单分词 + 停用词过滤，不依赖外部 NLP 库）
        top_keywords = self._extract_top_keywords(all_messages, max_topics)

        # 时间序样本消息（覆盖整个窗口）
        sample_messages = []
        step = max(1, len(chat_records) // max_samples)
        for index in range(0, len(chat_records), step):
            record = chat_records[index]
            ctx = record.context or {}
            player = str(ctx.get("chatPlayer") or "").strip()
            message = str(ctx.get("chatMessage") or record.content).strip()
            if message:
                prefix = f"<{player}> " if player else ""
                sample_messages.append(prefix + message)
            if len(sample_messages) >= max_samples:
                break

        category_counts: Counter[str] = Counter()
        label_counts: Counter[str] = Counter()
        severity_counts: Counter[str] = Counter()
        classified_messages: list[dict[str, Any]] = []
        admin_messages: list[dict[str, Any]] = []
        classified_limit = max(10, max_samples * 4)
        admin_limit = max(10, max_samples * 4)
        for record in sorted(chat_records, key=lambda item: item.timestamp or 0):
            ctx = record.context or {}
            chat_info = self._annotate_chat_classification(record)
            primary = str(chat_info.get("primary_category") or "普通交流")
            severity = str(chat_info.get("severity") or "info")
            labels = [str(label) for label in (chat_info.get("labels") or []) if str(label)]
            category_counts[primary] += 1
            severity_counts[severity] += 1
            for label in labels:
                label_counts[label] += 1
            item = {
                "time_text": _format_timestamp(record.timestamp) if record.timestamp else "",
                "server_id": record.server_id or "",
                "player": str(ctx.get("chatPlayer") or "").strip(),
                "message": str(ctx.get("chatMessage") or record.content).strip()[:200],
                "primary_category": primary,
                "labels": labels,
                "severity": severity,
                "needs_admin": bool(chat_info.get("needs_admin")),
            }
            if item["needs_admin"] and len(admin_messages) < admin_limit:
                admin_messages.append(item)
            if (
                len(classified_messages) < classified_limit
                and (
                    item["needs_admin"]
                    or severity != "info"
                    or len(classified_messages) < max_samples
                )
            ):
                classified_messages.append(item)

        # PR10 v2: 刷屏玩家结构化呈现——把 flood_events 转成 LLM 友好格式
        review_enabled = self._category_active("chat_review")
        flood_players = (
            self._format_flood_players(flood_events or [])
            if review_enabled
            else []
        )
        # PR10 v3: 重复违规玩家结构化呈现——同一玩家多次命中同类关键词
        abuse_players = (
            self._format_abuse_players(abuse_events or [])
            if review_enabled
            else []
        )

        # PR10 v3: 聊天审查证据——基于玩家上下文（行为 + 线索）
        review_evidence = (
            self._build_chat_review_evidence(chat_records, max_samples=10)
            if review_enabled
            else []
        )

        return {
            "total_messages": len(chat_records),
            "unique_players": len(player_messages),
            "top_players": top_players,
            "top_keywords": top_keywords,
            "sample_messages": sample_messages,
            "category_counts": dict(category_counts.most_common()),
            "label_counts": dict(label_counts.most_common(30)),
            "severity_counts": dict(severity_counts.most_common()),
            "classified_messages": classified_messages,
            "admin_messages": admin_messages,
            "flood_players": flood_players,
            "abuse_players": abuse_players,
            "review_evidence": review_evidence,
        }

    def _format_flood_players(
        self, flood_events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """把 flood_events 转成 LLM 友好的 flood_players 列表。

        每个玩家一个条目，聚合该玩家的所有刷屏事件：
        - player: 玩家名
        - flood_types: 刷屏类型列表（high_frequency/repeat_content/meaningless）
        - total_messages: 窗口内消息总数
        - time_range: 时间范围 HH:MM:SS-HH:MM:SS
        - events: 各刷屏事件详情（type/window/message_count/samples）
        """
        if not flood_events:
            return []
        # 按玩家聚合
        by_player: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in flood_events:
            by_player[event["player"]].append(event)
        result: list[dict[str, Any]] = []
        for player, events in by_player.items():
            all_ts = []
            for e in events:
                all_ts.append(e["window_start_ms"])
                all_ts.append(e["window_end_ms"])
            time_range = ""
            if all_ts:
                start = _format_timestamp(min(all_ts))
                end = _format_timestamp(max(all_ts))
                time_range = f"{start}-{end}"
            flood_types = sorted({e["flood_type"] for e in events})
            total_msgs = sum(e["message_count"] for e in events)
            # 收集样本（去重，最多 5 条）
            samples: list[str] = []
            seen: set[str] = set()
            for e in sorted(events, key=lambda x: x["window_start_ms"]):
                for s in e.get("samples", []):
                    if s not in seen:
                        samples.append(s)
                        seen.add(s)
                    if len(samples) >= 5:
                        break
                if len(samples) >= 5:
                    break
            result.append({
                "player": player,
                "flood_types": flood_types,
                "total_messages": total_msgs,
                "time_range": time_range,
                "events": [
                    {
                        "type": e["flood_type"],
                        "message_count": e["message_count"],
                        "time_range": (
                            f"{_format_timestamp(e['window_start_ms'])}-"
                            f"{_format_timestamp(e['window_end_ms'])}"
                        ),
                        "samples": e.get("samples", [])[:3],
                    }
                    for e in sorted(events, key=lambda x: x["window_start_ms"])
                ],
                "samples": samples,
            })
        # 按消息数降序
        result.sort(key=lambda x: x["total_messages"], reverse=True)
        return result

    def _format_abuse_players(
        self, abuse_events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """把 abuse_events 转成 LLM 友好的 abuse_players 列表。

        每个玩家一个条目，聚合该玩家的所有重复违规事件：
        - player: 玩家名
        - abuse_categories: 违规类别列表（url/abuse_language/trade_ad/sensitive）
        - total_hits: 命中总次数
        - events: 各违规事件详情（category/hit_count/samples）
        - samples: 样本原文（最多 5 条）
        """
        if not abuse_events:
            return []
        by_player: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in abuse_events:
            by_player[event["player"]].append(event)
        result: list[dict[str, Any]] = []
        for player, events in by_player.items():
            categories = sorted({e["category"] for e in events})
            total_hits = sum(e["hit_count"] for e in events)
            samples: list[str] = []
            seen: set[str] = set()
            for e in events:
                for s in e.get("samples", []):
                    if s not in seen:
                        samples.append(s)
                        seen.add(s)
                    if len(samples) >= 5:
                        break
                if len(samples) >= 5:
                    break
            result.append({
                "player": player,
                "abuse_categories": categories,
                "total_hits": total_hits,
                "events": [
                    {
                        "category": e["category"],
                        "hit_count": e["hit_count"],
                        "samples": e.get("samples", [])[:3],
                    }
                    for e in events
                ],
                "samples": samples,
            })
        result.sort(key=lambda x: x["total_hits"], reverse=True)
        return result

    @staticmethod
    def _chat_issue_players(chat_topics: dict[str, Any]) -> list[str]:
        players: list[str] = []
        seen: set[str] = set()
        for key in ("flood_players", "abuse_players", "review_evidence"):
            for item in chat_topics.get(key) or []:
                player = str((item or {}).get("player") or "").strip()
                if player and player not in seen:
                    seen.add(player)
                    players.append(player)
        return players

    @staticmethod
    def _chat_context_items(
        target: ObservationRecord,
        records: list[ObservationRecord],
        before: int = 2,
        after: int = 2,
        index_map: dict[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        # 优先用预建的 event_id -> index 映射做 O(1) 查找，避免每次调用线性扫描。
        index = -1
        if index_map is not None:
            index = index_map.get(target.event_id, -1)
        if index < 0:
            try:
                index = next(
                    i for i, record in enumerate(records)
                    if record is target or record.event_id == target.event_id
                )
            except StopIteration:
                index = -1
        if index < 0:
            window = [target]
        else:
            start = max(0, index - before)
            end = min(len(records), index + after + 1)
            window = records[start:end]
        items: list[dict[str, Any]] = []
        for record in window:
            ctx = record.context or {}
            items.append(
                {
                    "hit": record is target or record.event_id == target.event_id,
                    "time_text": _format_timestamp(record.timestamp) if record.timestamp else "",
                    "player": str(ctx.get("chatPlayer") or "").strip(),
                    "message": str(ctx.get("chatMessage") or record.content).strip()[:160],
                }
            )
        return items

    @staticmethod
    def _format_chat_context(items: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for item in items:
            prefix = ">" if item.get("hit") else ""
            time_text = str(item.get("time_text") or "").strip()
            player = str(item.get("player") or "").strip() or "unknown"
            message = str(item.get("message") or "").strip()
            if not message:
                continue
            parts.append(f"{prefix}{time_text} <{player}> {message}")
        return " | ".join(parts)

    @staticmethod
    def _chat_issue_action(chat_topics: dict[str, Any]) -> str:
        actions: list[str] = []
        type_labels = {
            "repeat_content": "重复发送相同内容",
            "meaningless": "连续发送无意义内容",
            "high_frequency": "短时间高频发言",
        }
        for item in chat_topics.get("flood_players") or []:
            player = str((item or {}).get("player") or "").strip()
            time_range = str((item or {}).get("time_range") or "").strip()
            flood_types = [
                type_labels.get(str(kind), str(kind))
                for kind in ((item or {}).get("flood_types") or [])
            ]
            samples = [str(s).strip() for s in ((item or {}).get("samples") or []) if str(s).strip()]
            sample = samples[0] if samples else ""
            total = int((item or {}).get("total_messages") or 0)
            if player:
                actions.append(
                    f"{player} {time_range or '该窗口'} {','.join(flood_types) or '触发聊天审查'}"
                    f"（{total} 条，样本：{sample or '见引用上下文'}）"
                )
            if len(actions) >= 4:
                break
        for item in chat_topics.get("abuse_players") or []:
            if len(actions) >= 4:
                break
            player = str((item or {}).get("player") or "").strip()
            categories = ",".join(str(v) for v in ((item or {}).get("abuse_categories") or []))
            samples = [str(s).strip() for s in ((item or {}).get("samples") or []) if str(s).strip()]
            if player:
                actions.append(
                    f"{player} 多次命中 {categories or '聊天审查'}（样本：{samples[0] if samples else '见引用上下文'}）"
                )
        if not actions:
            return (
                "按引用上下文逐条复核聊天原文、玩家、时间点和频道来源；只处理证据中列出的玩家与消息，不把整段聊天泛化为违规。"
            )
        return (
            "按玩家逐项判定：" + "；".join(actions)
            + "。先看引用上下文判断是玩笑/正常点名还是刷屏、骚扰、广告或隐私泄露；只处理列出的玩家与时间段，不把整段聊天泛化为违规。"
        )

    @staticmethod
    def _chat_issue_evidence_samples(
        chat_topics: dict[str, Any],
        limit: int,
    ) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()

        def add(line: str) -> None:
            clean = re.sub(r"\s+", " ", str(line or "")).strip()
            if not clean or clean in seen or len(out) >= limit:
                return
            seen.add(clean)
            out.append(clean[:240])

        review_items = list(chat_topics.get("review_evidence") or [])
        emitted_review: set[tuple[str, str, str]] = set()

        def add_review(item: dict[str, Any]) -> None:
            player = str((item or {}).get("player") or "").strip()
            time_text = str((item or {}).get("time_text") or "").strip()
            message = str((item or {}).get("message") or "").strip()
            key = (player, time_text, message)
            if key in emitted_review:
                return
            emitted_review.add(key)
            reason = str((item or {}).get("reason") or "review").strip()
            context = HeuristicReportBuilder._format_chat_context(
                list((item or {}).get("context_messages") or [])
            )
            if context:
                add(f"[chat {reason}] {context}")
            else:
                add(f"[chat {reason} {time_text}] <{player}> {message}")

        for item in chat_topics.get("flood_players") or []:
            player = str((item or {}).get("player") or "").strip()
            for review_item in review_items:
                if str((review_item or {}).get("player") or "").strip() == player:
                    add_review(review_item)
                    break

        if out and len(out) >= min(limit, len(chat_topics.get("flood_players") or [])):
            return out[:limit]

        for item in review_items:
            add_review(item)

        for item in chat_topics.get("flood_players") or []:
            player = str((item or {}).get("player") or "").strip()
            types = ", ".join(str(v) for v in ((item or {}).get("flood_types") or []))
            time_range = str((item or {}).get("time_range") or "").strip()
            samples = (item or {}).get("samples") or []
            if not samples:
                for event in (item or {}).get("events") or []:
                    samples.extend((event or {}).get("samples") or [])
            for sample in samples[:3]:
                add(f"[chat flood {types} {time_range}] <{player}> {sample}")

        for item in chat_topics.get("abuse_players") or []:
            player = str((item or {}).get("player") or "").strip()
            categories = ", ".join(
                str(v) for v in ((item or {}).get("abuse_categories") or [])
            )
            for sample in ((item or {}).get("samples") or [])[:3]:
                add(f"[chat review {categories}] <{player}> {sample}")

        for item in chat_topics.get("review_evidence") or []:
            player = str((item or {}).get("player") or "").strip()
            time_text = str((item or {}).get("time_text") or "").strip()
            reason = str((item or {}).get("reason") or "review").strip()
            message = str((item or {}).get("message") or "").strip()
            add(f"[chat {reason} {time_text}] <{player}> {message}")

        if not out:
            for sample in chat_topics.get("sample_messages") or []:
                add(f"[chat sample] {sample}")
        return out

    def _build_chat_review_evidence(
        self, chat_records: list[ObservationRecord], max_samples: int = 10
    ) -> list[dict[str, Any]]:
        """从聊天记录中提取需要审查的证据样本，基于玩家级行为上下文判断。

        PR10 v3: 行为判断必须有上下文。不再单条命中关键词就进证据，
        而是按玩家聚合，统计同一玩家在窗口内的：
        - 总消息数
        - 命中关键词的次数
        - 命中的关键词类型（URL/辱骂/代练/交易等）
        - 是否重复发送同类内容

        单次命中：作为"线索"呈现（reason=hint），不强制 chat_review
        重复命中：作为"行为"呈现（reason=abuse），强制 chat_review
        刷屏命中：作为"行为"呈现（reason=flood），强制 chat_review

        返回结构化列表，每条含：
        - player: 玩家名
        - player_total_messages: 该玩家窗口内总消息数（上下文）
        - player_hit_count: 该玩家命中关键词的消息数
        - message: 聊天原文（样本）
        - flood_types: 刷屏类型列表（参与刷屏时）
        - reason: 命中原因（flood/abuse/hint）
          flood=刷屏行为，abuse=重复关键词命中（行为），hint=单次命中（线索）
        - hit_keys: 命中的关键词
        - hit_category: 命中类别（url/abuse_language/trade_ad/sensitive）
        - time_text: HH:MM:SS 时间
        """
        # 第一步：按玩家聚合聊天记录，统计每个玩家的行为上下文
        all_records_sorted = sorted(chat_records, key=lambda r: r.timestamp or 0)
        # 预建 event_id -> index 映射，供 _chat_context_items 做 O(1) 查找，
        # 避免对每条证据样本都线性扫描 all_records_sorted。
        context_index_map: dict[str, int] = {
            record.event_id: i for i, record in enumerate(all_records_sorted)
        }
        player_records: dict[str, list[ObservationRecord]] = defaultdict(list)
        for record in chat_records:
            ctx = record.context or {}
            player = str(ctx.get("chatPlayer") or "").strip()
            if player:
                player_records[player].append(record)

        # 第二步：对每个玩家，找出命中关键词的记录并分类
        # 命中类别分组（用于判断是"单次线索"还是"重复行为"）
        evidence: list[dict[str, Any]] = []
        for player, records in player_records.items():
            records_sorted = sorted(records, key=lambda r: r.timestamp or 0)
            player_total = len(records_sorted)
            # 收集该玩家所有命中记录，按类别分组
            hit_records_by_category: dict[str, list[tuple[ObservationRecord, list[str]]]] = defaultdict(list)
            for record in records_sorted:
                ctx = record.context or {}
                tags = record.tags or []
                is_flood = "chat_flood" in tags
                if is_flood:
                    # 刷屏记录直接归入 flood 类别
                    hit_records_by_category["flood"].append((record, []))
                    continue
                # 检测关键词命中
                hit_keys = self._detect_chat_review_hits(record)
                if hit_keys:
                    category = self._classify_hit_keys(hit_keys)
                    hit_records_by_category[category].append((record, hit_keys))

            # 第三步：根据每个类别的命中次数判断是"行为"还是"线索"
            for category, hits in hit_records_by_category.items():
                hit_count = len(hits)
                # 行为判定阈值：同类命中 >=2 次视为"重复行为"（abuse）
                # 单次命中视为"线索"（hint），刷屏永远是"行为"（flood）
                if category == "flood":
                    reason = "flood"
                elif hit_count >= 2:
                    reason = "abuse"
                else:
                    reason = "hint"

                # 只把"行为"(flood/abuse) 进证据；"线索"(hint) 也进，但标记为 hint，
                # 让 LLM 能看到上下文区分严重性
                for record, hit_keys in hits[:3]:  # 每个玩家每类最多 3 条样本
                    ctx = record.context or {}
                    message = str(ctx.get("chatMessage") or record.content).strip()
                    flood_types = list(ctx.get("floodTypes") or [])
                    time_text = _format_timestamp(record.timestamp) if record.timestamp else ""
                    evidence.append({
                        "player": player,
                        "player_total_messages": player_total,
                        "player_hit_count": hit_count,
                        "message": message[:200],
                        "flood_types": flood_types,
                        "reason": reason,
                        "hit_keys": hit_keys[:5],
                        "hit_category": category if category != "flood" else "",
                        "time_text": time_text,
                        "context_messages": self._chat_context_items(
                            record,
                            all_records_sorted,
                            index_map=context_index_map,
                        ),
                    })
                    if len(evidence) >= max_samples:
                        return evidence
        return evidence

    @staticmethod
    def _detect_chat_review_hits(record: ObservationRecord) -> list[str]:
        """检测单条记录命中的 chat_review 关键词，返回命中的关键词列表。"""
        if "chat_message" not in (record.tags or []):
            return []
        content_lower = (record.content or "").lower()
        hits: list[str] = []
        for key in CHAT_REVIEW_GENERAL_MARKERS:
            if _is_word_key(key):
                if _word_boundary_regex((key,)) and _word_boundary_regex((key,)).search(content_lower):
                    hits.append(key)
            elif key in content_lower:
                hits.append(key)
        for key in CHAT_REVIEW_URL_MARKERS:
            if key in content_lower:
                hits.append(key)
        return hits

    @staticmethod
    def _classify_hit_keys(hit_keys: list[str]) -> str:
        """把命中的关键词归类，用于判断是哪类违规行为。

        返回类别：
        - url: URL/外链信号（discord.gg/http/https/www 等）
        - abuse_language: 辱骂/骚扰/威胁语言
        - trade_ad: 交易/代练/加群广告
        - sensitive: 敏感词（威胁/开盒/人肉/隐私）
        - other: 其他
        """
        url_set = set(CHAT_REVIEW_URL_MARKERS)
        sensitive_set = set(CHAT_SENSITIVE_MARKERS)
        trade_ad_keys = {"代练", "代打", "出售账号", "卖号", "买号", "加群", "加微信", "加qq", "举报聊天"}
        abuse_keys = {"swear", "profanity", "insult", "abuse", "harassment", "threat", "toxic",
                      "advertising", "辱骂", "骂人", "脏话", "骚扰", "威胁", "刷屏"}
        for key in hit_keys:
            if key in sensitive_set:
                return "sensitive"
        for key in hit_keys:
            if key in url_set:
                return "url"
        for key in hit_keys:
            if key in trade_ad_keys:
                return "trade_ad"
        for key in hit_keys:
            if key in abuse_keys:
                return "abuse_language"
        return "other"

    @staticmethod
    def _extract_top_keywords(
        messages: list[str], limit: int
    ) -> list[dict[str, Any]]:
        """从聊天消息中提取高频关键词。

        简单实现：英文按空格分词，过滤短词/停用词；中文按 2-3 字滑窗。
        不依赖 jieba 等分词库，结果用于 AI 进一步归纳的线索。
        """
        if not messages:
            return []
        stop_words = {
            "the", "a", "an", "and", "or", "but", "is", "are", "was", "were",
            "to", "of", "in", "on", "at", "for", "with", "by", "this", "that",
            "it", "as", "be", "have", "has", "do", "does", "i", "you", "he",
            "she", "we", "they", "me", "him", "her", "us", "them",
            "yes", "no", "ok", "okay", "lol", "haha", "ha", "le", "la", "de",
            "的", "了", "是", "在", "我", "你", "他", "她", "们", "个", "这",
            "那", "啊", "吧", "吗", "呢", "哦", "嗯", "呀",
        }
        counter: dict[str, int] = defaultdict(int)
        for message in messages:
            lowered = message.lower()
            # 英文词
            for word in re.findall(r"[a-z]{3,}", lowered):
                if word in stop_words:
                    continue
                counter[word] += 1
            # 中文 2-3 字滑窗
            for match in re.finditer(r"[\u4e00-\u9fa5]{2,}", message):
                segment = match.group(0)
                # 2-gram
                for i in range(len(segment) - 1):
                    gram = segment[i : i + 2]
                    if gram[0] in stop_words or gram[1] in stop_words:
                        continue
                    counter[gram] += 1
        # 至少出现 2 次才算热点
        hot = [(kw, count) for kw, count in counter.items() if count >= 2]
        hot.sort(key=lambda item: item[1], reverse=True)
        return [
            {"keyword": kw, "count": count} for kw, count in hot[:limit] if count > 0
        ]

    # --- Vulcan 反作弊告警结构化（PR10）-----------------------------------
    def _build_vulcan_alerts(
        self, records: list[ObservationRecord]
    ) -> dict[str, Any]:
        """从 anticheat_vulcan 标签记录中提取结构化告警。

        返回结构（应对海量告警，如真实日志 4202 条/2 玩家的场景）：
        - total: 告警总数
        - unique_players: 涉及不同玩家数
        - unique_checks: 涉及不同检查类型数
        - by_player: [{player, count, top_checks: [(check, count)]}] 按告警数降序
        - by_check: [{check, count, players: [player]}] 按告警数降序
        - time_range: {start, end} 最早/最晚告警时间文本
        - samples: 最多 20 条原始告警（time_text + player + check），按时间序

        Vulcan 检测关闭时返回空字典。
        """
        if (
            not self.config.runtime_log.vulcan_detect_enabled
            or not self._category_active("community")
        ):
            return {}
        vulcan_records = [
            record for record in records if "anticheat_vulcan" in record.tags
        ]
        if not vulcan_records:
            return {}
        vulcan_records.sort(key=lambda r: r.timestamp)

        # 按玩家聚合
        player_alerts: dict[str, list[tuple[str, ObservationRecord, int]]] = defaultdict(list)
        check_alerts: dict[str, list[tuple[str, ObservationRecord, int]]] = defaultdict(list)
        total_alerts = 0
        for record in vulcan_records:
            ctx = record.context or {}
            player = str(ctx.get("vulcanPlayer") or "").strip() or "(unknown)"
            check = str(ctx.get("vulcanCheck") or "").strip() or "(unknown)"
            try:
                weight = max(1, int(ctx.get("loopSuppressed") or 1))
            except (TypeError, ValueError):
                weight = 1
            total_alerts += weight
            player_alerts[player].append((check, record, weight))
            check_alerts[check].append((player, record, weight))

        # by_player 排序
        by_player = []
        for player, items in sorted(
            player_alerts.items(),
            key=lambda kv: sum(item[2] for item in kv[1]),
            reverse=True,
        ):
            check_counter: dict[str, int] = defaultdict(int)
            player_total = 0
            for check, _, weight in items:
                check_counter[check] += weight
                player_total += weight
            top_checks = sorted(
                check_counter.items(), key=lambda kv: kv[1], reverse=True
            )[:3]
            by_player.append(
                {
                    "player": player,
                    "count": player_total,
                    "top_checks": [
                        {"check": c, "count": n} for c, n in top_checks
                    ],
                }
            )

        # by_check 排序
        by_check = []
        for check, items in sorted(
            check_alerts.items(),
            key=lambda kv: sum(item[2] for item in kv[1]),
            reverse=True,
        ):
            players = sorted({p for p, _, _ in items})
            by_check.append(
                {
                    "check": check,
                    "count": sum(item[2] for item in items),
                    "players": players,
                }
            )

        # 时间范围
        first_ts = int(vulcan_records[0].timestamp or 0)
        last_ts = int(vulcan_records[-1].timestamp or 0)
        time_range = {
            "start": _format_timestamp(first_ts),
            "end": _format_timestamp(last_ts),
        }

        # 样本（最多 20 条，覆盖整个时间范围）
        sample_records = vulcan_records
        max_samples = 20
        if len(sample_records) > max_samples:
            step = max(1, len(sample_records) // max_samples)
            sample_records = [sample_records[i] for i in range(0, len(sample_records), step)][:max_samples]
        samples = []
        for record in sample_records:
            ctx = record.context or {}
            samples.append(
                {
                    "time_text": _format_timestamp(int(record.timestamp or 0)),
                    "server_id": record.server_id or "",
                    "player": str(ctx.get("vulcanPlayer") or "").strip(),
                    "check": str(ctx.get("vulcanCheck") or "").strip(),
                }
            )

        return {
            "total": total_alerts,
            "unique_players": len(player_alerts),
            "unique_checks": len(check_alerts),
            "by_player": by_player,
            "by_check": by_check,
            "time_range": time_range,
            "samples": samples,
        }

    def _issue_terms(self, group: list[ObservationRecord]) -> list[str]:
        terms: list[str] = []
        record_texts = [self._record_text(record) for record in group]
        # severity markers 用 substring（便于匹配 errors/failed 等）
        substring_markers = CRITICAL_MARKERS + ERROR_MARKERS + WARN_MARKERS + PERFORMANCE_MARKERS
        for marker in substring_markers:
            if any(marker in text for text in record_texts):
                terms.append(marker)
            if len(terms) >= 8:
                return terms
        # 分类 markers 用 _keys_match（与 classify 一致）
        category_marker_groups = (
            NETWORK_MARKERS,
            PLUGIN_MARKERS,
            COMMUNITY_MARKERS,
            CHAT_REVIEW_MARKERS,
            PLAYER_FEEDBACK_MARKERS,
            COMMUNITY_OPS_MARKERS,
        )
        combined_text = " ".join(record_texts)
        for markers in category_marker_groups:
            for marker in markers:
                if _is_word_key(marker):
                    # 词边界匹配的交给 _keys_match 整体判断，单独词不重复输出
                    continue
                if marker in combined_text and marker not in terms:
                    terms.append(marker)
                    if len(terms) >= 8:
                        return terms
        # 补齐词边界命中的短词。分类阶段已经为每条 record 建过 token cache，
        # 这里复用它，避免对同一组日志再次构造多组词边界正则。
        combined_words: set[str] | None = None
        for markers in category_marker_groups:
            word_keys = _word_keys(markers)
            if not word_keys:
                continue
            if combined_words is None:
                combined_words = set()
                for record, text in zip(group, record_texts):
                    combined_words.update(self._record_words(record, text))
            for word in word_keys:
                if word not in combined_words:
                    continue
                if word not in terms:
                    terms.append(word)
                    if len(terms) >= 8:
                        return terms
        return terms

    def _issue_evidence_samples(
        self,
        group: list[ObservationRecord],
        category: str,
        limit: int,
    ) -> list[str]:
        if limit <= 0:
            return []
        scored = [
            (self._issue_evidence_score(record, category), index, record)
            for index, record in enumerate(group)
        ]
        ranked = sorted(
            scored,
            key=lambda item: (-item[0], int(item[2].timestamp or 0), item[1]),
        )
        samples: list[str] = []
        seen: set[str] = set()
        selected_ids: set[int] = set()

        def add_record(record: ObservationRecord) -> bool:
            sample = record.evidence_text()
            key = self._issue_evidence_dedupe_key(record, sample)
            if not key or key in seen:
                return False
            seen.add(key)
            selected_ids.add(id(record))
            samples.append(sample)
            return True

        top_quota = max(1, min(limit, limit * 3 // 5))
        for _, _, record in ranked:
            add_record(record)
            if len(samples) >= top_quota:
                break
        if len(samples) < limit:
            for score, _, record in scored:
                if id(record) in selected_ids:
                    continue
                if score < 50:
                    continue
                add_record(record)
                if len(samples) >= limit:
                    break
        if len(samples) < limit:
            for _, _, record in ranked:
                if id(record) in selected_ids:
                    continue
                add_record(record)
                if len(samples) >= limit:
                    break
        return samples

    def _issue_evidence_dedupe_key(
        self,
        record: ObservationRecord,
        sample: str,
    ) -> str:
        text = self._record_text(record) or sample
        text = re.sub(r"@[0-9a-f]{4,}\b", "@<id>", text)
        text = re.sub(r"\(conn=\d+\)", "(conn=<n>)", text)
        text = re.sub(r"\b\d+\b", "<n>", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:260]

    def _issue_evidence_score(self, record: ObservationRecord, category: str) -> float:
        ctx = record.context or {}
        text = self._record_text(record)
        raw_content = self._record_raw_content(record)
        level = self._normalized_level(str(ctx.get("level") or ""))
        score = 0.0
        if level == "fatal":
            score += 80.0
        elif level == "severe":
            score += 70.0
        elif level == "error":
            score += 55.0
        elif level in {"warn", "warning"}:
            score += 30.0
        if "daily_noise" in (record.tags or []):
            score -= 100.0
        if "anomaly_spike" in (record.tags or []):
            score += 18.0
        if "new_template" in (record.tags or []):
            score += 10.0

        ops = ctx.get("opsClassification")
        if isinstance(ops, dict):
            ops_severity = str(ops.get("severity") or "").lower()
            if bool(ops.get("needs_admin")):
                score += 35.0
            score += OPS_SEVERITY_RANK.get(ops_severity, 0) * 6.0
            ops_category = str(ops.get("category") or "")
            if (
                ops_category in {"启动与关闭", "指标观察"}
                and not bool(ops.get("needs_admin"))
                and ops_severity in {"", "info", "low"}
            ):
                score -= 35.0

        if any(marker in text for marker in CRITICAL_MARKERS):
            score += 80.0
        if any(marker in text for marker in ERROR_MARKERS):
            score += 45.0
        if any(marker in text for marker in WARN_MARKERS):
            score += 12.0
        if self._record_keys_match(record, text, PERFORMANCE_MARKERS):
            score += 10.0
        strong_markers = (
            "connect timed out",
            "connection timed out",
            "connection reset",
            "failed to validate connection",
            "unknown system variable",
            "invalidconfigurationexception",
            "sqlexception",
            "sqltimeoutexception",
            "hikaripool",
            "hikari pool",
            "cannot initialize",
            "could not load",
            "could not enable",
            "failed to load",
            "failed to enable",
        )
        if any(marker in text for marker in strong_markers):
            score += 28.0

        if category == "economy" and self._record_keys_match(
            record,
            text,
            ("quickshop", "vault", "economy", "transaction", "balance", "money", "shop", "商店", "经济"),
        ):
            score += 8.0
        elif category == "plugin" and self._record_keys_match(
            record,
            text,
            ("plugin", "plugins", "dependency", "depend", "config", "插件", "依赖", "配置"),
        ):
            score += 8.0
        elif category == "network" and self._record_keys_match(record, text, NETWORK_MARKERS):
            score += 8.0

        if self._is_benign_mechanical_record(record, raw_content, text, level):
            score -= 60.0
        return score

    def _invalidate_record_text_caches(self, record: ObservationRecord) -> None:
        """修改 record.tags 后清除依赖 tags 的缓存，避免缓存与标签不同步。

        _record_text_uncached 会把 tags 拼入文本，因此追加 chat_flood/chat_abuse
        等标签后必须失效相关缓存，否则后续匹配用的是不含新标签的旧文本。
        """
        key = id(record)
        self._record_text_cache.pop(key, None)
        self._record_word_cache.pop(key, None)
        self._benign_mechanical_cache.pop(key, None)
        self._gate_classification_cache.pop(key, None)
        self._native_category_feature_cache.pop(key, None)

    def _record_text(self, record: ObservationRecord) -> str:
        key = id(record)
        cached = self._record_text_cache.get(key)
        if cached is not None:
            return cached
        text = self._record_text_uncached(record)
        self._record_text_cache[key] = text
        return text

    def _record_raw_content(self, record: ObservationRecord) -> str:
        key = id(record)
        cached = self._record_raw_content_cache.get(key)
        if cached is not None:
            return cached
        text = str(record.content or "").lstrip().lower()
        self._record_raw_content_cache[key] = text
        return text

    def _record_words(self, record: ObservationRecord, text: str) -> frozenset[str]:
        key = id(record)
        cached = self._record_word_cache.get(key)
        if cached is not None and cached[0] == text:
            return cached[1]
        words = frozenset(_KEY_TOKEN_RE.findall(text))
        self._record_word_cache[key] = (text, words)
        return words

    def _record_keys_match(
        self,
        record: ObservationRecord,
        text: str,
        keys: tuple[str, ...],
        record_words: frozenset[str] | None = None,
    ) -> bool:
        """Match category keys using per-record token cache for short ASCII words."""
        words = _word_keys(keys)
        if words:
            if record_words is None:
                record_words = self._record_words(record, text)
            if not record_words.isdisjoint(words):
                return True
        non_word_keys = _non_word_keys(keys)
        if not non_word_keys:
            return False
        if len(non_word_keys) == 1:
            return non_word_keys[0] in text
        return any(key in text for key in non_word_keys)

    def _is_benign_mechanical_record(
        self,
        record: ObservationRecord,
        raw_content: str,
        text: str,
        level: str,
    ) -> bool:
        key = id(record)
        cached = self._benign_mechanical_cache.get(key)
        if cached is not None:
            return cached
        value = _is_benign_mechanical_record(raw_content, text, level)
        ctx = record.context or {}
        hint_code = str(ctx.get("opsHintCode") or "").strip()
        if not value and not hint_code:
            line_kind = str(ctx.get("logLineKind") or "").strip()
            if line_kind in {"stacktrace_frame", "diagnostic_detail"}:
                value = True
            elif level in {"warn", "warning"}:
                quality_flags = {
                    str(flag)
                    for flag in (ctx.get("dataQualityFlags") or ctx.get("qualityFlags") or ())
                }
                value = "low_signal_repetition" in quality_flags
        self._benign_mechanical_cache[key] = value
        return value

    @staticmethod
    def _record_text_uncached(record: ObservationRecord) -> str:
        return f"{record.content} {' '.join(record.tags)}".lower()
