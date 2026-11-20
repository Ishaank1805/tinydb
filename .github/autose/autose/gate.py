"""Gate job: who triggered what, on which issue/PR, with which variant.

No LLM runs here. It checks the trigger and the actor's permission, takes a
sanitised snapshot of the issue (the text the maintainer saw when triggering),
posts the status comment and hands everything to the next jobs as outputs.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from . import events as gha
from .config import Config, ConfigError
from .github import GitHub, GitHubError
from .untrusted import describe, sanitize

PERM_RANK = {'none': 0, 'read': 1, 'triage': 2, 'write': 3, 'maintain': 4, 'admin': 5}
LABELS = {'autose': ('1d76db', 'Start the AutoSE pipeline'),
          'autose:review-pending': ('c5def5', 'AutoSE: waiting for the independent review'),
          'autose:approved': ('0e8a16', 'AutoSE reviewer approved; a human decides'),
          'autose:changes-requested': ('fbca04', 'AutoSE reviewer requested changes'),
          'autose:needs-human': ('d93f0b', 'AutoSE finished without approval; a human decides'),
          'autose:stopped': ('b60205', 'AutoSE stopped; see the comment')}
HELP = ('**AutoSE commands** (people with write access):\n'
        '- on an issue: `/autose` or `/autose run` - run the whole pipeline; add `variant=<name>` for experiments\n'
        '- on a pull request: `/autose review [#issue]` - one independent review of the current head\n'
        '- or add the `autose` label to an issue.')


def parse_command(body: str, command: str = '/autose') -> dict | None:
    lines = (body or '').strip().splitlines()
    if not lines:
        return None
    m = re.match(rf'^{re.escape(command)}(?:\s+(.*))?$', lines[0].strip(), re.I)
    if not m:
        return None
    out: dict[str, Any] = {'sub': '', 'issue': None, 'variant': None, 'unknown': [],
                           'notes': '\n'.join(lines[1:]).strip()}
    for token in (m.group(1) or '').split():
        low = token.lower()
        if low in ('run', 'review'):
            out['sub'] = low
        elif re.fullmatch(r'#?\d+', token):
            out['issue'] = int(token.lstrip('#'))
        elif low.startswith('variant='):
            out['variant'] = token.split('=', 1)[1]
        else:
            out['unknown'].append(token)
    return out


def linked_issue(pr: dict, explicit: int | None, prefix: str) -> int | None:
    if explicit:
        return explicit
    m = re.match(rf'^{re.escape(prefix)}(\d+)', (pr.get('head') or {}).get('ref', ''))
    if m:
        return int(m.group(1))
    m = re.search(r'\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?)\s+#(\d+)', pr.get('body') or '', re.I)
    return int(m.group(1)) if m else None


def run_gate(cfg: Config, secrets: Any) -> int:
    env = os.environ
    repo, event_name = env['GITHUB_REPOSITORY'], env['GITHUB_EVENT_NAME']
    payload = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    run_url = f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/runs/{env.get('GITHUB_RUN_ID', '')}"
    gh = GitHub(secrets.github_token, repo)

    def skip(reason: str, notify: int | None = None, message: str = '') -> int:
        gha.notice(f'AutoSE skipped: {reason}')
        if notify and message:
            try:
                gh.create_comment(notify, message)
            except GitHubError:
                pass
        gha.set_outputs(proceed='false', reason=reason)
        return 0

    sender = (payload.get('sender') or {}).get('login') or env.get('GITHUB_ACTOR', '')
    mode, variant, notes = 'full', None, ''
    issue_no: int | None = None
    pr_no: int | None = None
    explicit_issue: int | None = None
    issue: dict | None = None
    comment_id = None
    if event_name == 'issues':
        if (payload.get('label') or {}).get('name') != cfg.get('trigger.label'):
            return skip('a different label was added')
        issue = payload['issue']
        if issue.get('pull_request'):
            return skip('labels on pull requests do not start a run')
        issue_no = issue['number']
    elif event_name == 'issue_comment':
        comment = payload['comment']
        if (comment.get('user') or {}).get('type') == 'Bot':
            return skip('comment written by a bot')
        cmd = parse_command(comment.get('body', ''), cfg.get('trigger.command', '/autose'))
        if cmd is None:
            return skip('not an AutoSE command')
        comment_id, issue, variant, notes = comment['id'], payload['issue'], cmd['variant'], cmd['notes']
        if cmd['unknown']:
            return skip('unknown arguments', issue['number'], f"Unknown arguments: `{' '.join(cmd['unknown'])}`\n\n{HELP}")
        if issue.get('pull_request'):
            if cmd['sub'] == 'run':
                return skip('run on a PR', issue['number'], HELP)
            mode, pr_no, explicit_issue = 'review', issue['number'], cmd['issue']
        else:
            if cmd['sub'] == 'review':
                return skip('review on an issue', issue['number'], HELP)
            issue_no = issue['number']
    elif event_name == 'workflow_dispatch':
        inputs = payload.get('inputs') or {}
        mode, variant = inputs.get('mode') or 'full', inputs.get('variant') or None
        number = int(inputs['number'])
        if mode == 'review':
            pr_no = number
            explicit_issue = int(inputs['issue']) if str(inputs.get('issue') or '').strip() else None
        else:
            issue_no = number
    else:
        return skip(f'unsupported event {event_name}')

    if event_name != 'workflow_dispatch':     # dispatching already requires write access
        perm = gh.permission(sender)
        need = cfg.get('trigger.min_permission', 'write')
        if PERM_RANK.get(perm, 0) < PERM_RANK.get(need, 3):
            return skip(f'{sender} has {perm} permission', issue_no or pr_no,
                        f'@{sender} only people with {need} access can start AutoSE.')
    variant = variant or 'baseline'
    try:
        vcfg = Config.load(variant=variant)
    except ConfigError as exc:
        return skip('unknown variant', issue_no or pr_no, f'{exc}\n\n{HELP}')

    head_ref = ''
    if mode == 'review':
        pr = gh.get_pr(pr_no)
        if pr.get('state') != 'open':
            return skip('the pull request is not open')
        head_ref = pr['head']['ref']
        issue_no = linked_issue(pr, explicit_issue, cfg.get('repository.branch_prefix', 'autose/issue-'))
        if not issue_no:
            return skip('no linked issue', pr_no, 'Which issue does this PR address? Comment `/autose review #N`.')
        issue = gh.get_issue(issue_no)
    elif issue is None or event_name == 'workflow_dispatch':
        issue = gh.get_issue(issue_no)
    if mode == 'full' and issue.get('state') == 'closed':
        return skip('the issue is closed', issue_no, 'AutoSE does not run on closed issues.')

    body, report = sanitize(issue.get('body'))
    title, _ = sanitize(issue.get('title'), 300)
    notes_clean, _ = sanitize(notes, 4000)
    note = describe(report)
    snapshot = {'number': issue_no, 'title': title, 'body': body, 'author': (issue.get('user') or {}).get('login', ''),
                'url': issue.get('html_url', ''), 'labels': [lbl['name'] for lbl in issue.get('labels') or []],
                'sanitize_report': report, 'sanitize_note': note,
                'trigger': {'event': event_name, 'actor': sender, 'comment': notes_clean}}
    state_dir = Path(env.get('AUTOSE_STATE_DIR') or 'autose-issue')
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'issue.json').write_text(json.dumps(snapshot, indent=2), encoding='utf-8')

    prefix = cfg.get('repository.branch_prefix', 'autose/issue-')
    branch = head_ref if mode == 'review' else f'{prefix}{issue_no}' + ('' if variant == 'baseline' else f'-{variant}')
    for name, (color, description) in LABELS.items():
        try:
            gh.ensure_label(name, color, description)
        except GitHubError:
            pass
    target = pr_no if mode == 'review' else issue_no
    what = f'independent review of PR #{pr_no} (issue #{issue_no})' if mode == 'review' else 'full pipeline'
    status = gh.create_comment(target, (
        '<!-- autose:status -->\n### 🤖 AutoSE run\n'
        f'**Mode:** {what} · **variant:** `{variant}` · **triggered by:** @{sender} via `{event_name}` · '
        f'[Actions run]({run_url})\n\n**Input sanitation:** {note}\n\n'
        '_Each job updates its own section below. The workflow never merges; a person decides._'))
    if comment_id:
        try:
            gh.add_reaction(comment_id, 'eyes')
        except GitHubError:
            pass
    gha.set_outputs(proceed='true', mode=mode, issue=issue_no, pr=pr_no or '', variant=variant, branch=branch,
                    status_comment_id=status.get('id', ''),
                    max_change_requests=int(vcfg.get('pipeline.max_change_requests', 2)),
                    reviewer_context=str(bool(vcfg.get('review.include_implementer_context'))).lower())
    return 0
