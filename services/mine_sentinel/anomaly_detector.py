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


class TemplateAnomalyDetector:
    """模板计数突增检测器。

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
    """

    def __init__(
        self,
        bucket_seconds: int = 60,
        ewma_alpha: float = 0.3,
        spike_threshold: float = 3.0,
        min_baseline_count: int = 5,
        percentile: float = 0.95,
        min_window_size: int = 10,
    ):
        self._lock = threading.Lock()
        self._bucket_seconds = max(1, bucket_seconds)
        self._ewma_alpha = max(0.01, min(1.0, ewma_alpha))
        self._spike_threshold = max(1.5, spike_threshold)
        self._min_baseline_count = max(1, min_baseline_count)
        self._percentile = max(0.5, min(0.999, percentile))
        self._min_window_size = max(3, min_window_size)
        # (server_id, template_id) -> TemplateStat
        self._stats: dict[tuple[str, str], TemplateStat] = {}
        # 当前桶的计数：(server_id, template_id) -> (bucket_index, count)
        self._current_bucket: dict[tuple[str, str], tuple[int, int]] = {}

    def observe(
        self,
        server_id: str,
        template_id: str,
        template: str = "",
        level: str = "INFO",
        timestamp_ms: int | None = None,
    ) -> AnomalyResult:
        """记录一条日志，更新统计并返回当前异常评估。"""
        if timestamp_ms is None:
            timestamp_ms = int(time.time() * 1000)
        bucket_index = timestamp_ms // (self._bucket_seconds * 1000)
        key = (server_id, template_id)

        with self._lock:
            stat = self._stats.get(key)
            if stat is None:
                stat = TemplateStat(
                    server_id=server_id,
                    template_id=template_id,
                    template=template,
                    level=level,
                    first_seen_ms=timestamp_ms,
                    last_update_ms=timestamp_ms,
                )
                self._stats[key] = stat

            # 桶切换：把上一个桶的计数写入滑动窗口并用它更新 EWMA，
            # 然后重置当前桶。注意：当前桶的 count 不更新 EWMA，
            # 否则突增桶会拉高自己的基线导致 ratio 不够。
            bucket_state = self._current_bucket.get(key)
            current_count = 0
            if bucket_state is None or bucket_state[0] != bucket_index:
                if bucket_state is not None and bucket_state[1] > 0:
                    self._update_ewma(stat, bucket_state[1])
                    stat.window.append(bucket_state[1])
                current_count = 1
                self._current_bucket[key] = (bucket_index, current_count)
            else:
                current_count = bucket_state[1] + 1
                self._current_bucket[key] = (bucket_index, current_count)

            stat.total_count += 1
            stat.last_seen_ms = timestamp_ms
            if template:
                stat.template = template
            stat.level = level

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

    def get_anomalies(self, min_score: float = 0.5) -> list[TemplateStat]:
        """返回当前所有异常模板（score >= min_score）。"""
        with self._lock:
            return [
                stat
                for stat in self._stats.values()
                if stat.last_anomaly_score >= min_score
            ]

    def snapshot(self) -> dict[str, Any]:
        """返回所有模板统计快照，用于 LLM 证据和报告。"""
        with self._lock:
            return {
                "template_count": len(self._stats),
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
                        self._stats.values(),
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
        with self._lock:
            for key, state in list(self._current_bucket.items()):
                if server_id and key[0] != server_id:
                    continue
                if state[0] != bucket_index and state[1] > 0:
                    stat = self._stats.get(key)
                    if stat:
                        stat.window.append(state[1])
                    self._current_bucket[key] = (bucket_index, 0)


# 全局单例
_global_detector: TemplateAnomalyDetector | None = None
_global_lock = threading.Lock()


def get_anomaly_detector() -> TemplateAnomalyDetector:
    """获取全局异常检测器单例。"""
    global _global_detector
    if _global_detector is None:
        with _global_lock:
            if _global_detector is None:
                _global_detector = TemplateAnomalyDetector()
    return _global_detector
