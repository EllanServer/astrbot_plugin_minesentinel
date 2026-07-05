"""Pillow image renderer for MineSentinel reports."""

from __future__ import annotations

import asyncio
import re
import time
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
    resolve_attachment_name,
)
from .incidents import IncidentGroup, IncidentGrouper, IssuePolicy, issue_sort_key
from .labels import DEFAULT_LABELS
from .presentation import ReportPresentationBuilder


class MineSentinelReportImageRenderer:
    """Render an incident-level MineSentinel report as a QQ-friendly PNG."""

    WIDTH = 1200
    OUTER_PAD = 34
    CARD_PAD = 28
    CONTENT_W = WIDTH - OUTER_PAD * 2
    BG = "#eef2f7"
    CARD = "#ffffff"
    TEXT = "#111827"
    MUTED = "#6b7280"
    BORDER = "#e5e7eb"
    BLUE = "#2563eb"
    CYAN = "#0891b2"
    GREEN = "#059669"
    AMBER = "#d97706"
    RED = "#dc2626"

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.font_provider = FontProvider(cache_dir / "fonts")
        self._font_cache: dict[int, object] = {}
        self._assets_ready = False
        self._labels = DEFAULT_LABELS
        self._presentation_builder = ReportPresentationBuilder(
            issue_policy=IssuePolicy(),
            incident_grouper=IncidentGrouper(merge_window_ms=60 * 60 * 1000),
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

        canvas = _ReportCanvas(self)
        canvas.header(
            "MineSentinel 巡检报告",
            f"{_format_servers(report)} · {_format_time_window(report)}",
        )
        incident_groups, observation_groups = text_report._split_incident_groups(
            presentation.incidents
        )
        canvas.stats(
            [
                ("重点事件", str(len(incident_groups)), "#fff7ed", self.AMBER),
                ("高风险事件", str(text_report._high_risk_count(incident_groups)), "#fef2f2", self.RED),
                ("待人工复核", str(text_report._manual_review_count(incident_groups)), "#eff6ff", self.BLUE),
                ("一般观察", str(len(observation_groups)), "#ecfdf5", self.GREEN),
            ]
        )
        player_count = text_report._player_count(report, presentation.unique_players)
        canvas.section_title("整体情况")
        for line in text_report._overall_lines(
            report,
            player_count,
            len(incident_groups),
            len(observation_groups),
            text_report._high_risk_count(incident_groups),
            text_report._manual_review_count(incident_groups),
            _format_duration(report),
            incident_groups,
        ):
            canvas.paragraph(line, size=26, color=self.TEXT)

        canvas.section_title("重点事件总结")
        if incident_groups:
            for index, group in enumerate(incident_groups[:8], 1):
                canvas.incident_card(index, group)
            canvas.info_note(_quiet_window_text(report, incident_groups))
        else:
            canvas.info_note("本窗口未发现需要管理员优先处理的事故或玩家问题。")

        canvas.section_title("聊天与社区观察")
        if observation_groups:
            for index, group in enumerate(observation_groups[:6], 1):
                canvas.incident_card(index, group, label="观察", observation=True)
        else:
            canvas.info_note("本窗口未识别到需要单独记录的低风险聊天或社区观察。")

        canvas.section_title("玩家问题/投诉识别")
        canvas.bullet_list(text_report._player_problem_lines(presentation.issues, incident_groups + observation_groups))

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

        canvas.section_title("建议处理")
        canvas.numbered_list(text_report._action_lines(presentation.issues))

        canvas.footer(f"证据：共 {presentation.total_count} 条观察，涉及玩家 {player_count} 人。")
        canvas.footer(f"本次总结由 AI 根据完整{_format_duration(report)}聊天上下文、玩家事件和服务器指标生成。")
        return canvas.output()

    async def _ensure_assets(self):
        if self._assets_ready:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        await self.font_provider.ensure_cached()
        self._assets_ready = True

    def font(self, size: int):
        if size not in self._font_cache:
            self._font_cache[size] = self.font_provider.font(size)
        return self._font_cache[size]

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

    def output(self) -> BytesIO:
        bottom = min(self.image.height, self.y + self.r.OUTER_PAD)
        return save_png(self.image.crop((0, 0, self.r.WIDTH, bottom)))

    def header(self, title: str, subtitle: str):
        x = self.r.OUTER_PAD
        w = self.r.CONTENT_W
        h = 150
        self._ensure(h + 20)
        self.draw.rounded_rectangle((x, self.y, x + w, self.y + h), radius=24, fill="#dbeafe")
        self.draw.rectangle((x + 30, self.y + 28, x + 44, self.y + h - 28), fill=self.r.BLUE)
        self.draw.text((x + 66, self.y + 28), title, font=self.r.font(48), fill=self.r.TEXT)
        self.draw.text((x + 68, self.y + 94), subtitle, font=self.r.font(24), fill=self.r.MUTED)
        self.y += h + 18

    def stats(self, items: list[tuple[str, str, str, str]]):
        gap = 14
        x = self.r.OUTER_PAD
        w = (self.r.CONTENT_W - gap * (len(items) - 1)) // len(items)
        h = 112
        self._ensure(h + 18)
        for title, value, bg, color in items:
            self.draw.rounded_rectangle((x, self.y, x + w, self.y + h), radius=18, fill=bg)
            self.draw.text((x + 22, self.y + 18), title, font=self.r.font(22), fill=self.r.MUTED)
            self._fit_text(value, x + 22, self.y + 52, w - 44, self.r.font(34), color)
            x += w + gap
        self.y += h + 20

    def section_title(self, title: str):
        self._ensure(64)
        self.draw.text((self.r.OUTER_PAD + 2, self.y), title, font=self.r.font(34), fill=self.r.TEXT)
        self.y += 54

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
            radius=20,
            fill=self.r.CARD,
        )
        self.y += self.r.CARD_PAD

        badge_fill, accent = _incident_colors(group.family)
        self.draw.rectangle((x, top + 22, x + 8, top + 58), fill=accent)
        self._badge(f"{label} #{index}", x + 24, self.y + 2, "#f3f4f6", self.r.TEXT)
        self._fit_text(title, x + 140, self.y, w - 178, self.r.font(30), self.r.TEXT)
        self.draw.text((x + 140, self.y + 40), time_text, font=self.r.font(20), fill=self.r.MUTED)
        self.y += 76

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
                radius=20,
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

        bottom = self.y + self.r.CARD_PAD
        if bottom < placeholder_bottom:
            self.draw.rectangle((x, bottom, x + w, placeholder_bottom), fill=self.r.BG)
        self.draw.rounded_rectangle(
            (x, top, x + w, bottom),
            radius=20,
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
        self.draw.rounded_rectangle((x, self.y, x + self.r.CONTENT_W, self.y + h), radius=16, fill="#f8fafc")
        y = self.y + 15
        for line in lines:
            self.draw.text((x + 22, y), line, font=self.r.font(22), fill=self.r.MUTED)
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

    def reference_list(self, items: list[tuple[str, list[str]]]):
        x = self.r.OUTER_PAD
        w = self.r.CONTENT_W
        for title, lines in items:
            if not lines:
                continue
            title_h = self._line_height(self.r.font(21), extra=6)
            body_font = self.r.font(20)
            wrapped: list[str] = []
            for line in lines:
                wrapped.extend(self._wrap(line, w - 86, body_font))
            line_h = self._line_height(body_font, extra=6)
            h = 34 + title_h + max(1, len(wrapped)) * line_h
            self._ensure(h + 12)
            self.draw.rounded_rectangle(
                (x, self.y, x + w, self.y + h),
                radius=16,
                fill="#ffffff",
                outline=self.r.BORDER,
            )
            self.draw.rectangle((x + 28, self.y + 24, x + 34, self.y + h - 24), fill="#cbd5e1")
            self.draw.text((x + 54, self.y + 18), title, font=self.r.font(21), fill=self.r.MUTED)
            yy = self.y + 18 + title_h
            for line in wrapped:
                self.draw.text((x + 54, yy), line, font=body_font, fill="#374151")
                yy += line_h
            self.y += h + 12

    def footer(self, text: str):
        self.y += 8
        self.info_note(text)

    def _detail_row(self, label: str, value: str):
        if not value or value == "未知":
            return
        x = self.r.OUTER_PAD + self.r.CARD_PAD
        label_w = 118
        lines = self._wrap(value, self.r.CONTENT_W - self.r.CARD_PAD * 2 - label_w, self.r.font(22))
        line_h = self._line_height(self.r.font(22), extra=5)
        self._ensure(max(34, len(lines) * line_h) + 10)
        self.draw.text((x, self.y), label, font=self.r.font(21), fill=self.r.MUTED)
        yy = self.y
        for line in lines:
            self.draw.text((x + label_w, yy), line, font=self.r.font(22), fill=self.r.TEXT)
            yy += line_h
        self.y = max(self.y + 34, yy + 4)

    def _subhead(self, text: str):
        self._ensure(42)
        self.draw.text(
            (self.r.OUTER_PAD + self.r.CARD_PAD, self.y + 4),
            text,
            font=self.r.font(22),
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
            self.draw.rounded_rectangle((x, self.y, x + w, self.y + h), radius=12, fill="#f9fafb")
            yy = self.y + 10
            self.draw.rectangle((x + 14, yy, x + 18, self.y + h - 10), fill="#cbd5e1")
            for line in lines:
                self.draw.text((x + 30, yy), line, font=self.r.font(20), fill="#374151")
                yy += line_h
            self.y += h + 8

    def _mini_bullet_list(self, items: list[str]):
        for item in items:
            self._bullet(item, "·", x_offset=self.r.CARD_PAD, size=21)

    def _bullet(self, item: str, marker: str, x_offset: int = 0, size: int = 24):
        x = self.r.OUTER_PAD + x_offset
        marker_w = 42 if marker.endswith(".") else 28
        lines = self._wrap(item, self.r.CONTENT_W - x_offset - marker_w, self.r.font(size))
        line_h = self._line_height(self.r.font(size), extra=7)
        self._ensure(line_h * len(lines) + 8)
        self.draw.text((x, self.y), marker, font=self.r.font(size), fill=self.r.BLUE)
        yy = self.y
        for line in lines:
            self.draw.text((x + marker_w, yy), line, font=self.r.font(size), fill=self.r.TEXT)
            yy += line_h
        self.y = yy + 8

    def _badge_row(self, labels: list[str], x: int, fill: str, text_color: str):
        if not labels:
            return
        start_x = x
        max_x = self.r.OUTER_PAD + self.r.CONTENT_W - self.r.CARD_PAD
        for label in labels:
            badge_w = int(self.draw.textlength(label, font=self.r.font(19))) + 26
            if x + badge_w > max_x:
                x = start_x
                self.y += 38
            self._badge(label, x, self.y, fill, text_color)
            x += badge_w + 8
        self.y += 44

    def _badge(self, text: str, x: int, y: int, fill: str, text_color: str):
        font = self.r.font(19)
        w = int(self.draw.textlength(text, font=font)) + 24
        self._ensure(34)
        self.draw.rounded_rectangle((x, y, x + w, y + 30), radius=15, fill=fill)
        self.draw.text((x + 12, y + 4), text, font=font, fill=text_color)

    def _fit_text(self, text: str, x: int, y: int, max_w: int, font, color: str):
        value = text
        if self.draw.textlength(value, font=font) > max_w:
            ellipsis = "..."
            cut = len(value)
            while cut > 0:
                candidate = value[:cut].rstrip() + ellipsis
                if self.draw.textlength(candidate, font=font) <= max_w:
                    value = candidate
                    break
                cut -= 1
            else:
                value = ellipsis
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
                    cut = max(1, len(line) - 1)
                    while cut > 1 and self.draw.textlength(line[:cut], font=font) > max_width:
                        cut -= 1
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


def _format_attachment_name(report: dict) -> str:
    return resolve_attachment_name(report) or "未生成"


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


def _incident_players(issues: list[dict[str, Any]]) -> str:
    players: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        for player in issue.get("players") or []:
            value = str(player).strip()
            if value and value not in seen:
                seen.add(value)
                players.append(value)
    return "、".join(sorted(players)) if players else "未知"


def _incident_locations(issues: list[dict[str, Any]]) -> str:
    locations: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        for location in issue.get("affected_locations") or []:
            value = str(location).strip()
            if value and value not in seen:
                seen.add(value)
                locations.append(value)
    return "、".join(sorted(locations)) if locations else "未知"


def _incident_colors(family: str) -> tuple[str, str]:
    if family == "community":
        return "#fefce8", "#ca8a04"
    if family == "moderation":
        return "#fef2f2", "#dc2626"
    if family == "suggestion":
        return "#f0fdf4", "#059669"
    return "#eff6ff", "#2563eb"
