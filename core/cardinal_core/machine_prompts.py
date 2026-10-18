"""Cheap recognition of Claude-generated prompts; no runtime or network imports."""

import re

CLAUDE = re.compile(
    r"^\s*(?:<?task-notification[\s>]|(?:[⏺●]\s*)?Monitor event:)", re.IGNORECASE
)


def is_claude_machine_prompt(prompt):
    return isinstance(prompt, str) and CLAUDE.match(prompt) is not None
