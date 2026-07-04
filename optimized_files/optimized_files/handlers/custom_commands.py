"""Custom Minecraft command mapping parser."""

from __future__ import annotations

import re


class CustomCommandParser:
    """Parses user-facing custom command shortcuts into real MC commands."""

    SEPARATOR = "<<>>"

    def __init__(self, mappings: list[str]):
        self.mappings: list[dict[str, object]] = []
        for mapping in mappings:
            parsed = self._parse_mapping(mapping)
            if parsed:
                self.mappings.append(parsed)

    def _parse_mapping(self, mapping: str) -> dict[str, object] | None:
        if self.SEPARATOR not in mapping:
            return None

        trigger_part, command_part = mapping.split(self.SEPARATOR, 1)
        trigger_part = trigger_part.strip()
        command_part = command_part.strip()

        param_pattern = r"<&(\w+)&>"
        param_names = re.findall(param_pattern, trigger_part)

        trigger_regex = trigger_part
        for param in param_names:
            trigger_regex = trigger_regex.replace(f"<&{param}&>", f"(?P<{param}>\\S+)")

        trigger_name = trigger_part.split()[0] if trigger_part else ""
        # Pre-compile the trigger regex so per-message matching doesn't pay the
        # compilation cost on every call. match() is invoked for every incoming
        # non-command message × every mapping.
        compiled = re.compile(f"^{trigger_regex}$", re.IGNORECASE)
        return {
            "trigger_part": trigger_part,
            "trigger_name": trigger_name,
            "trigger_regex": trigger_regex,
            "compiled_regex": compiled,
            "param_names": param_names,
            "command_template": command_part,
        }

    def match(
        self, text: str, sender_mc_name: str | None = None
    ) -> tuple[str, dict] | None:
        for mapping in self.mappings:
            command_template = mapping["command_template"]
            match = mapping["compiled_regex"].match(text)
            if not match:
                continue

            params = match.groupdict()
            params["sender"] = sender_mc_name or ""

            command = str(command_template)
            for key, value in params.items():
                command = command.replace(f"{{{key}}}", value)
                command = command.replace(f"<&{key}&>", value)

            return command, params

        return None

    def get_missing_usage(self, text: str) -> str | None:
        tokens = re.split(r"\s+", text.strip())
        if not tokens or not tokens[0]:
            return None

        first_token = tokens[0].lower()
        for mapping in self.mappings:
            trigger_name = str(mapping["trigger_name"]).lower()
            if not trigger_name or first_token != trigger_name:
                continue
            param_names = mapping["param_names"]
            expected_count = 1 + len(param_names)
            if len(tokens) < expected_count:
                return str(mapping["trigger_part"])

        return None
