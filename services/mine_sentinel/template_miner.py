"""Drain3-based log template miner for MineSentinel.

把原始日志行解析成 (template_id, template, params) 三元组，供 loop_filter 去重、
统计异常检测和 LLM 证据总结使用。设计目标：

- 可选依赖：drain3 未安装时自动降级为旧 fingerprint 方案，不破坏现有行为。
- 在线学习：每条日志都会更新 parse tree，模板会随样本增多而收敛。
- 状态持久化：可选把模板树存盘，重启后继续学习（默认关闭，避免 IO）。
- 线程安全：drain3 的 TemplateMiner 不是线程安全的，这里加锁保护。

使用方式::

    miner = LogTemplateMiner()
    result = miner.parse("[14:02:11 INFO]: Steve joined the game")
    # result = ParsedTemplate(
    #     template_id="3",
    #     template="<*> INFO]: <*> joined the game",
    #     params=["14:02:11", "Steve"],
    #     is_new_template=False,
    #     cluster_size=2,
    # )
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

try:
    from drain3 import TemplateMiner
    from drain3.template_miner_config import TemplateMinerConfig

    _DRAIN3_AVAILABLE = True
except ImportError:  # pragma: no cover - drain3 是可选依赖
    _DRAIN3_AVAILABLE = False
    TemplateMiner = None  # type: ignore[assignment,misc]
    TemplateMinerConfig = None  # type: ignore[assignment,misc]


@dataclass
class ParsedTemplate:
    """单条日志的模板解析结果。"""

    template_id: str
    """Drain 分配的模板簇 ID，同一模板的日志共享此 ID。"""

    template: str
    """参数化后的模板字符串，如 ``<*> INFO]: <*> joined the game``。"""

    params: list[str] = field(default_factory=list)
    """从原始日志中提取的变量值（时间戳、玩家名、坐标等），按出现顺序排列。"""

    is_new_template: bool = False
    """本次解析是否创建了新模板簇。"""

    cluster_size: int = 0
    """该模板簇目前已积累的样本数。"""

    fallback: bool = False
    """True 表示 drain3 不可用，使用了降级 fingerprint 方案。"""

    fallback_fingerprint: str = ""
    """降级模式下的 fingerprint（兼容旧 loop_filter）。"""


class LogTemplateMiner:
    """Drain3 包装层，提供线程安全的模板解析。

    Parameters
    ----------
    persistence_path:
        若指定，会把模板树状态存盘到此路径，重启后加载继续学习。
        默认 None 表示纯内存模式（重启后重新学习，对 Minecraft 日志够用）。
    sim_th:
        模板相似度阈值，越低越容易合并（默认 0.4，drain3 推荐值）。
    max_depth:
        parse tree 最大深度（默认 4）。
    max_children:
        每个内部节点最大子节点数（默认 100）。
    """

    def __init__(
        self,
        persistence_path: str | None = None,
        sim_th: float = 0.4,
        max_depth: int = 4,
        max_children: int = 100,
    ):
        self._lock = threading.Lock()
        self._persistence_path = persistence_path
        self._available = _DRAIN3_AVAILABLE
        self._miner: Any = None

        if not _DRAIN3_AVAILABLE:
            logger.warning(
                "[MineSentinel] drain3 未安装，模板解析降级为 fingerprint 方案。"
                "建议 pip install drain3 启用模板驱动的异常检测。"
            )
            return

        config = TemplateMinerConfig()
        config.sim_th = sim_th
        config.max_depth = max_depth
        config.max_children = max_children
        # 参数提取：开启后 match() 能返回变量值
        config.parametric_name = True

        persistence = None
        if persistence_path:
            try:
                from drain3.file_persistence import FilePersistence

                persistence = FilePersistence(persistence_path)
            except ImportError:
                logger.warning(
                    "[MineSentinel] drain3 FilePersistence 不可用，回退到内存模式。"
                )

        self._miner = TemplateMiner(config=config, persistence_handler=persistence)
        if persistence:
            try:
                self._miner.load_state(persistence_path)
                logger.info(
                    f"[MineSentinel] 模板树已从 {persistence_path} 加载，"
                    f"共 {len(self._miner.drain.id_to_cluster)} 个模板簇。"
                )
            except Exception as exc:
                logger.warning(
                    f"[MineSentinel] 加载模板树状态失败: {exc}，将从头学习。"
                )

    @property
    def available(self) -> bool:
        return self._available

    def parse(self, line: str) -> ParsedTemplate:
        """解析一条日志，返回模板信息。线程安全。"""
        if not self._available or self._miner is None:
            # 降级：返回 fallback fingerprint，调用方用旧逻辑去重
            from .runtime_log import _fingerprint

            fp = _fingerprint(line)
            return ParsedTemplate(
                template_id=fp,
                template=line,
                params=[],
                is_new_template=False,
                cluster_size=0,
                fallback=True,
                fallback_fingerprint=fp,
            )

        with self._lock:
            result = self._miner.add_log_message(line)
            cluster_id = str(result.get("cluster_id") or "")
            template = str(result.get("template_mined") or line)
            cluster_size = int(result.get("cluster_size") or 0)
            change_type = str(result.get("change_type") or "")
            is_new = change_type == "cluster_created"

            params: list[str] = []
            try:
                params = list(self._miner.get_parameter_list(line, extract_parameters=True))
            except Exception:
                # 参数提取失败不影响模板去重
                pass

            return ParsedTemplate(
                template_id=cluster_id,
                template=template,
                params=params,
                is_new_template=is_new,
                cluster_size=cluster_size,
                fallback=False,
            )

    def match(self, line: str) -> ParsedTemplate | None:
        """只匹配不学习。若模板未见过的返回 None。"""
        if not self._available or self._miner is None:
            return None
        with self._lock:
            result = self._miner.match(line)
            if result is None:
                return None
            return ParsedTemplate(
                template_id=str(result.cluster_id),
                template=result.get_template(),
                params=[],
                is_new_template=False,
                cluster_size=result.get_size(),
                fallback=False,
            )

    def snapshot(self) -> dict[str, Any]:
        """返回所有已学习模板的快照，用于报告和 LLM 证据。"""
        if not self._available or self._miner is None:
            return {"available": False, "clusters": {}}
        with self._lock:
            clusters: dict[str, dict[str, Any]] = {}
            for cluster_id, cluster in self._miner.drain.id_to_cluster.items():
                clusters[str(cluster_id)] = {
                    "template": cluster.get_template(),
                    "size": cluster.get_size(),
                }
            return {"available": True, "clusters": clusters}

    def save_state(self) -> bool:
        """手动触发模板树存盘。返回是否成功。"""
        if not self._available or self._miner is None or not self._persistence_path:
            return False
        with self._lock:
            try:
                self._miner.save_state(self._persistence_path)
                return True
            except Exception as exc:
                logger.warning(f"[MineSentinel] 模板树存盘失败: {exc}")
                return False


# 全局单例：整个进程共享一棵 parse tree
_global_miner: LogTemplateMiner | None = None
_global_lock = threading.Lock()


def get_template_miner() -> LogTemplateMiner:
    """获取全局 LogTemplateMiner 单例。"""
    global _global_miner
    if _global_miner is None:
        with _global_lock:
            if _global_miner is None:
                _global_miner = LogTemplateMiner()
    return _global_miner
