"""Final job: one summary per run - status comment, labels, run_summary.json and a
Mermaid sequence diagram generated from the real event log of every job."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from . import events as gha
from .events import EventLog
from .github import GitHub, GitHubError
from .status import StatusBoard

WHO = {'requirements': 'P', 'design': 'P', 'implementation': 'I', 'testing': 'T', 'release': 'H'}


def load_artifacts(root: Path) -> dict:
    events: dict[str, list] = {}
    usage: dict[str, dict] = {}
    states: list[dict] = []
    reviews: list[dict] = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []:
        for f in sorted((folder / 'events').glob('*.jsonl')):
            events.setdefault(f.stem, EventLog.read(f))
        for f in sorted((folder / 'usage').glob('*.json')):
            usage.setdefault(f.stem, json.loads(f.read_text(encoding='utf-8')).get('summary', {}))
        if (folder / 'state.json').exists():
            states.append({'artifact': folder.name, **json.loads((folder / 'state.json').read_text())})
        if (folder / 'review.json').exists():
            reviews.append(json.loads((folder / 'review.json').read_text(encoding='utf-8')))
    reviews.sort(key=lambda r: int(r.get('round') or 0))
    return {'events': events, 'usage': usage, 'states': states, 'reviews': reviews}


def _clean(text: Any, limit: int = 70) -> str:
    return re.sub(r'[#;:<>{}\[\]"`|]', ' ', str(text)).replace('\n', ' ').strip()[:limit]


def sequence(events: list[dict], trigger: str, final: str) -> str:
    lines = ['sequenceDiagram', '  autonumber', '  actor M as Maintainer', '  participant G as Gate job',
             '  participant H as Harness (orchestrator, checks)', '  participant P as Planner agent',
             '  participant I as Implementer agent', '  participant T as Test-author agent',
             '  participant C as CI workflow', '  participant R as Reviewer agent (own job)',
             '  actor U as Human', f'  M->>G: {_clean(trigger)}', '  G->>H: sanitised issue snapshot']
    for e in sorted(events, key=lambda x: x.get('ts', '')):
        kind = e.get('event')
        stage = e.get('stage', '')
        if kind == 'stage' and e.get('status') == 'running' and stage in WHO:
            lines.append(f"  H->>{WHO[stage]}: {stage} (round {e.get('round', 0)}) {_clean(e.get('note', ''), 30)}")
        elif kind == 'check' and stage in WHO:
            mark = 'passed' if e.get('passed') else 'failed'
            lines.append(f'  {WHO[stage]}-->>H: submit for the {stage} check')
            lines.append(f'  Note over H: {stage} check {mark}')
        elif kind == 'stage' and e.get('status') in ('looped', 'failed'):
            lines.append(f"  Note over H: {stage} {e.get('status')} - {_clean(e.get('note', ''), 50)}")
        elif kind == 'ci_finished':
            lines.append('  H->>C: workflow_dispatch on the PR branch')
            lines.append(f"  C-->>H: {_clean(e.get('conclusion'))}")
        elif kind == 'pushed':
            lines.append('  Note over H: commit and push, open or update the PR')
        elif kind == 'review_submitted':
            lines.append(f"  H->>R: review round {e.get('round')} (issue + diff + repo only)")
            lines.append(f"  R-->>H: {_clean(e.get('verdict'))} target {_clean(e.get('target'))}")
        elif kind == 'stopped':
            lines.append(f'  Note over H: stopped at {_clean(stage)}')
    lines.append(f'  H->>U: {_clean(final)}')
    return '\n'.join(lines) + '\n'


def check_stats(events: list[dict]) -> tuple[dict, list]:
    stats: dict[str, dict] = {}
    loops = []
    for e in events:
        if e.get('event') == 'check' and e.get('stage'):
            key = e['stage'] + (f":{e['what']}" if e.get('what') else '')
            slot = stats.setdefault(key, {'passed': 0, 'failed': 0})
            slot['passed' if e.get('passed') else 'failed'] += 1
        elif e.get('event') == 'stage' and e.get('status') == 'looped':
            loops.append({'stage': e.get('stage'), 'round': e.get('round'), 'note': e.get('note', '')})
    return stats, loops


def outcome(needs: dict, reviews: list[dict]) -> tuple[str, dict]:
    jobs = [('build', 0), ('rework_1', 1), ('rework_2', 2)]
    builds = [(n, (needs.get(job) or {})) for job, n in jobs if (needs.get(job) or {}).get('result') not in (None, 'skipped')]
    last_build = builds[-1][1] if builds else {}
    last_round_built = builds[-1][0] if builds else -1
    last_review = reviews[-1] if reviews else None
    if any(b.get('result') == 'failure' for _, b in builds):
        return 'error', last_build
    if (last_build.get('outputs') or {}).get('status') == 'stopped':
        return 'stopped', last_build
    if last_review is None:
        return ('error' if builds else 'skipped'), last_build
    if int(last_review.get('round') or 0) <= last_round_built:
        return 'error', last_build
    verdict = last_review.get('verdict')
    return {'approve': 'approved', 'request_changes': 'changes_requested'}.get(verdict, 'review_error'), last_build


def run_report(cfg: Any, secrets: Any) -> int:
    env = os.environ
    needs = json.loads(env.get('AUTOSE_NEEDS_JSON') or '{}')
    gate = (needs.get('gate') or {}).get('outputs') or {}
    data = load_artifacts(Path(env.get('AUTOSE_ARTIFACTS_DIR') or 'artifacts'))
    result, last_build = outcome(needs, data['reviews'])
    states = sorted(data['states'], key=lambda s: s.get('artifact', ''))
    pr = (last_build.get('outputs') or {}).get('pr') or gate.get('pr') or next(
        (s.get('pr_number') for s in reversed(states) if s.get('pr_number')), '')
    pr_url = (last_build.get('outputs') or {}).get('pr_url') or next(
        (s.get('pr_url') for s in reversed(states) if s.get('pr_url')), '')
    totals = {'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0, 'total': 0}
    cost = 0.0
    for summary in data['usage'].values():
        for key in totals:
            totals[key] += int((summary.get('totals') or {}).get(key, 0))
        cost += float(summary.get('est_cost_usd') or 0)
    all_events = [e for evs in data['events'].values() for e in evs]
    reviews = [{'round': r.get('round'), 'verdict': r.get('verdict'), 'target_stage': r.get('target_stage'),
                'model': r.get('model'), 'facts': r.get('facts'), 'url': r.get('review_url', ''), 'error': (r.get('error') or '')[:300]}
               for r in data['reviews']]
    run_url = f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{env.get('GITHUB_REPOSITORY')}/actions/runs/{env.get('GITHUB_RUN_ID')}"
    titles = {'approved': '✅ the independent reviewer approved',
              'changes_requested': '🔁 the reviewer still requests changes and the review rounds are used up',
              'stopped': '⛔ a stage could not pass its check and the run stopped',
              'review_error': '⚠️ the review could not be completed', 'error': '⚠️ a job failed (see the run)',
              'skipped': 'nothing ran'}
    summary = {'run_id': env.get('GITHUB_RUN_ID'), 'run_url': run_url, 'repository': env.get('GITHUB_REPOSITORY'),
               'mode': gate.get('mode'), 'issue': gate.get('issue'), 'variant': gate.get('variant'),
               'branch': gate.get('branch'), 'pr': pr, 'pr_url': pr_url, 'outcome': result,
               'stopped_stage': (last_build.get('outputs') or {}).get('stopped_stage', ''),
               'stop_reason': (last_build.get('outputs') or {}).get('reason', ''),
               'jobs': {name: {'result': v.get('result'), 'outputs': v.get('outputs')} for name, v in needs.items()},
               'reviews': reviews, 'progress': {s['artifact']: s.get('progress') for s in states},
               'tokens': totals, 'est_cost_usd': round(cost, 4), 'usage_by_job': data['usage']}
    summary['head_sha'] = (last_build.get('outputs') or {}).get('head_sha') or next(
        (st.get('head_sha') for st in reversed(states) if st.get('head_sha')), '')
    summary['check_stats'], summary['loops'] = check_stats(all_events)
    summary['ci_runs'] = next((st.get('ci_runs') for st in reversed(states) if st.get('ci_runs')), [])
    final = f"{titles.get(result, result)}" + (f' - PR {pr}' if pr else '')
    out_dir = Path(env.get('AUTOSE_STATE_DIR') or 'run-summary')
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'run_summary.json').write_text(json.dumps(summary, indent=2, default=str), encoding='utf-8')
    trigger = f"{gate.get('mode')} on #{gate.get('issue')} ({gate.get('variant')})"
    (out_dir / 'sequence.mmd').write_text(sequence(all_events, trigger, final), encoding='utf-8')

    lines = [f'### AutoSE finished: {titles.get(result, result)}', '']
    if pr_url:
        lines.append(f'**Pull request:** {pr_url}')
    if summary['stopped_stage']:
        lines.append(f"**Stopped at:** `{summary['stopped_stage']}` - {summary['stop_reason']}")
    if reviews:
        lines.append('**Reviews:** ' + ' → '.join(f"round {r['round']}: {r['verdict']}" for r in reviews))
    lines.append(f"**Tokens:** {totals['total']:,} (uncached in {totals['input']:,}, out {totals['output']:,}, "
                 f"cache read {totals['cache_read']:,}, cache write {totals['cache_write']:,}); est. ${cost:.2f}")
    lines += ['', '👤 **Human decision required:** review the pull request and merge or close it. '
                  'AutoSE never merges.', '', f'[Run]({run_url}) · artifacts: `autose-*`, summary: `autose-run-summary`']
    text = '\n'.join(lines)
    gha.step_summary(text + '\n\n```mermaid\n' + (out_dir / 'sequence.mmd').read_text() + '```\n')
    if secrets.github_token:
        gh = GitHub(secrets.github_token, env['GITHUB_REPOSITORY'])
        StatusBoard(gh, gate.get('status_comment_id'), 'final').update(text)
        try:
            if pr:
                for label in ('autose:review-pending', 'autose:needs-human'):
                    gh.remove_label(int(pr), label)
                if result != 'approved':
                    gh.add_labels(int(pr), ['autose:needs-human'])
            target = gate.get('issue') if gate.get('mode') == 'full' else pr
            if target:
                gh.create_comment(int(target), text)
        except (GitHubError, ValueError) as exc:
            gha.warning(f'could not post the final summary: {exc}')
    return 0
