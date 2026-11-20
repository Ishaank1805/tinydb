"""The live status comment: one comment per run, one section per job."""
from __future__ import annotations

from typing import Any

from .github import GitHubError

STAGES = [
    ('requirements', '1 Requirements', 'planner', 'spec.md'),
    ('design', '2 Design', 'planner', 'design.md'),
    ('implementation', '3 Implementation', 'implementer', 'diff.patch'),
    ('testing', '4 Testing', 'test author + harness', 'test_report.md'),
    ('release', '5 Release', 'harness (no LLM)', 'pr.md'),
    ('review', '6 Review', 'reviewer (own job)', 'review.md'),
]
ICONS = {'running': '⏳', 'passed': '✅', 'failed': '❌', 'looped': '↩️', 'skipped': '⏭️', 'pending': '·'}


def replace_section(body: str, section: str, content: str) -> str:
    start, end = f'<!-- autose:section {section} -->', f'<!-- /autose:section {section} -->'
    block = f'{start}\n{content}\n{end}'
    if start in body and end in body:
        before = body.split(start, 1)[0]
        after = body.split(end, 1)[1]
        return before + block + after
    return body.rstrip() + '\n\n' + block


def stage_table(progress: dict, title: str) -> str:
    rows = [f'**{title}**', '', '| Stage | Agent | Status | Artifact | Note |', '|---|---|---|---|---|']
    for key, label, agent, artifact in STAGES:
        if key == 'review':
            continue
        item = progress.get(key) or {}
        status = item.get('status', 'pending')
        note = str(item.get('note', '')).replace('|', '\\|').replace('\n', ' ')[:180]
        rows.append(f'| {label} | {agent} | {ICONS.get(status, "")} {status} | `{artifact}` | {note} |')
    return '\n'.join(rows)


class StatusBoard:
    def __init__(self, gh: Any, comment_id: str | int | None, section: str, events: Any = None):
        self.gh = gh
        self.comment_id = comment_id
        self.section = section
        self.events = events

    def update(self, markdown: str) -> None:
        if not self.gh or not self.comment_id:
            return
        try:
            body = self.gh.get_comment(self.comment_id).get('body') or ''
            self.gh.update_comment(self.comment_id, replace_section(body, self.section, markdown))
        except GitHubError as exc:
            if self.events:
                self.events.emit('status_update_failed', error=str(exc)[:300])
