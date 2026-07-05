"""Statistical anomaly detection for MineSentinel template counts.

在模板解析（Drain3）之上做轻量统计异常检测：

- EWMA 突增检测：对每个 (server_id, template_id) 维护近期计数基线，
  当前计数显著高于基线时触发突增告警。
- 分位数阈值：维护滑动窗口计数，超过历史 P95 阈值视为异常。
- 新模板检测：首次出现的模板（is_new_template）标记为 novelty。

设计原则：
- 零依赖、纯 Python，不引入 sklearn/torch。
- 在线学习，无需离线训练，冷启动友好。
- 输出异常分数（0~1）和触发原因，供 rules.py 提升 severity。
- 线程安全，可被 tailer 和 hourly job 共享。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TemplateStat:
    """单个 (server_id, template_id) 的统计状态。"""

    server_id: str
    template_id: str
    template: str = ""
    level: str = "INFO"
    # EWMA 状态
    ewma_count: float = 0.0
    ewma_var: float = 0.0
    last_update_ms: int = 0
    # 滑动窗口（最近 N 个 bucket 的计数）
    window: deque[int] = field(default_factory=lambda: deque(maxlen=50))
    # 累计统计
    total_count: int = 0
    first_seen_ms: int = 0
    last_seen_ms: int = 0
    # 异常状态
    last_anomaly_score: float = 0.0
    last_anomaly_reason: str = ""


@dataclass
class AnomalyResult:
    """单次检测的结果。"""

    is_anomaly: bool
    score: float  # 0~1
    reason: str
    stat: TemplateStat
    current_count: int
    baseline: float


class _Shard:
    """单个 server_id 的异常检测分片。

    PR9: 把原本全局共享的 _stats / _current_bucket / _observe_count 按 server_id
    分片，每个分片有自己的锁，使多服/Velocity 场景下 observe() 真正并行。
    """

    __slots__ = (
        "server_id",
        "stats",
        "current_bucket",
        "observe_count",
        "lock",
    )

    def __init__(self, server_id: str):
        self.server_id = server_id
        # template_id -> TemplateStat
        self.stats: dict[str, TemplateStat] = {}
        # template_id -> (bucket_index, count)
        self.current_bucket: dict[str, tuple[int, int]] = {}
        self.observe_count = 0
        self.lock = threading.Lock()


class TemplateAnomalyDetector:
    """模板计数突增检测器。

    PR9 锁分片：``_shards`` 按 ``server_id`` 维护独立的 stats/bucket/锁，
    不同服务器的 observe() 互不阻塞。snapshot/get_anomalies/flush_bucket
    会按 server_id 排序依次获取各分片锁，聚合跨服视图。

    Parameters
    ----------
    bucket_seconds:
        计数桶大小（秒），同一桶内的同模板日志累计计数。默认 60s。
    ewma_alpha:
        EWMA 平滑系数，越小越平滑（默认 0.3）。
    spike_threshold:
        突增倍数阈值，当前计数 > baseline * spike_threshold 触发（默认 3.0）。
    min_baseline_count:
        基线最小计数，低于此值不触发突增（避免冷启动误报，默认 5）。
    percentile:
        分位数阈值（0~1），超过历史 P{percentile*100} 视为异常（默认 0.95）。
    min_window_size:
        滑动窗口最小样本数，低于此值不触发分位数检测（默认 10）。
    max_templates_per_server:
        每个服务器最多跟踪多少个模板，超过时淘汰最久未见的（默认 500）。
    inactive_template_ttl_hours:
        超过此小时数未出现的模板被视为不活跃，会被清理（默认 24）。
    cleanup_interval:
        每隔多少次 observe 触发一次清理（默认 200，按分片独立计数）。
    """

    def __init__(
        self,
        bucket_seconds: int = 60,
        ewma_alpha: float = 0.3,
        spike_threshold: float = 3.0,
        min_baseline_count: int = 5,
        percentile: float = 0.95,
        min_window_size: int = 10,
        max_templates_per_server: int = 500,
        inactive_template_ttl_hours: int = 24,
        cleanup_interval: int = 200,
    ):
        # PR9: per-server 分片。_shards_guard 仅保护 _shards 字典本身，
        # 持有时间极短；observe() 在 _Shard.lock 下进行，互不阻塞。
        self._shards_guard = threading.Lock()
        self._shards: dict[str, _Shard] = {}
        self._bucket_seconds = max(1, bucket_seconds)
        self._ewma_alpha = max(0.01, min(1.0, ewma_alpha))
        self._spike_threshold = max(1.5, spike_threshold)
        self._min_baseline_count = max(1, min_baseline_count)
        self._percentile = max(0.5, min(0.999, percentile))
        self._min_window_size = max(3, min_window_size)
        self._max_templates_per_server = max(1, max_templates_per_server)
        self._inactive_ttl_ms = max(1, inactive_template_ttl_hours) * 3600 * 1000
        self._cleanup_interval = max(1, cleanup_interval)
        # 跨分片汇总的清理统计（仅在 _shards_guard 下读写，或 snapshot 聚合时读取）
        self._cleanup_count = 0
        self._last_cleanup_ms = 0

    def _shard_for(self, server_id: str) -> _Shard:
        """获取或创建指定 server_id 的分片。"""
        with self._shards_guard:
            shard = self._shards.get(server_id)
            if shard is None:
                shard = _Shard(server_id)
                self._shards[server_id] = shard
            return shard

    def observe(
        self,
        server_id: str,
        template_id: str,
        template: str = "",
        level: str = "INFO",
        timestamp_ms: int | None = None,
    ) -> AnomalyResult:
        """记录一条日志，更新统计并返回当前异常评估。线程安全（per-server 锁）。"""
        if timestamp_ms is None:
            timestamp_ms = int(time.time() * 1000)
        bucket_index = timestamp_ms // (self._bucket_seconds * 1000)
        shard = self._shard_for(server_id)

        with shard.lock:
            stat = shard.stats.get(template_id)
            if stat is None:
                stat = TemplateStat(
                    server_id=server_id,
                    template_id=template_id,
                    template=template,
                    level=level,
                    first_seen_ms=timestamp_ms,
                    last_seen_ms=timestamp_ms,
                    last_update_ms=timestamp_ms,
                )
                shard.stats[template_id] = stat
                # 新模板创建时检查 per-server 上限
                self._enforce_max_templates(shard, timestamp_ms)

            # 桶切换：把上一个桶的计数写入滑动窗口并用它更新 EWMA，
            # 然后重置当前桶。注意：当前桶的 count 不更新 EWMA，
            # 否则突增桶会拉高自己的基线导致 ratio 不够。
            bucket_state = shard.current_bucket.get(template_id)
            current_count = 0
            if bucket_state is None or bucket_state[0] != bucket_index:
                if bucket_state is not None and bucket_state[1] > 0:
                    self._update_ewma(stat, bucket_state[1])
                    stat.window.append(bucket_state[1])
                current_count = 1
                shard.current_bucket[template_id] = (bucket_index, current_count)
            else:
                current_count = bucket_state[1] + 1
                shard.current_bucket[template_id] = (bucket_index, current_count)

            stat.total_count += 1
            stat.last_seen_ms = timestamp_ms
            if template:
                stat.template = template
            stat.level = level

            # 周期性清理不活跃模板，防止长期运行状态膨胀（按分片独立计数）
            shard.observe_count += 1
            if shard.observe_count % self._cleanup_interval == 0:
                self._cleanup_inactive(shard, timestamp_ms)

            return self._evaluate(stat, current_count, timestamp_ms)

    def _update_ewma(self, stat: TemplateStat, bucket_count: int):
        """用上一个 bucket 的计数更新 EWMA（不在 observe 时逐条更新）。"""
        alpha = self._ewma_alpha
        prev_ewma = stat.ewma_count
        stat.ewma_count = alpha * bucket_count + (1 - alpha) * prev_ewma
        diff = bucket_count - stat.ewma_count
        stat.ewma_var = (1 - alpha) * (stat.ewma_var + alpha * diff * diff)

    def _evaluate(
        self,
        stat: TemplateStat,
        current_count: int,
        now_ms: int,
    ) -> AnomalyResult:
        """评估当前 bucket 计数是否异常。EWMA 已由 bucket 切换时更新。"""
        stat.last_update_ms = now_ms
        baseline = stat.ewma_count  # 历史基线（不含当前 bucket）
        score = 0.0
        reason = ""

        # 检测 1：EWMA 突增
        # 要求模板至少积累 min_baseline_count 次样本，避免冷启动误报
        if (
            stat.total_count >= self._min_baseline_count
            and baseline > 0
            and current_count >= baseline * self._spike_threshold
        ):
            ratio = current_count / max(baseline, 0.1)
            score = max(score, min(1.0, (ratio - 1.0) / 10.0))
            reason = f"ewma_spike: count={current_count} baseline={baseline:.1f} ratio={ratio:.1f}"

        # 检测 2：分位数阈值
        if len(stat.window) >= self._min_window_size:
            threshold = self._percentile_value(list(stat.window), self._percentile)
            if threshold > 0 and current_count > threshold * 1.5:
                ratio = current_count / max(threshold, 0.1)
                pct_score = min(1.0, (ratio - 1.0) / 5.0)
                if pct_score > score:
                    score = pct_score
                    reason = (
                        f"percentile_spike: count={current_count} "
                        f"p{int(self._percentile * 100)}={threshold:.1f} ratio={ratio:.1f}"
                    )

        # 检测 3：新模板（首次出现）
        if stat.total_count == 1:
            score = max(score, 0.3)
            if not reason:
                reason = "new_template: first occurrence"

        is_anomaly = score >= 0.5
        stat.last_anomaly_score = score
        stat.last_anomaly_reason = reason

        return AnomalyResult(
            is_anomaly=is_anomaly,
            score=score,
            reason=reason,
            stat=stat,
            current_count=current_count,
            baseline=baseline,
        )

    def _percentile_value(self, values: list[int], percentile: float) -> float:
        """简单分位数计算（无 numpy 依赖）。"""
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        idx = int(len(sorted_vals) * percentile)
        if idx >= len(sorted_vals):
            idx = len(sorted_vals) - 1
        return float(sorted_vals[idx])

    def _cleanup_inactive(self, shard: _Shard, now_ms: int):
        """清理分片内超过 TTL 未活跃的模板，防止长期运行状态膨胀。"""
        cutoff = now_ms - self._inactive_ttl_ms
        expired_keys = [
            template_id
            for template_id, stat in shard.stats.items()
            if stat.last_seen_ms < cutoff
        ]
        for template_id in expired_keys:
            shard.stats.pop(template_id, None)
            shard.current_bucket.pop(template_id, None)
        if expired_keys:
            self._cleanup_count += len(expired_keys)
            self._last_cleanup_ms = now_ms

    def _enforce_max_templates(self, shard: _Shard, now_ms: int):
        """当分片内模板数超过上限时，淘汰最久未见的。"""
        if len(shard.stats) <= self._max_templates_per_server:
            return
        # 按 last_seen_ms 升序，淘汰最旧的
        sorted_items = sorted(shard.stats.items(), key=lambda x: x[1].last_seen_ms)
        evict_count = len(sorted_items) - self._max_templates_per_server
        for template_id, _ in sorted_items[:evict_count]:
            shard.stats.pop(template_id, None)
            shard.current_bucket.pop(template_id, None)
        self._cleanup_count += evict_count
        self._last_cleanup_ms = now_ms

    def _iter_shards_sorted(self) -> list[tuple[str, _Shard]]:
        """返回按 server_id 排序的分片列表（snapshot/aggregate 用，避免死锁）。"""
        with self._shards_guard:
            return sorted(self._shards.items())

    def get_anomalies(self, min_score: float = 0.5) -> list[TemplateStat]:
        """返回当前所有异常模板（score >= min_score）。"""
        result: list[TemplateStat] = []
        for _, shard in self._iter_shards_sorted():
            with shard.lock:
                result.extend(
                    stat
                    for stat in shard.stats.values()
                    if stat.last_anomaly_score >= min_score
                )
        return result

    def snapshot(self) -> dict[str, Any]:
        """返回所有模板统计快照，用于 LLM 证据和报告。"""
        per_server: dict[str, int] = {}
        total_count = 0
        all_stats: list[TemplateStat] = []
        for server_id, shard in self._iter_shards_sorted():
            with shard.lock:
                shard_stats = list(shard.stats.values())
            per_server[server_id] = len(shard_stats)
            total_count += len(shard_stats)
            all_stats.extend(shard_stats)
        return {
            "template_count": total_count,
            "per_server_count": per_server,
            "cleanup_count": self._cleanup_count,
            "last_cleanup_ms": self._last_cleanup_ms,
            "anomalies": [
                {
                    "server_id": s.server_id,
                    "template_id": s.template_id,
                    "template": s.template,
                    "level": s.level,
                    "total_count": s.total_count,
                    "ewma_count": round(s.ewma_count, 2),
                    "current_score": round(s.last_anomaly_score, 3),
                    "reason": s.last_anomaly_reason,
                    "first_seen_ms": s.first_seen_ms,
                    "last_seen_ms": s.last_seen_ms,
                }
                for s in sorted(
                    all_stats,
                    key=lambda x: x.last_anomaly_score,
                    reverse=True,
                )[:20]
            ],
        }

    def flush_bucket(self, server_id: str | None = None, now_ms: int | None = None):
        """手动把当前桶计数写入滑动窗口（用于 hourly 切换或周期报告前）。"""
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        bucket_index = now_ms // (self._bucket_seconds * 1000)
        if server_id is not None:
            shard = self._shard_for(server_id)
            with shard.lock:
                self._flush_shard_bucket(shard, bucket_index)
            return
        for _, shard in self._iter_shards_sorted():
            with shard.lock:
                self._flush_shard_bucket(shard, bucket_index)

    def _flush_shard_bucket(self, shard: _Shard, bucket_index: int):
        for template_id, state in list(shard.current_bucket.items()):
            if state[0] != bucket_index and state[1] > 0:
                stat = shard.stats.get(template_id)
                if stat:
                    stat.window.append(state[1])
                shard.current_bucket[template_id] = (bucket_index, 0)


# 全局单例
_global_detector: TemplateAnomalyDetector | None = None
_global_lock = threading.Lock()


def get_anomaly_detector(
    max_templates_per_server: int | None = None,
    inactive_template_ttl_hours: int | None = None,
    cleanup_interval: int | None = None,
) -> TemplateAnomalyDetector:
    """获取全局异常检测器单例。

    首次调用可通过参数覆盖默认值（来自 config）；后续调用的参数被忽略
    （单例已创建）。service 层在初始化时传入 config，其他调用方无参获取。
    """
    global _global_detector
    if _global_detector is None:
        with _global_lock:
            if _global_detector is None:
                kwargs: dict[str, Any] = {}
                if max_templates_per_server is not None:
                    kwargs["max_templates_per_server"] = max_templates_per_server
                if inactive_template_ttl_hours is not None:
                    kwargs["inactive_template_ttl_hours"] = inactive_template_ttl_hours
                if cleanup_interval is not None:
                    kwargs["cleanup_interval"] = cleanup_interval
                _global_detector = TemplateAnomalyDetector(**kwargs)
    return _global_detector


def reset_anomaly_detector() -> None:
    """重置全局单例（仅供测试使用，避免跨测试污染）。"""
    global _global_detector
    with _global_lock:
        _global_detector = None
