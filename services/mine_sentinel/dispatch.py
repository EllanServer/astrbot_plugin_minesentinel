"""MineSentinel report dispatch orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

from .delivery import MineSentinelDelivery
from .models import ObservationRecord
from .routing import MineSentinelTargetRouter


class MineSentinelReportDispatcher:
    """Send report text/files to the sessions derived from observation records."""

    def __init__(
        self,
        delivery: MineSentinelDelivery,
        router: MineSentinelTargetRouter,
        error_sink: Callable[[str], None] | None = None,
    ):
        self.delivery = delivery
        self.router = router
        self.error_sink = error_sink

    def records_by_session(
        self,
        records: list[ObservationRecord],
        include_server_targets: bool = True,
        include_report_targets: bool = True,
    ) -> dict[str, list[ObservationRecord]]:
        return self.router.records_by_session(
            records,
            include_server_targets=include_server_targets,
            include_report_targets=include_report_targets,
        )

    async def send_to_target_sessions(
        self,
        text: str,
        records: list[ObservationRecord],
        current_session: str = "",
        include_server_targets: bool = True,
        include_report_targets: bool = True,
        image: BytesIO | None = None,
        file_path: Path | None = None,
    ) -> bool:
        current_resolved = self._resolve_session(current_session)
        seen: set[str] = set()
        targets: list[str] = []
        for umo in self.router.sessions_for_records(
            records,
            current_session,
            include_server_targets=include_server_targets,
            include_report_targets=include_report_targets,
        ):
            resolved = self._resolve_session(umo)
            if resolved and resolved == current_resolved:
                continue
            if resolved and resolved in seen:
                continue
            seen.add(resolved or umo)
            targets.append(umo)
        if not targets:
            return False
        # 并发投递到各 session，Semaphore 限流避免瞬时压力过大；
        # 单个 session 失败不影响其他 session 的投递。
        semaphore = asyncio.Semaphore(4)

        async def _send_one(target_umo: str) -> bool:
            async with semaphore:
                try:
                    return await self.send_report(
                        target_umo, text, image=image, file_path=file_path
                    )
                except Exception as exc:
                    if self.error_sink:
                        self.error_sink(f"投递到 {target_umo} 失败: {exc}")
                    return False

        results = await asyncio.gather(*(_send_one(umo) for umo in targets))
        return any(results)

    async def send_report(
        self,
        umo: str,
        text: str,
        image: BytesIO | None = None,
        file_path: Path | None = None,
    ) -> bool:
        send_report = getattr(self.delivery, "send_report", None)
        if callable(send_report):
            sent = await send_report(umo, text, image, file_path)
        else:
            sent = await self.delivery.send_message(umo, text, file_path)
        if not sent:
            self._capture_delivery_error()
        return sent

    async def send_file(self, umo: str, file_path: Path):
        sent = await self.delivery.send_file(umo, file_path)
        if not sent:
            self._capture_delivery_error()

    def _capture_delivery_error(self):
        if self.error_sink and self.delivery.last_error:
            self.error_sink(self.delivery.last_error)

    def _resolve_session(self, umo: str) -> str:
        resolver = getattr(self.delivery, "resolve_session", None)
        if callable(resolver):
            return resolver(umo)
        return umo
