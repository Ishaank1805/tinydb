"""The six SDLC stages. Stages 1-5 run in the build job; stage 6 is ``autose/review.py``."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..tools import matches
from ..untrusted import wrap


@dataclass
class StageOutcome:
    status: str            # passed | failed | redesign | dispute | tests_invalid | ci_failed
    message: str = ''
    data: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == 'passed'


def issue_block(ctx: Any) -> str:
    issue = ctx.issue
    parts = [wrap('issue', f"#{issue.get('number')} {issue.get('title', '')}\n\n{issue.get('body', '')}",
                  number=issue.get('number', ''), author=issue.get('author', ''))]
    trigger = issue.get('trigger') or {}
    if trigger.get('comment'):
        parts.append(wrap('trigger_comment', trigger['comment'], author=trigger.get('actor', '')))
    return '\n\n'.join(parts)


def snapshot(ctx: Any) -> dict[str, bytes | None]:
    """Contents of every file that differs from HEAD right now."""
    return {rel: ctx.git.worktree_bytes(rel) for rel in ctx.git.changed_files('HEAD')}


def enforce(ctx: Any, snap: dict[str, bytes | None], allowed: list[str]) -> list[str]:
    """Revert every change made since ``snap`` to a path outside ``allowed``.

    This catches writes that bypassed the file tools (e.g. via run_command).
    """
    reverted = []
    for rel in sorted(set(ctx.git.changed_files('HEAD')) | set(snap)):
        before = snap[rel] if rel in snap else ctx.git.show_file('HEAD', rel)
        if ctx.git.worktree_bytes(rel) == before:
            continue
        if matches(rel, allowed) and not matches(rel, ctx.protected_globs):
            continue
        ctx.git.restore(rel, before)
        reverted.append(rel)
    if reverted:
        ctx.events.emit('policy_revert', files=reverted)
    return reverted


def agent_failure(result: Any, who: str) -> str:
    return {
        'max_steps': f'the {who} used all of its tool-call rounds without finishing',
        'no_tool_calls': f'the {who} stopped calling tools: {result.detail[:200]}',
        'refused': f'the model declined the request: {result.detail[:200]}',
        'stuck': f'the {who} got stuck ({result.detail})',
    }.get(result.outcome, f'the {who} ended with "{result.outcome}": {result.detail[:200]}')


def top_level(ctx: Any) -> str:
    names = []
    for path in sorted(ctx.repo_dir.iterdir()):
        rel = path.name + ('/' if path.is_dir() else '')
        if path.name == '.git' or ctx.workspace().is_hidden(path.name):
            continue
        names.append(rel)
    return ', '.join(names)
