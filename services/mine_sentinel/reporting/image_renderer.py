"""Pillow image renderer for MineSentinel reports."""

from __future__ import annotations

import asyncio
import re
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from ...rendering.fonts import FontProvider
from ...rendering.image import save_png
from . import text_renderer as text_report
from .incident_format import (
    dedupe_key as _dedupe_key,
    format_duration as _format_duration,
    format_time_window as _format_time_window,
    incident_time_text as _incident_time_text,
    incident_title as _incident_title,
    quiet_window_text as _quiet_window_text,
)
from .incidents import IncidentGroup, IncidentGrouper, IssuePolicy, issue_sort_key
from .labels import DEFAULT_LABELS
from .presentation import ReportPresentationBuilder


class MineSentinelReportImageRenderer:
    """Render an incident-level MineSentinel report as a QQ-friendly PNG."""

    WIDTH = 1200
    OUTER_PAD = 38
    CARD_PAD = 30
    CONTENT_W = WIDTH - OUTER_PAD * 2
    BG = "#f3f6fa"
    CARD = "#ffffff"
    TEXT = "#172033"
    MUTED = "#667085"
    BORDER = "#dce3ec"
    BLUE = "#3157d5"
    CYAN = "#0e7490"
    GREEN = "#15803d"
    AMBER = "#b45309"
    RED = "#c2413b"
    HEADER = "#18243a"
    HEADER_MUTED = "#b8c4d6"

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.font_provider = FontProvider(cache_dir / "fonts")
        self._font_cache: dict[tuple[int, str], object] = {}
        self._assets_ready = False
        self._labels = DEFAULT_LABELS
        self._presentation_builder = ReportPresentationBuilder(
            issue_policy=IssuePolicy(),
            incident_grouper=IncidentGrouper(),
        )

    async def render(
        self,
        report: dict,
        total_count: int,
        dedupe_count: int,
        unique_players: int,
    ) -> BytesIO:
        await self._ensure_assets()
        # The actual drawing is pure CPU-bound PIL work on a large canvas;
        # run it off the event loop so heartbeats/message dispatch are not
        # blocked while a report image (often 20000+px tall) is rendered.
        return await asyncio.to_thread(
            self._draw_report,
            report,
            total_count,
            dedupe_count,
            unique_players,
        )

    def _draw_report(
        self,
        report: dict,
        total_count: int,
        dedupe_count: int,
        unique_players: int,
    ) -> BytesIO:
        presentation = self._presentation_builder.build(
            report,
            total_count,
            dedupe_count,
            unique_players,
        )
        incident_groups, observation_groups = text_report._split_incident_groups(
            presentation.incidents
        )
        category_observations = text_report._category_observation_lines(report)
        high_risk_count = text_report._high_risk_count(incident_groups)
        manual_review_count = text_report._manual_review_count(incident_groups)
        player_count = text_report._player_count(report, presentation.unique_players)
        status_label, status_color = _report_status(
            len(incident_groups), high_risk_count
        )

        canvas = _ReportCanvas(self)
        canvas.header(
            "MineSentinel 巡检报告",
            f"{_format_servers(report)} · {_format_time_window(report)}",
            status_label,
            status_color,
        )
        canvas.stats(
            [
                ("重点事件", str(len(incident_groups)), "#fff7ed", self.AMBER),
                ("高风险事件", str(high_risk_count), "#fef2f2", self.RED),
                ("待人工复核", str(manual_review_count), "#eff6ff", self.BLUE),
                (
                    "一般观察",
                    str(len(observation_groups) + len(category_observations)),
                    "#ecfdf5",
                    self.GREEN,
                ),
            ]
        )
        canvas.section_title("整体情况")
        canvas.summary_panel(
            text_report._overall_lines(
                report,
                player_count,
                len(incident_groups),
                len(observation_groups) + len(category_observations),
                high_risk_count,
                manual_review_count,
                _format_duration(report),
                incident_groups,
            ),
            status_color,
        )

        canvas.section_title("重点事件总结")
        if incident_groups:
            for index, group in enumerate(incident_groups[:8], 1):
                canvas.incident_card(index, group)
            if len(incident_groups) > 8:
                canvas.info_note(
                    f"另有 {len(incident_groups) - 8} 个重点事件未在图片展开；"
                    "图片已优先展示风险最高的 8 个，完整证据见附件。"
                )
            canvas.info_note(_quiet_window_text(report, incident_groups))
        else:
            canvas.info_note("本窗口未发现需要管理员优先处理的事故或玩家问题。")

        canvas.section_title("聊天与社区观察")
        if observation_groups:
            for index, group in enumerate(observation_groups[:6], 1):
                canvas.incident_card(index, group, label="观察", observation=True)
            if category_observations:
                canvas.bullet_list(category_observations)
        else:
            canvas.bullet_list(text_report._observation_lines(report, []))

        canvas.section_title("玩家问题/投诉识别")
        canvas.bullet_list(
            text_report._player_problem_lines(
                presentation.issues,
                incident_groups + observation_groups,
            )
        )

        canvas.section_title("风险提醒与建议处理")
        canvas.bullet_list(
            text_report._risk_lines(
                report,
                presentation.issues,
                presentation.actionable_issues,
                len(incident_groups),
                incident_groups,
                observation_groups,
            )
        )
        canvas.subsection_title("处置顺序")
        canvas.numbered_list(text_report._action_lines(presentation.issues))

        duration = _format_duration(report)
        canvas.report_footer(
            [
                f"证据：共 {presentation.total_count} 条观察，涉及玩家 {player_count} 人。",
                f"本报告基于{text_report._duration_with_prefix('完整', duration)}"
                "运行日志、玩家事件和结构化分类生成。",
            ]
        )
        return canvas.output()

    async def _ensure_assets(self):
        if self._assets_ready:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        await self.font_provider.ensure_cached()
        self._assets_ready = True

    def font(self, size: int, weight: str = "regular"):
        key = (size, weight)
        if key not in self._font_cache:
            self._font_cache[key] = self.font_provider.font(size, weight)
        return self._font_cache[key]

    def issue_title(self, issue: dict[str, Any]) -> str:
        chat_labels = [
            str(label).strip()
            for label in (issue.get("chat_labels") or [])
            if str(label).strip()
        ]
        if chat_labels:
            return "、".join(chat_labels[:4])
        ops_subtypes = [
            str(label).strip()
            for label in (issue.get("ops_subtypes") or [])
            if str(label).strip()
        ]
        if ops_subtypes:
            return "、".join(ops_subtypes[:4])
        return self._labels.issue_title(issue)


class _ReportCanvas:
    def __init__(self, renderer: MineSentinelReportImageRenderer):
        self.r = renderer
        self.image = Image.new("RGB", (renderer.WIDTH, 1800), renderer.BG)
        self.draw = ImageDraw.Draw(self.image)
        self.y = renderer.OUTER_PAD
        self.section_index = 0

    def output(self) -> BytesIO:
        bottom = min(self.image.height, self.y + self.r.OUTER_PAD)
        # 显式管理 crop 产物与底层 image 的生命周期，避免 Pillow Image 对象泄漏。
        cropped = self.image.crop((0, 0, self.r.WIDTH, bottom))
        try:
            return save_png(cropped)
        finally:
            cropped.close()
            self.image.close()

    def header(
        self,
        title: str,
        subtitle: str,
        status_label: str,
        status_color: str,
    ):
        x = self.r.OUTER_PAD
        w = self.r.CONTENT_W
        h = 182
        self._ensure(h + 20)
        self.draw.rounded_rectangle(
            (x, self.y, x + w, self.y + h),
            radius=8,
            fill=self.r.HEADER,
        )
        self.draw.rectangle((x, self.y, x + w, self.y + 7), fill=self.r.BLUE)
        self.draw.text(
            (x + 34, self.y + 28),
            "MINECRAFT OPERATIONS · AI MONITORING",
            font=self.r.font(19, "medium"),
            fill="#80d4e8",
        )
        status_w = max(
            188,
            int(
                self.draw.textlength(
                    status_label,
                    font=self.r.font(23, "semibold"),
                )
            )
            + 48,
        )
        status_x = x + w - status_w - 32
        self.draw.rounded_rectangle(
            (status_x, self.y + 27, status_x + status_w, self.y + 73),
            radius=6,
            fill=status_color,
        )
        self.draw.text(
            (status_x + 22, self.y + 37),
            status_label,
            font=self.r.font(23, "semibold"),
            fill="#ffffff",
        )
        self._fit_text(
            title,
            x + 34,
            self.y + 66,
            w - 68,
            self.r.font(50, "semibold"),
            "#ffffff",
        )
        self._fit_text(
            subtitle,
            x + 36,
            self.y + 132,
            w - 72,
            self.r.font(23),
            self.r.HEADER_MUTED,
        )
        self.y += h + 20

    def stats(self, items: list[tuple[str, str, str, str]]):
        gap = 16
        x = self.r.OUTER_PAD
        w = (self.r.CONTENT_W - gap * (len(items) - 1)) // len(items)
        h = 122
        self._ensure(h + 18)
        for title, value, bg, color in items:
            self.draw.rounded_rectangle(
                (x + 2, self.y + 4, x + w + 2, self.y + h + 4),
                radius=8,
                fill="#e4e9f0",
            )
            self.draw.rounded_rectangle(
                (x, self.y, x + w, self.y + h),
                radius=8,
                fill=self.r.CARD,
                outline=self.r.BORDER,
                width=1,
            )
            self.draw.rectangle((x, self.y, x + w, self.y + 5), fill=color)
            self.draw.text(
                (x + 22, self.y + 21),
                title,
                font=self.r.font(21, "medium"),
                fill=self.r.MUTED,
            )
            self.draw.rounded_rectangle(
                (x + w - 42, self.y + 22, x + w - 22, self.y + 29),
                radius=3,
                fill=bg,
                outline=color,
                width=1,
            )
            self._fit_text(
                value,
                x + 22,
                self.y + 58,
                w - 44,
                self.r.font(38, "semibold"),
                color,
            )
            x += w + gap
        self.y += h + 30

    def section_title(self, title: str):
        self.section_index += 1
        self._ensure(72)
        x = self.r.OUTER_PAD
        box = 42
        self.draw.rounded_rectangle(
            (x, self.y, x + box, self.y + box),
            radius=6,
            fill="#e8edff",
        )
        number = f"{self.section_index:02d}"
        number_w = self.draw.textlength(
            number,
            font=self.r.font(19, "semibold"),
        )
        self.draw.text(
            (x + (box - number_w) / 2, self.y + 10),
            number,
            font=self.r.font(19, "semibold"),
            fill=self.r.BLUE,
        )
        title_x = x + box + 16
        self.draw.text(
            (title_x, self.y + 2),
            title,
            font=self.r.font(34, "semibold"),
            fill=self.r.TEXT,
        )
        title_w = self.draw.textlength(
            title,
            font=self.r.font(34, "semibold"),
        )
        line_x = min(x + self.r.CONTENT_W, title_x + title_w + 22)
        if line_x < x + self.r.CONTENT_W:
            self.draw.line(
                (line_x, self.y + 23, x + self.r.CONTENT_W, self.y + 23),
                fill=self.r.BORDER,
                width=2,
            )
        self.y += 62

    def subsection_title(self, title: str):
        self._ensure(58)
        x = self.r.OUTER_PAD
        self.draw.rectangle((x, self.y + 8, x + 5, self.y + 38), fill=self.r.BLUE)
        self.draw.text(
            (x + 18, self.y + 3),
            title,
            font=self.r.font(27, "semibold"),
            fill=self.r.TEXT,
        )
        self.y += 52

    def summary_panel(self, items: list[str], accent: str):
        if not items:
            return
        x = self.r.OUTER_PAD
        inner_w = self.r.CONTENT_W - 64
        rows: list[tuple[list[str], int, str, int, str]] = []
        total_h = 52
        for index, item in enumerate(items):
            size = 27 if index == 0 else 24
            weight = "medium" if index == 0 else "regular"
            color = self.r.TEXT if index == 0 else "#344054"
            font = self.r.font(size, weight)
            lines = self._wrap(item, inner_w, font)
            line_h = self._line_height(font, extra=8)
            rows.append((lines, line_h, color, size, weight))
            total_h += len(lines) * line_h + (10 if index < len(items) - 1 else 0)
        self._ensure(total_h + 24)
        self.draw.rounded_rectangle(
            (x + 3, self.y + 5, x + self.r.CONTENT_W + 3, self.y + total_h + 5),
            radius=8,
            fill="#e4e9f0",
        )
        self.draw.rounded_rectangle(
            (x, self.y, x + self.r.CONTENT_W, self.y + total_h),
            radius=8,
            fill=self.r.CARD,
            outline=self.r.BORDER,
            width=1,
        )
        self.draw.rectangle((x, self.y, x + 7, self.y + total_h), fill=accent)
        yy = self.y + 25
        for lines, line_h, color, size, weight in rows:
            for line in lines:
                self.draw.text(
                    (x + 32, yy),
                    line,
                    font=self.r.font(size, weight),
                    fill=color,
                )
                yy += line_h
            yy += 10
        self.y += total_h + 28

    def paragraph(self, text: str, size: int = 24, color: str | None = None, indent: int = 0):
        lines = self._wrap(text, self.r.CONTENT_W - indent, self.r.font(size))
        line_h = self._line_height(self.r.font(size), extra=8)
        self._ensure(line_h * len(lines) + 10)
        for line in lines:
            self.draw.text(
                (self.r.OUTER_PAD + indent, self.y),
                line,
                font=self.r.font(size),
                fill=color or self.r.TEXT,
            )
            self.y += line_h
        self.y += 8

    def _card_paragraph(self, text: str, size: int = 21, color: str | None = None):
        x = self.r.OUTER_PAD + self.r.CARD_PAD
        w = self.r.CONTENT_W - self.r.CARD_PAD * 2
        lines = self._wrap(text, w, self.r.font(size))
        line_h = self._line_height(self.r.font(size), extra=6)
        self._ensure(line_h * len(lines) + 10)
        for line in lines:
            self.draw.text((x, self.y), line, font=self.r.font(size), fill=color or self.r.TEXT)
            self.y += line_h
        self.y += 8

    def incident_card(
        self,
        index: int,
        group: IncidentGroup,
        label: str = "事件",
        observation: bool = False,
    ):
        issues = list(group.issues)
        labels = _incident_labels(self.r, issues)
        title = text_report._incident_display_title(group, labels, observation=observation)
        time_text = _incident_time_text(group)
        x = self.r.OUTER_PAD
        w = self.r.CONTENT_W
        top = self.y
        placeholder_height = 760 if observation else 1800
        placeholder_bottom = top + placeholder_height
        self._ensure(placeholder_height)
        self.draw.rounded_rectangle(
            (x, top, x + w, placeholder_bottom),
            radius=8,
            fill=self.r.CARD,
        )
        self.y += self.r.CARD_PAD

        if observation:
            badge_fill, accent = "#ecfeff", self.r.CYAN
        else:
            badge_fill, accent = _severity_colors(group.max_severity)
        self.draw.rectangle((x, top, x + w, top + 6), fill=accent)
        badge_x = x + self.r.CARD_PAD
        badge_w = self._badge(
            f"{label} {index:02d}",
            badge_x,
            self.y + 2,
            badge_fill,
            accent,
        )
        severity_text = (
            "一般观察"
            if observation
            else f"风险 {text_report._severity_label(group)}"
        )
        severity_w = self._badge_width(severity_text)
        severity_x = x + w - self.r.CARD_PAD - severity_w
        self._badge(
            severity_text,
            severity_x,
            self.y + 2,
            badge_fill,
            accent,
        )
        title_x = badge_x + badge_w + 18
        self._fit_text(
            title,
            title_x,
            self.y,
            max(80, severity_x - title_x - 16),
            self.r.font(30, "medium"),
            self.r.TEXT,
        )
        self.draw.text(
            (title_x, self.y + 42),
            time_text,
            font=self.r.font(20),
            fill=self.r.MUTED,
        )
        self.y += 82
        self.draw.line(
            (
                x + self.r.CARD_PAD,
                self.y,
                x + w - self.r.CARD_PAD,
                self.y,
            ),
            fill=self.r.BORDER,
            width=1,
        )
        self.y += 20

        self._detail_row("等级", text_report._severity_label(group))
        self._detail_row("状态", text_report._group_status(group))
        evidence_strength = text_report._evidence_strength_line(group)
        if evidence_strength:
            self._detail_row("证据强度", evidence_strength)
        if observation:
            self._detail_row("处理", text_report._incident_recommended_action(group))
            bottom = self.y + self.r.CARD_PAD
            if bottom < placeholder_bottom:
                self.draw.rectangle((x, bottom, x + w, placeholder_bottom), fill=self.r.BG)
            self.draw.rounded_rectangle(
                (x, top, x + w, bottom),
                radius=8,
                outline=self.r.BORDER,
                width=1,
            )
            self.y = bottom + 18
            return

        self._detail_row("影响范围", text_report._impact_scope(group))

        self._subhead("摘要")
        self._card_paragraph(text_report._incident_summary_sentence(group, labels), size=21, color=self.r.TEXT)

        evidence = text_report._incident_key_evidence(issues, limit=3)
        self._subhead("关键证据")
        if evidence:
            self._quote_list(evidence)
        else:
            self._mini_bullet_list(["无可直接展示的关键证据，需查看完整附件。"])

        self._detail_row("初步判断", text_report._incident_judgement_line(group))
        self._detail_row(
            "建议处理",
            text_report._incident_recommended_action(group),
        )
        self._detail_row(
            "参考来源",
            text_report._incident_research_sources(group),
        )

        bottom = self.y + self.r.CARD_PAD
        if bottom < placeholder_bottom:
            self.draw.rectangle((x, bottom, x + w, placeholder_bottom), fill=self.r.BG)
        self.draw.rounded_rectangle(
            (x, top, x + w, bottom),
            radius=8,
            outline=self.r.BORDER,
            width=1,
        )
        self.y = bottom + 18

    def info_note(self, text: str):
        if not text:
            return
        x = self.r.OUTER_PAD
        lines = self._wrap(text, self.r.CONTENT_W - 44, self.r.font(22))
        h = 30 + len(lines) * self._line_height(self.r.font(22), extra=5)
        self._ensure(h + 12)
        self.draw.rounded_rectangle(
            (x, self.y, x + self.r.CONTENT_W, self.y + h),
            radius=8,
            fill="#ffffff",
            outline=self.r.BORDER,
            width=1,
        )
        self.draw.rectangle((x, self.y, x + 5, self.y + h), fill=self.r.CYAN)
        y = self.y + 15
        for line in lines:
            self.draw.text((x + 24, y), line, font=self.r.font(22), fill=self.r.MUTED)
            y += self._line_height(self.r.font(22), extra=5)
        self.y += h + 18

    def bullet_list(self, items: list[str]):
        for item in items:
            self._bullet(item, "•")
        self.y += 4

    def numbered_list(self, items: list[str]):
        for index, item in enumerate(items, 1):
            self._bullet(item, f"{index}.")
        self.y += 4

    def report_footer(self, items: list[str]):
        items = [item for item in items if item]
        if not items:
            return
        self.y += 18
        x = self.r.OUTER_PAD
        self.draw.line(
            (x, self.y, x + self.r.CONTENT_W, self.y),
            fill="#cbd5e1",
            width=2,
        )
        self.y += 24
        for item in items:
            lines = self._wrap(item, self.r.CONTENT_W, self.r.font(20))
            line_h = self._line_height(self.r.font(20), extra=6)
            self._ensure(line_h * len(lines) + 10)
            for line in lines:
                self.draw.text(
                    (x, self.y),
                    line,
                    font=self.r.font(20),
                    fill=self.r.MUTED,
                )
                self.y += line_h
            self.y += 6

    def _detail_row(self, label: str, value: str):
        if not value or value == "未知":
            return
        x = self.r.OUTER_PAD + self.r.CARD_PAD
        label_w = 118
        lines = self._wrap(value, self.r.CONTENT_W - self.r.CARD_PAD * 2 - label_w, self.r.font(22))
        line_h = self._line_height(self.r.font(22), extra=5)
        self._ensure(max(34, len(lines) * line_h) + 10)
        self.draw.text(
            (x, self.y),
            label,
            font=self.r.font(21, "medium"),
            fill=self.r.MUTED,
        )
        yy = self.y
        for line in lines:
            self.draw.text((x + label_w, yy), line, font=self.r.font(22), fill=self.r.TEXT)
            yy += line_h
        self.y = max(self.y + 34, yy + 4)

    def _subhead(self, text: str):
        self._ensure(42)
        x = self.r.OUTER_PAD + self.r.CARD_PAD
        self.draw.rectangle((x, self.y + 8, x + 4, self.y + 31), fill=self.r.BLUE)
        self.draw.text(
            (x + 14, self.y + 4),
            text,
            font=self.r.font(22, "medium"),
            fill=self.r.TEXT,
        )
        self.y += 38

    def _quote_list(self, items: list[str]):
        x = self.r.OUTER_PAD + self.r.CARD_PAD
        w = self.r.CONTENT_W - self.r.CARD_PAD * 2
        for item in items:
            lines = self._wrap(item, w - 34, self.r.font(20))
            line_h = self._line_height(self.r.font(20), extra=5)
            h = 20 + line_h * len(lines)
            self._ensure(h + 8)
            self.draw.rounded_rectangle(
                (x, self.y, x + w, self.y + h),
                radius=6,
                fill="#f6f8fb",
                outline="#e4e9f0",
                width=1,
            )
            yy = self.y + 10
            self.draw.rectangle(
                (x + 14, yy, x + 18, self.y + h - 10),
                fill="#aab6c8",
            )
            for line in lines:
                self.draw.text(
                    (x + 30, yy),
                    line,
                    font=self.r.font(20),
                    fill="#344054",
                )
                yy += line_h
            self.y += h + 8

    def _mini_bullet_list(self, items: list[str]):
        for item in items:
            self._bullet(item, "·", x_offset=self.r.CARD_PAD, size=21)

    def _bullet(self, item: str, marker: str, x_offset: int = 0, size: int = 24):
        x = self.r.OUTER_PAD + x_offset
        numbered = marker.endswith(".")
        marker_w = 46 if numbered else 28
        lines = self._wrap(item, self.r.CONTENT_W - x_offset - marker_w, self.r.font(size))
        line_h = self._line_height(self.r.font(size), extra=7)
        self._ensure(line_h * len(lines) + 8)
        if numbered:
            marker_text = marker[:-1]
            box_size = 32
            self.draw.rounded_rectangle(
                (x, self.y + 1, x + box_size, self.y + box_size + 1),
                radius=5,
                fill="#e8edff",
            )
            marker_text_w = self.draw.textlength(
                marker_text,
                font=self.r.font(size - 4, "medium"),
            )
            self.draw.text(
                (x + (box_size - marker_text_w) / 2, self.y + 5),
                marker_text,
                font=self.r.font(size - 4, "medium"),
                fill=self.r.BLUE,
            )
        else:
            self.draw.ellipse(
                (x + 5, self.y + 11, x + 15, self.y + 21),
                fill=self.r.BLUE,
            )
        yy = self.y
        for line in lines:
            self.draw.text((x + marker_w, yy), line, font=self.r.font(size), fill=self.r.TEXT)
            yy += line_h
        self.y = yy + 8

    def _badge_width(self, text: str) -> int:
        font = self.r.font(19, "medium")
        return int(self.draw.textlength(text, font=font)) + 24

    def _badge(self, text: str, x: int, y: int, fill: str, text_color: str) -> int:
        font = self.r.font(19, "medium")
        w = self._badge_width(text)
        self._ensure(34)
        self.draw.rounded_rectangle(
            (x, y, x + w, y + 32),
            radius=5,
            fill=fill,
        )
        self.draw.text((x + 12, y + 4), text, font=font, fill=text_color)
        return w

    def _fit_text(self, text: str, x: int, y: int, max_w: int, font, color: str):
        value = text
        if self.draw.textlength(value, font=font) > max_w:
            ellipsis = "..."
            # textlength 对前缀长度单调，用二分查找最大前缀 cut 使
            # value[:cut].rstrip() + ellipsis 不超宽，替代逐字符递减的 O(n) 测量。
            lo, hi = 0, len(value)
            best = 0
            while lo <= hi:
                mid = (lo + hi) // 2
                candidate = value[:mid].rstrip() + ellipsis
                if self.draw.textlength(candidate, font=font) <= max_w:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            value = value[:best].rstrip() + ellipsis
        self.draw.text((x, y), value, font=font, fill=color)

    def _wrap(self, text: str, max_width: int, font) -> list[str]:
        result: list[str] = []
        for paragraph in str(text or "").splitlines() or [""]:
            tokens = _wrap_tokens(paragraph)
            line = ""
            for token in tokens:
                candidate = f"{line}{token}" if line else token.lstrip()
                if not candidate:
                    continue
                if self.draw.textlength(candidate, font=font) <= max_width:
                    line = candidate
                    continue
                if line and token in _CLOSING_PUNCTUATION:
                    line = candidate
                    continue
                if line:
                    result.append(line.rstrip())
                    line = token.lstrip()
                while line and self.draw.textlength(line, font=font) > max_width:
                    # 二分查找最大前缀 cut 使 line[:cut] 不超宽，至少保留 1 字符。
                    lo, hi = 1, max(1, len(line) - 1)
                    cut = 1
                    while lo <= hi:
                        mid = (lo + hi) // 2
                        if self.draw.textlength(line[:mid], font=font) <= max_width:
                            cut = mid
                            lo = mid + 1
                        else:
                            hi = mid - 1
                    result.append(line[:cut].rstrip())
                    line = line[cut:].lstrip()
            if line:
                result.append(line.rstrip())
            if not tokens:
                result.append("")
        return result or [""]

    def _line_height(self, font, extra: int = 6) -> int:
        bbox = self.draw.textbbox((0, 0), "国Ag", font=font)
        return max(12, bbox[3] - bbox[1] + extra)

    def _ensure(self, needed: int):
        if self.y + needed + self.r.OUTER_PAD <= self.image.height:
            return
        new_h = self.image.height
        while self.y + needed + self.r.OUTER_PAD > new_h:
            new_h += 1800
        expanded = Image.new("RGB", (self.r.WIDTH, new_h), self.r.BG)
        expanded.paste(self.image, (0, 0))
        self.image = expanded
        self.draw = ImageDraw.Draw(self.image)


def _wrap_tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_./:@#%+\-]+|\s+|.", text)


_CLOSING_PUNCTUATION = set("，。；：！？、）】》」』”’")


def _format_servers(report: dict) -> str:
    values: list[str] = []
    server_names = report.get("server_names") or []
    server_fields = ("server_names",) if server_names else ("servers",)
    for field in server_fields + ("proxy_ids",):
        raw = report.get(field) or []
        if isinstance(raw, str):
            raw = [raw]
        for value in raw:
            value = str(value).strip()
            if value and value not in values:
                values.append(value)
    return " / ".join(values) if values else "全部服务器"


def _incident_labels(
    renderer: MineSentinelReportImageRenderer,
    issues: list[dict[str, Any]],
) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for issue in sorted(issues, key=issue_sort_key):
        label = renderer.issue_title(issue)
        key = _dedupe_key(label)
        if key in seen:
            continue
        seen.add(key)
        labels.append(label)
    return labels


def _report_status(incident_count: int, high_risk_count: int) -> tuple[str, str]:
    if high_risk_count:
        return "需要优先处置", "#b42318"
    if incident_count:
        return "需要关注", "#b45309"
    return "运行稳定", "#15803d"


def _severity_colors(severity: str) -> tuple[str, str]:
    value = str(severity or "low").lower()
    if value in {"critical", "high"}:
        return "#fef2f2", "#c2413b"
    if value == "medium":
        return "#fff7ed", "#b45309"
    if value in {"low", "info"}:
        return "#ecfdf5", "#15803d"
    return "#eff6ff", "#3157d5"
