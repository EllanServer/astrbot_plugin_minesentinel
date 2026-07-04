"""MC 与其他平台之间转发消息的消息桥接服务"""

import asyncio
import re
import time
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain

from ..core.models import MCMessage, MessageType, ServerConfig

if TYPE_CHECKING:
    from astrbot.core.star.context import Context

    from ..core.server_manager import ServerManager


# 消息转发反馈的 Emoji 响应常量
EMOJI_OK_GESTURE = 124  # 👌
EMOJI_THUMBS_UP = 76  # 👍
EMOJI_LOVE = 66  # ❤️
EMOJI_ROSE = 63  # 🌹


class MessageBridge:
    """在 MC 服务器和 AstrBot 会话之间转发消息的服务"""

    def __init__(self, context: "Context", server_manager: "ServerManager"):
        self.context = context
        self.server_manager = server_manager
        # 从会话 UMO 到希望接收消息的服务器配置的映射
        self._session_to_servers: dict[str, list[tuple[str, ServerConfig]]] = {}
        # 从 server_id 到配置的映射
        self._server_configs: dict[str, ServerConfig] = {}
        # Track recently forwarded messages to suppress echo
        # Key: (server_id, content_hash), Value: timestamp
        self._recently_forwarded: dict[tuple[str, str], float] = {}
        # Echo suppression window in seconds
        self._echo_suppress_window = 5.0

    def register_server(self, config: ServerConfig):
        """注册用于消息转发的服务器"""
        self._server_configs[config.server_id] = config

        # 为目标会话构建反向映射
        for session in config.target_sessions:
            if session not in self._session_to_servers:
                self._session_to_servers[session] = []
            self._session_to_servers[session].append((config.server_id, config))

    def unregister_server(self, server_id: str):
        """从消息转发中取消注册服务器"""
        config = self._server_configs.pop(server_id, None)
        if config:
            # 从反向映射中移除
            for session in config.target_sessions:
                if session in self._session_to_servers:
                    remaining = [
                        (sid, cfg)
                        for sid, cfg in self._session_to_servers[session]
                        if sid != server_id
                    ]
                    if remaining:
                        self._session_to_servers[session] = remaining
                    else:
                        # 清理空列表，避免内存泄漏与 get_servers_for_session 返回空列表
                        del self._session_to_servers[session]

    def rename_server(self, old_id: str, new_id: str, config: ServerConfig):
        """Keep forwarding maps in sync when discovery changes the runtime id."""
        if old_id == new_id:
            self._server_configs[new_id] = config
            return

        self._server_configs.pop(old_id, None)
        self._server_configs[new_id] = config

        for session, servers in list(self._session_to_servers.items()):
            updated: list[tuple[str, ServerConfig]] = []
            for sid, cfg in servers:
                if sid == old_id:
                    updated.append((new_id, config))
                else:
                    updated.append((sid, cfg))
            self._session_to_servers[session] = updated

        for key in list(self._recently_forwarded):
            sid, content = key
            if sid == old_id:
                self._recently_forwarded[(new_id, content)] = (
                    self._recently_forwarded.pop(key)
                )

    async def handle_mc_message(self, server_id: str, msg: MCMessage) -> bool:
        """处理来自 MC 服务器的消息并转发到目标会话

        如果消息被转发则返回 True。
        """
        config = self._server_configs.get(server_id)
        if not config:
            return False

        # 检查是否已启用转发
        if msg.type == MessageType.MESSAGE_FORWARD:
            if not config.forward_chat_to_astrbot:
                return False
            # Suppress echo: if this message was recently forwarded FROM external
            content = msg.payload.get("content", "")
            echo_key = (server_id, content)
            now = time.time()
            if echo_key in self._recently_forwarded:
                if (
                    now - self._recently_forwarded[echo_key]
                    < self._echo_suppress_window
                ):
                    del self._recently_forwarded[echo_key]
                    return False
                del self._recently_forwarded[echo_key]
        elif msg.type in (MessageType.PLAYER_JOIN, MessageType.PLAYER_QUIT):
            if not config.forward_join_leave_to_astrbot:
                return False
        else:
            return False

        # 获取目标会话
        targets = config.target_sessions
        if not targets:
            return False

        # 格式化消息内容
        content = self._format_mc_message(msg, config)
        if not content:
            return False

        # 发送到每个目标会话
        await asyncio.gather(
            *(self._send_to_session(target_umo, content) for target_umo in targets)
        )

        return True

    def _format_mc_message(self, msg: MCMessage, config: ServerConfig) -> str:
        """
        格式化 MC 消息以转发到外部平台。

        参数:
            msg: 要格式化的 Minecraft 消息
            config: 包含格式模板的服务器配置

        返回:
            准备好转发的格式化消息字符串，如果消息类型不支持转发，则为空字符串。

        注意:
            支持 MESSAGE_FORWARD、PLAYER_JOIN 和 PLAYER_QUIT 消息类型。
        """
        if msg.type == MessageType.MESSAGE_FORWARD:
            player_name = msg.source.player_name if msg.source else "未知"
            content = msg.payload.get("content", "")
            return config.forward_chat_format.format(
                player=player_name, message=content
            )

        if msg.type in (MessageType.PLAYER_JOIN, MessageType.PLAYER_QUIT):
            player_name = msg.source.player_name if msg.source else "未知"
            server_name = msg.source.server_name if msg.source else ""
            online = msg.payload.get("onlineCount", 0)
            max_players = msg.payload.get("maxPlayers", 0)
            count_part = f" ({online}/{max_players})" if max_players else ""
            server_part = f" {server_name}" if server_name else "服务器"

            if msg.type == MessageType.PLAYER_JOIN:
                return f"🟢 {player_name} 加入了{server_part}{count_part}"

            reason = msg.payload.get("reason", "QUIT")
            reason_text = {
                "QUIT": "离开",
                "KICK": "被踢出",
                "TIMEOUT": "超时断开",
            }.get(reason, "离开")
            return f"🔴 {player_name} {reason_text}了{server_part}{count_part}"

        return ""

    async def _send_to_session(self, umo: str, content: str):
        """
        通过平台管理器发送消息到特定会话。

        参数:
            umo: 格式为 'platform:type:id' 的统一消息源
            content: 要发送的消息内容

        注意:
            解析 UMO 以查找目标平台并通过平台管理器发送。
            如果 UMO 格式无效或找不到平台，则记录警告。
        """
        try:
            # 创建消息链
            message_chain = MessageChain([Plain(text=content)])

            # 使用 Context 直接发送，内部会解析 UMO
            sent = await self.context.send_message(umo, message_chain)
            if not sent:
                logger.warning(f"[MessageBridge] 未找到平台: {umo}")

        except Exception as e:
            logger.error(f"[MessageBridge] 发送消息失败: {e}")

    async def handle_external_message(self, event: AstrMessageEvent) -> bool:
        """处理来自外部平台的消息并在需要时转发到 MC

        如果消息被转发则返回 True。
        """
        # 获取消息内容
        message_str = event.message_str
        umo = event.unified_msg_origin

        # 通过反向索引直接拿到绑定了该会话的服务器，避免 O(N×M) 线性扫描。
        matched_servers = self._session_to_servers.get(umo)
        if not matched_servers:
            return False

        # 这些值不随服务器变化，提前取一次即可。
        sender_name = event.get_sender_name()
        sender_id = event.get_sender_id()
        platform_name = event.get_platform_name()

        # 检查每个匹配的服务器配置
        any_forwarded = False
        for server_id, config in matched_servers:
            # 前缀为空时转发全部消息，否则检查前缀
            if config.auto_forward_prefix:
                if not message_str.startswith(config.auto_forward_prefix):
                    continue
                # 移除前缀
                content = message_str[len(config.auto_forward_prefix) :].strip()
            else:
                content = message_str.strip()

            if not content:
                continue

            # 发送到 MC 服务器
            server = self.server_manager.get_server(server_id)
            if server and server.connected:
                success = await server.ws_client.send_incoming_message(
                    platform=platform_name,
                    user_id=sender_id,
                    user_name=sender_name,
                    content=content,
                )

                if success:
                    # Track this message to suppress echo
                    echo_key = (server_id, content)
                    self._recently_forwarded[echo_key] = time.time()
                    # Clean up old entries
                    self._cleanup_recently_forwarded()
                    # Send feedback based on mark_option (only once)
                    if not any_forwarded:
                        await self._send_forward_feedback(event, config)
                    any_forwarded = True

        return any_forwarded

    async def _send_forward_feedback(
        self, event: AstrMessageEvent, config: ServerConfig
    ):
        """在消息转发成功后发送反馈

        Behavior by mark_option:
        - "none": do nothing
        - "emoji": only react with emoji, no text message
        - "text": only send text confirmation "✓ 消息已转发"
        """
        mark_option = config.mark_option

        if mark_option == "none":
            return

        elif mark_option == "emoji":
            # Only emoji reaction, no text
            await self._react_with_emoji(event)

        elif mark_option == "text":
            # Only text confirmation
            try:
                await event.send(MessageChain([Plain(text="✓ 消息已转发")]))
            except Exception:
                pass

    def _cleanup_recently_forwarded(self):
        """Clean up expired entries in the recently forwarded tracker"""
        now = time.time()
        expired = [
            k
            for k, t in self._recently_forwarded.items()
            if now - t > self._echo_suppress_window
        ]
        for k in expired:
            del self._recently_forwarded[k]

    async def _react_with_emoji(
        self, event: AstrMessageEvent, emoji_id: int = EMOJI_OK_GESTURE
    ):
        """
        使用 napcat/onebot API 对消息作出表情符号反应。

        参数:
            event: 要反应的消息事件
            emoji_id: 要使用的表情符号 ID (默认: EMOJI_OK_GESTURE)
                常见表情符号 ID:
                - EMOJI_OK_GESTURE (124): 👌
                - EMOJI_THUMBS_UP (76): 👍
                - EMOJI_LOVE (66): ❤️
                - EMOJI_ROSE (63): 🌹
        """
        platform_name = event.get_platform_name()

        # 仅 aiocqhttp (OneBot v11) 支持表情符号反应
        if platform_name != "aiocqhttp":
            return

        try:
            # 运行时惰性导入以避免循环依赖
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            if not isinstance(event, AiocqhttpMessageEvent):
                return

            # 获取机器人口端
            client = event.bot
            message_id = event.message_obj.message_id

            # 调用 napcat/onebot API 设置表情符号反应
            # API: set_msg_emoji_like
            payloads = {
                "message_id": int(message_id),
                "emoji_id": str(emoji_id),
            }

            await client.api.call_action("set_msg_emoji_like", **payloads)
            logger.debug(
                f"[MessageBridge] 已对消息 {message_id} 作出表情响应 {emoji_id}"
            )

        except Exception as e:
            # 表情符号反应失败，这不是关键错误
            logger.debug(f"[MessageBridge] 表情响应失败: {e}")

    def get_servers_for_session(self, umo: str) -> list[str]:
        """获取目标会话包含该 UMO 的服务器 ID 列表"""
        matched = self._session_to_servers.get(umo)
        if not matched:
            return []
        return [server_id for server_id, _ in matched]

    def strip_color_codes(self, text: str) -> str:
        """从文本中移除 Minecraft 颜色代码"""
        # 移除 § 后跟任意字符
        return re.sub(r"§[0-9a-fk-or]", "", text)
