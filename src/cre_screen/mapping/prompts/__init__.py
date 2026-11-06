"""Prompt templates (SPEC section 14). Templates use ``str.format`` with named fields."""

from __future__ import annotations

from pathlib import Path

SYSTEM_LINE = (
    "You classify labels for a commercial real estate underwriting tool. "
    "You never invent numbers. Return only the requested structure."
)

_DIR = Path(__file__).resolve().parent


def render_prompt(name: str, **fields: str) -> str:
    template = (_DIR / f"{name}.md").read_text(encoding="utf-8")
    return template.format(**fields).rstrip("\n")
