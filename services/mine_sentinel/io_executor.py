"""Bounded ThreadPoolExecutor helper for MineSentinel IO-bound work.

PR9: 当 ``runtime_log.io_workers > 0`` 时，service 层会创建一个专用的有界
线程池，把 runtime log 读取、报告生成、hourly 扫描等 IO-bound 任务从
asyncio 默认线程池里隔离出来，避免与 AstrBot 其他插件/调度任务争用
默认池导致磁盘争用和 CPU 抢占。

用法::

    executor = build_io_executor(io_workers=2)
    io_runner = executor_runner(executor)  # 兼容 asyncio.to_thread 签名
    # ...io_runner(fn, *args) -> awaitable
    # 退出时调用 shutdown_io_executor(executor)

当 ``io_workers <= 0`` 时返回 None，调用方应回退到 ``asyncio.to_thread``。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

_THREAD_NAME_PREFIX = "mine-sentinel-io"


def build_io_executor(io_workers: int) -> ThreadPoolExecutor | None:
    """按配置创建有界线程池。``io_workers <= 0`` 时返回 None（沿用默认池）。"""
    if io_workers <= 0:
        return None
    return ThreadPoolExecutor(
        max_workers=max(1, int(io_workers)),
        thread_name_prefix=_THREAD_NAME_PREFIX,
    )


def executor_runner(
    executor: ThreadPoolExecutor | None,
) -> Callable[..., Awaitable[Any]]:
    """构造一个兼容 ``asyncio.to_thread`` 签名的 io_runner。

    - executor 为 None 时回退到 ``asyncio.to_thread``（沿用默认池）。
    - executor 非 None 时通过 ``loop.run_in_executor`` 提交到专用池。
    """

    async def _runner(fn: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        if executor is None:
            return await asyncio.to_thread(fn, *args)
        return await loop.run_in_executor(executor, fn, *args)

    return _runner


def shutdown_io_executor(executor: ThreadPoolExecutor | None) -> None:
    """关闭线程池（service.stop 时调用）。None 时为空操作。"""
    if executor is None:
        return
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        # Python 3.8 没有 cancel_futures 参数
        executor.shutdown(wait=False)
