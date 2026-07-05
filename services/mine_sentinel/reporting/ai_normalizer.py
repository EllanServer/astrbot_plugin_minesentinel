"""Normalize AI report JSON back onto deterministic runtime-log facts."""

from __future__ import annotations

import json
import re
from typing import Any

from .common import format_locations


def parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def repair_json_object_text(text: str) -> str:
    match = re.search(r"\{.*\}", text, flags=re.S)
    return match.group(0) if match else ""


class AIReportNormalizer:
    """Preserves locations, evidence, and counts from fallback facts."""

    def normalize_report(
        self,
        data: dict[str, Any],
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        result = dict(fallback)
        result.update({key: value for key, value in data.items() if key in result})
        categories = result.get("categories")
        if not isinstance(categories, dict):
            categories = fallback["categories"]
        for key in (
            "daily",
            "complaint",
            "bug",
            "network",
            "plugin",
            "economy",
            "community",
            "chat_review",
            "player_feedback",
            "community_ops",
            "moderation",
            "cross_server",
            "suggestion",
        ):
            if not isinstance(categories.get(key), list):
                categories[key] = []
        result["categories"] = categories
        self.normalize_issues(result, fallback)
        if not isinstance(result.get("ops_notes"), list):
            result["ops_notes"] = fallback["ops_notes"]
        if not isinstance(result.get("incident_findings"), list):
            result["incident_findings"] = fallback.get("incident_findings", [])
        return result

    def normalize_issues(self, result: dict[str, Any], fallback: dict[str, Any]):
        if not isinstance(result.get("issues"), list):
            result["issues"] = fallback["issues"]
            return

        fallback_issues = [
            issue for issue in fallback.get("issues", []) if isinstance(issue, dict)
        ]
        used_fallback_indexes: set[int] = set()
        normalized_issues = []
        for issue in result["issues"]:
            if not isinstance(issue, dict):
                continue
            fallback_index, fallback_issue = self._match_fallback_issue(
                issue,
                fallback_issues,
                used_fallback_indexes,
            )
            if fallback_index < 0:
                continue
            if fallback_index >= 0:
                used_fallback_indexes.add(fallback_index)
            self._normalize_structured_fields(issue, fallback_issue)
            self._normalize_players(issue, fallback_issue)
            self._normalize_counts(issue, fallback_issue)
            self._normalize_locations(issue, fallback_issue)
            normalized_issues.append(issue)

        if normalized_issues:
            result["issues"] = normalized_issues
        else:
            result["issues"] = fallback["issues"]

    @staticmethod
    def _match_fallback_issue(
        issue: dict[str, Any],
        fallback_issues: list[dict[str, Any]],
        used_indexes: set[int],
    ) -> tuple[int, dict[str, Any]]:
        key = (issue.get("category"), issue.get("tag"))
        incident_index = _as_int(issue.get("incident_index"))
        if incident_index is not None:
            for index, fallback_issue in enumerate(fallback_issues):
                if index in used_indexes:
                    continue
                fallback_key = (fallback_issue.get("category"), fallback_issue.get("tag"))
                if (
                    fallback_key == key
                    and _as_int(fallback_issue.get("incident_index")) == incident_index
                ):
                    return index, fallback_issue

        for index, fallback_issue in enumerate(fallback_issues):
            if index in used_indexes:
                continue
            fallback_key = (fallback_issue.get("category"), fallback_issue.get("tag"))
            if fallback_key == key:
                return index, fallback_issue
        return -1, {}

    @staticmethod
    def _normalize_structured_fields(
        issue: dict[str, Any],
        fallback_issue: dict[str, Any],
    ):
        fallback_incident = _as_int(fallback_issue.get("incident_index"))
        issue_incident = _as_int(issue.get("incident_index"))
        if fallback_incident is not None:
            issue["incident_index"] = fallback_incident
        elif issue_incident is not None:
            issue["incident_index"] = issue_incident
        for field in (
            "source_tag",
            "first_seen_ts",
            "last_seen_ts",
            "urgent_signal_count",
        ):
            if field not in issue and field in fallback_issue:
                issue[field] = fallback_issue[field]
        if not isinstance(issue.get("issue_terms"), list):
            terms = fallback_issue.get("issue_terms") or []
            issue["issue_terms"] = [str(term) for term in terms if term]
        if not isinstance(issue.get("evidence_samples"), list):
            samples = fallback_issue.get("evidence_samples") or []
            issue["evidence_samples"] = [str(sample) for sample in samples if sample]

    @staticmethod
    def _normalize_players(
        issue: dict[str, Any],
        fallback_issue: dict[str, Any],
    ):
        players = issue.get("players") or issue.get("player_names")
        if not isinstance(players, list):
            players = fallback_issue.get("players") or []
        issue["players"] = [str(player) for player in players if player]
        if not issue.get("players_text"):
            issue["players_text"] = "无"

        mentioned_players = issue.get("mentioned_players")
        if not isinstance(mentioned_players, list):
            mentioned_players = fallback_issue.get("mentioned_players") or []
        issue["mentioned_players"] = [
            str(player) for player in mentioned_players if player
        ]
        if not issue.get("mentioned_players_text"):
            issue["mentioned_players_text"] = "无"

    @staticmethod
    def _normalize_counts(
        issue: dict[str, Any],
        fallback_issue: dict[str, Any],
    ):
        for count_field in (
            "evidence_count",
            "signal_count",
            "distinct_message_count",
            "unique_players",
        ):
            if count_field not in issue and count_field in fallback_issue:
                issue[count_field] = fallback_issue[count_field]

    @staticmethod
    def _normalize_locations(
        issue: dict[str, Any],
        fallback_issue: dict[str, Any],
    ):
        for list_field in (
            "affected_servers",
            "affected_backends",
            "affected_locations",
        ):
            values = issue.get(list_field)
            if not isinstance(values, list):
                values = fallback_issue.get(list_field) or []
            issue[list_field] = [str(value) for value in values if value]
        if not issue.get("affected_locations_text"):
            issue["affected_locations_text"] = format_locations(
                issue.get("affected_locations") or []
            )

def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
