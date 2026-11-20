"""Stage 6 · Independent review, in its own job with a read-only token.

Three steps keep the model away from credentials:
  prepare (token, read-only)  ->  review (no token at all, LLM)  ->  post (token, comment only)
The reviewer sees the issue, the PR diff and the repository at the PR head.
It never sees spec.md, design.md, test_report.md, pr.md or any transcript,
unless the `reviewer-sees-rationale` experiment variant is selected.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from . import events as gha
from . import difffacts, prompts, render
from .config import Config
from .context import build_ctx
from .github import GitHub, GitHubError
from .sandbox import truncate_middle
from .stages import agent_failure, issue_block
from .tools import ToolOutput, Workspace, command_handler, diff_handler, file_handlers
from .untrusted import wrap
from .usage import BudgetExceeded

VERDICT_LABEL = {'approve': 'autose:approved', 'request_changes': 'autose:changes-requested',
                 'error': 'autose:needs-human'}


def review_problems(args: dict, commands: list, require_evidence: bool = True, require_probe: bool = False,
                    probe_markers: tuple = ('compare.sh', '$SCRATCH')) -> list[str]:
    """Evidence gate: why a submitted review contradicts itself (empty list = consistent)."""
    problems = []
    test_runs = [c for c in commands if re.search(r'\bpytest\b', c.command) and not c.denied]
    blocking = [f for f in args.get('findings') or [] if f.get('severity') in ('blocker', 'major')]
    if args['verdict'] == 'approve':
        if require_evidence and not test_runs:
            problems.append('run the test suite yourself before approving')
        elif require_evidence and test_runs[-1].exit_code != 0:
            problems.append('your most recent pytest run failed; approving would contradict it')
        unmet = [i['requirement'] for i in args.get('issue_checklist') or [] if i.get('status') != 'met']
        if unmet:
            problems.append('approval needs every checklist item met; not met/unclear: ' + '; '.join(unmet))
        if blocking:
            problems.append('approval is inconsistent with blocker/major findings')
        if require_probe and not any(any(m in c.command for m in probe_markers) and not c.denied for c in commands):
            problems.append('run at least one probe of your own before approving (for example '
                            '`bash $SCRATCH/compare.sh probe.py`)')
    else:
        if not blocking:
            problems.append('request_changes needs at least one blocker or major finding (or approve and '
                            'list minor issues as minor/nit)')
        if args.get('target_stage') not in ('requirements', 'design', 'implementation'):
            problems.append('target_stage must be requirements, design or implementation')
    return problems


def _dir() -> Path:
    path = Path(os.environ.get('AUTOSE_REVIEW_DIR') or 'review-in')
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare(cfg: Config, secrets: Any) -> int:
    repo, number = os.environ['GITHUB_REPOSITORY'], int(os.environ['AUTOSE_PR'])
    gh = GitHub(secrets.github_token, repo)
    pr = gh.get_pr(number)
    head_repo = ((pr.get('head') or {}).get('repo') or {}).get('full_name')
    if pr['base']['repo']['full_name'] != repo or head_repo != repo:
        gha.error('Only pull requests from branches of this repository are reviewed.')
        return 1
    if pr.get('state') != 'open':
        gha.error(f'PR #{number} is {pr.get("state")}')
        return 1
    meta = {'number': number, 'head_sha': pr['head']['sha'], 'base_sha': pr['base']['sha'],
            'head_ref': pr['head']['ref'], 'base_ref': pr['base']['ref'], 'title': pr.get('title', ''),
            'url': pr.get('html_url', ''), 'additions': pr.get('additions'), 'deletions': pr.get('deletions'),
            'changed_files': pr.get('changed_files')}
    if cfg.get('review.include_implementer_context'):
        meta['body'] = pr.get('body') or ''      # experiment variant only
    (_dir() / 'pr.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')
    (_dir() / 'diff.patch').write_text(gh.pr_diff(number), encoding='utf-8')
    gha.set_outputs(head_sha=meta['head_sha'])
    return 0


def _context_block(cfg: Config, meta: dict) -> str:
    if not cfg.get('review.include_implementer_context'):
        return ''
    parts = []
    ctx_dir = Path(os.environ.get('AUTOSE_INPUT_DIR', '')) / 'context' / 'artifacts'
    for name in ('design.md', 'pr.md'):
        path = ctx_dir / name
        if path.exists():
            parts.append(wrap('implementer_' + name.replace('.', '_'), truncate_middle(path.read_text(), 15000)))
    if meta.get('body') and not parts:
        parts.append(wrap('pr_description', truncate_middle(meta['body'], 15000)))
    if not parts:
        return ''
    return ("## The implementer's own account (experiment variant)\nThe authors of this change describe it as "
            'follows. Judge the code, not the description.\n\n' + '\n\n'.join(parts))


def review(cfg: Config, secrets: Any, make_llm: Any = None) -> int:
    round_no = int(os.environ.get('AUTOSE_ROUND') or 1)
    ctx = build_ctx(f'review-r{round_no}', cfg, secrets, use_github=False, make_llm=make_llm)
    meta = json.loads((_dir() / 'pr.json').read_text(encoding='utf-8'))
    diff_path = _dir() / 'diff.patch'
    scratch = ctx.scratch / 'review-scratch'
    ctx.sandbox.extra_env['SCRATCH'] = str(scratch)
    ws = Workspace(ctx.repo_dir, cfg.get('target.hidden_globs') or [], ctx.protected_globs, [], scratch=scratch)
    diff_text = diff_path.read_text(encoding='utf-8', errors='replace')
    base_dir, base_status = scratch / 'base', 'not provided in this variant'
    if cfg.get('review.base_tree'):
        error = difffacts.build_base_tree(ctx.repo_dir, diff_path, base_dir)
        if error:
            base_status = f'unavailable ({error})'
        else:
            difffacts.write_compare_script(scratch)
            base_status = 'rebuilt from the diff at $BASE (scratch/base for read_file)'
    ctx.sandbox.extra_env.update(REPO=str(ctx.repo_dir), BASE=str(base_dir))
    diff_facts, facts_md = {}, ''
    if cfg.get('review.diff_facts'):
        issue_text = f"{ctx.issue.get('title', '')}\n{ctx.issue.get('body', '')}"
        diff_facts = difffacts.compute(diff_text, ctx.repo_dir, base_dir if base_dir.is_dir() else None, issue_text,
                                       ctx.source_globs, cfg.get('target.test_dir') or 'tests', ctx.doc_globs)
        facts_md = difffacts.markdown(diff_facts, base_status)
    require_probe = bool(cfg.get('review.require_probe', False))
    probe_markers = ('compare.sh', '$SCRATCH', '${SCRATCH}', str(scratch))
    commands: list = []
    require_evidence = bool(cfg.get('review.require_evidence', True))
    seen = {'n': 0}

    def submit_review(args: dict) -> ToolOutput:
        seen['n'] += 1
        problems = review_problems(args, commands, require_evidence, require_probe, probe_markers)
        if not problems:
            if args['verdict'] == 'approve':
                args['target_stage'] = 'none'
            return ToolOutput('Review recorded.', done=True, outcome='accepted', payload=args)
        if seen['n'] < 4:
            return ToolOutput('The review is inconsistent:\n- ' + '\n- '.join(problems)
                              + '\nFix it and call submit_review again.', True)
        # Never record a verdict that contradicts the reviewer's own evidence: a person decides instead.
        return ToolOutput('The review is still inconsistent, so no verdict is recorded.', True, True,
                          'inconsistent', payload={'problems': problems})

    handlers = {**file_handlers(ws, write=True), 'run_command': command_handler(ctx.sandbox, commands),
                'get_diff': diff_handler(diff_path), 'submit_review': submit_review}
    diff_lines = len(diff_path.read_text(encoding='utf-8', errors='replace').splitlines())
    task = '\n\n'.join(filter(None, [
        f"Review pull request #{meta['number']} for issue #{ctx.issue.get('number')}.",
        issue_block(ctx),
        f"PR title: {wrap('pr_title', meta.get('title', ''))}\n"
        f"{meta.get('changed_files')} file(s) changed, +{meta.get('additions')} -{meta.get('deletions')}. "
        f"The diff has {diff_lines} lines: read it with get_diff. The repository is checked out at the PR head "
        f"({str(meta['head_sha'])[:12]}).",
        _context_block(cfg, meta),
        facts_md,
        ('The code before the change is rebuilt from the diff at $BASE (read it as scratch/base/...). '
         '`bash $SCRATCH/compare.sh <script>` runs one of your probe scripts on the PR head and on that code.')
        if base_dir.is_dir() else '',
        f"The full suite runs with: `{cfg.req('target.test')}`. Probe files you write under scratch/ live at "
        '$SCRATCH for run_command (e.g. `python -m pytest -q -p no:cacheprovider -o addopts= $SCRATCH/test_probe.py`).',
        'Follow your review procedure, then call submit_review.',
    ]))
    error, payload = '', None
    try:
        result = ctx.run_agent('reviewer', 'review', prompts.system(cfg.get('review.prompt') or 'reviewer'), handlers, 'submit_review', task)
        if result.outcome == 'accepted':
            payload = result.payload
        elif result.outcome == 'inconsistent':
            error = ('the reviewer submitted a review that contradicts its own evidence four times, so no verdict '
                     'was recorded: ' + '; '.join(result.payload['problems']))
        else:
            error = agent_failure(result, 'reviewer')
    except BudgetExceeded as exc:
        error = f'budget exhausted: {exc}'
    except Exception as exc:  # report, never crash silently
        error = f'{type(exc).__name__}: {exc}'
    test_runs = [c for c in commands if re.search(r'\bpytest\b', c.command) and not c.denied]
    facts = {'reviewer_test_runs': len(test_runs),
             'reviewer_tests_passed': bool(test_runs) and test_runs[-1].exit_code == 0,
             'commands_run': len(commands), 'ci_conclusion': '{{CI}}'}
    data = {'round': round_no, 'pr': meta['number'], 'head_sha': meta['head_sha'], 'issue': ctx.issue.get('number'),
            'model': cfg.agent('reviewer')['model'], 'variant': cfg.variant,
            'implementer_context': bool(cfg.get('review.include_implementer_context')),
            'verdict': payload['verdict'] if payload else 'error',
            'target_stage': payload['target_stage'] if payload else 'none',
            'review': payload, 'facts': facts, 'error': error, 'usage': ctx.ledger.summary(),
            'prompt': cfg.get('review.prompt') or 'reviewer', 'diff_facts': diff_facts, 'diff_facts_md': facts_md}
    ctx.events.emit('review_submitted', verdict=data['verdict'], target=data['target_stage'], round=round_no)
    (ctx.state.root / 'review.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
    ctx.state.write_artifact('review.md', render.review_md(data, payload, facts, error))
    ctx.ledger.save()
    return 0


def post(cfg: Config, secrets: Any) -> int:
    repo = os.environ['GITHUB_REPOSITORY']
    state_dir = Path(os.environ['AUTOSE_STATE_DIR'])
    data = json.loads((state_dir / 'review.json').read_text(encoding='utf-8'))
    gh = GitHub(secrets.github_token, repo)
    conclusion = 'none'
    workflow = cfg.get('pipeline.ci_workflow')
    if workflow:
        try:
            runs = gh.workflow_runs(workflow, head_sha=data['head_sha'])
            done = [r for r in runs if r.get('status') == 'completed']
            conclusion = (done[0].get('conclusion') if done else (runs[0].get('status') if runs else 'none')) or 'none'
        except GitHubError as exc:
            conclusion = f'unknown ({exc.status})'
    data['facts']['ci_conclusion'] = conclusion
    markdown = render.review_md(data, data.get('review'), data['facts'], data.get('error', ''))
    (state_dir / 'artifacts').mkdir(parents=True, exist_ok=True)
    (state_dir / 'artifacts' / 'review.md').write_text(markdown, encoding='utf-8')
    posted = gh.create_review(int(data['pr']), markdown, commit_id=data['head_sha']) or {}
    data['review_url'] = posted.get('html_url', '')
    (state_dir / 'review.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
    for label in VERDICT_LABEL.values():
        gh.ensure_label(label, '0e8a16' if label.endswith('approved') else 'fbca04', 'AutoSE review verdict')
        gh.remove_label(int(data['pr']), label)
    gh.remove_label(int(data['pr']), 'autose:review-pending')
    gh.add_labels(int(data['pr']), [VERDICT_LABEL[data['verdict']]])
    gha.set_outputs(verdict=data['verdict'], target_stage=data['target_stage'])
    gha.step_summary(f"### Review round {data['round']}: {data['verdict']} → {data['target_stage']}\n")
    return 0
