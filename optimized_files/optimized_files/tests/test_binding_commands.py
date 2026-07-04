from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

try:
    from astrbot_plugin_minecraft_adapter.handlers.binding_commands import (
        BindingCommandHandler,
    )
except ModuleNotFoundError:
    from handlers.binding_commands import BindingCommandHandler


class BindingCommandHandlerTests(unittest.TestCase):
    def test_bind_requires_player_id(self):
        handler = _handler()
        result = asyncio.run(_collect(handler.handle_bind(_FakeEvent(), "")))

        self.assertEqual(result, ["❌ 请指定要绑定的游戏ID"])

    def test_bind_waits_for_server_selection(self):
        handler = _handler(resolve=lambda *_args, **_kwargs: (None, "pick a server"))
        result = asyncio.run(_collect(handler.handle_bind(_FakeEvent(), "Alice")))

        self.assertEqual(result, ["pick a server"])

    def test_bind_respects_disabled_config(self):
        handler = _handler(
            get_config=lambda sid: SimpleNamespace(bind_enable=False),
        )
        result = asyncio.run(_collect(handler.handle_bind(_FakeEvent(), "Alice")))

        self.assertEqual(result, ["❌ 绑定功能未启用"])

    def test_bind_success_passes_identity_and_server(self):
        binding = _BindingService(bind_result=(True, "绑定成功"))
        handler = _handler(binding_service=binding)

        result = asyncio.run(_collect(handler.handle_bind(_FakeEvent(), "Alice")))

        self.assertEqual(result, ["✅ 绑定成功"])
        self.assertEqual(
            binding.bound,
            {
                "platform": "qq",
                "user_id": "user-1",
                "mc_player_name": "Alice",
                "server_id": "survival",
            },
        )

    def test_unbind_uses_failure_prefix(self):
        binding = _BindingService(unbind_result=(False, "未绑定"))
        handler = _handler(binding_service=binding)

        result = asyncio.run(_collect(handler.handle_unbind(_FakeEvent())))

        self.assertEqual(result, ["❌ 未绑定"])


def _handler(
    binding_service=None,
    get_config=None,
    resolve=None,
):
    return BindingCommandHandler(
        binding_service or _BindingService(),
        get_config or (lambda sid: SimpleNamespace(bind_enable=True)),
        resolve or (lambda *_args, **_kwargs: (_server(), "")),
    )


def _server():
    return SimpleNamespace(server_id="survival")


async def _collect(generator):
    return [item async for item in generator]


class _BindingService:
    def __init__(
        self,
        bind_result=(True, "ok"),
        unbind_result=(True, "ok"),
    ):
        self.bind_result = bind_result
        self.unbind_result = unbind_result
        self.bound = None
        self.unbound = None

    async def bind(self, **kwargs):
        self.bound = kwargs
        return self.bind_result

    async def unbind(self, **kwargs):
        self.unbound = kwargs
        return self.unbind_result


class _FakeEvent:
    unified_msg_origin = "group:test"

    def plain_result(self, text):
        return text

    def get_platform_name(self):
        return "qq"

    def get_sender_id(self):
        return "user-1"


if __name__ == "__main__":
    unittest.main()
