"""Prompt files live in ../prompts; the A1 spec-writing skill in ../skills/spec-writer."""
from __future__ import annotations

import re
from pathlib import Path

from .config import HARNESS_DIR

PROMPT_DIR = HARNESS_DIR / 'prompts'
SKILL_DIR = HARNESS_DIR / 'skills' / 'spec-writer'
FALLBACK_MARKER = 'AUTOSE_FALLBACK_SKILL'


def render(template: str, **values: object) -> str:
    return re.sub(r'\{\{\s*(\w+)\s*\}\}', lambda m: str(values.get(m.group(1), m.group(0))), template)


def load(name: str, **values: object) -> str:
    return render((PROMPT_DIR / f'{name}.md').read_text(encoding='utf-8'), **values)


def system(role: str, **values: object) -> str:
    return load('_security') + '\n\n' + load(role, **values)


def skill() -> tuple[str, bool, Path]:
    """Return (skill text, is_fallback, folder) for the requirements agent."""
    path = SKILL_DIR / 'SKILL.md'
    if not path.exists():
        return ('(no spec-writing skill installed)', True, SKILL_DIR)
    text = path.read_text(encoding='utf-8')
    is_fallback = FALLBACK_MARKER in text
    body = re.sub(r'\A---\n.*?\n---\n', '', text, count=1, flags=re.S).strip()
    others = sorted(p.relative_to(SKILL_DIR).as_posix() for p in SKILL_DIR.rglob('*')
                    if p.is_file() and p != path)
    if others:
        body += ('\n\nOther files in this skill (read them with read_skill_file when the skill refers to '
                 'them): ' + ', '.join(others))
    return body, is_fallback, SKILL_DIR
