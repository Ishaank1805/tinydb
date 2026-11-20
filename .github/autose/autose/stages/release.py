"""Stage 5 · Release (deterministic, no LLM): commit, push, open/update the PR, run CI -> pr.md.

Check: the pull request is open and CI passes. CI is started with
workflow_dispatch because pushes made with GITHUB_TOKEN do not trigger
``on: push/pull_request`` workflows.
"""
from __future__ import annotations

import os
import time
from typing import Any

from .. import render
from ..github import GitHubError
from ..tools import matches
from . import StageOutcome

VERDICT_LABELS = ['autose:approved', 'autose:changes-requested', 'autose:needs-human', 'autose:stopped']


def run_ci(ctx: Any, head_sha: str) -> dict:
    workflow = ctx.cfg.get('pipeline.ci_workflow')
    if not workflow:
        return {'conclusion': 'skipped', 'url': '', 'note': 'no CI workflow configured'}
    poll = int(os.environ.get('AUTOSE_CI_POLL_SECONDS', '15'))
    cid = f'autose-{ctx.run.run_id or "local"}-i{ctx.run.issue}-r{ctx.run.round}-{int(time.time())}'
    ctx.gh.dispatch(workflow, ctx.run.branch, {'correlation_id': cid})
    ctx.events.emit('ci_dispatched', workflow=workflow, correlation_id=cid)
    deadline = time.time() + 60 * int(ctx.cfg.get('pipeline.ci_timeout_minutes', 20))
    found = None
    while found is None and time.time() < deadline:
        time.sleep(poll)
        for item in ctx.gh.workflow_runs(workflow, branch=ctx.run.branch, event='workflow_dispatch'):
            if cid in (item.get('display_title') or ''):
                found = item
                break
    if found is None:
        return {'conclusion': 'not_found', 'url': '', 'note': 'the dispatched CI run never appeared'}
    while found.get('status') != 'completed' and time.time() < deadline:
        time.sleep(poll)
        found = ctx.gh.get_run(found['id'])
    result = {'conclusion': found.get('conclusion') or 'timed_out', 'url': found.get('html_url', ''),
              'run_id': found.get('id'), 'head_sha': found.get('head_sha')}
    if result['head_sha'] and result['head_sha'] != head_sha:
        result['note'] = f"CI ran on {result['head_sha'][:12]}, expected {head_sha[:12]}"
    if result['conclusion'] != 'success':
        try:
            failed = [j for j in ctx.gh.run_jobs(found['id']) if j.get('conclusion') not in ('success', 'skipped')]
            if failed:
                result['log_tail'] = ctx.gh.job_log(failed[0]['id'])[-6000:]
        except GitHubError as exc:
            result['log_tail'] = f'(could not fetch the CI log: {exc})'
    ctx.events.emit('ci_finished', conclusion=result['conclusion'], url=result['url'])
    return result


def run(ctx: Any, impl_summary: str = '') -> StageOutcome:
    issue_no = ctx.run.issue
    allowed = ctx.source_globs + ctx.doc_globs + [ctx.test_file]
    for rel in ctx.git.changed_files('HEAD'):
        if not matches(rel, allowed) or matches(rel, ctx.protected_globs):
            ctx.git.restore(rel, ctx.git.show_file('HEAD', rel))
            ctx.events.emit('policy_revert', stage='release', files=[rel])
    changed = ctx.git.changed_files('HEAD')
    leaked = ctx.secrets.found_in(ctx.git.diff('HEAD'))
    if leaked:
        return StageOutcome('failed', f'refusing to publish: the diff contains the value of {", ".join(leaked)}')
    if not changed:
        return StageOutcome('failed', 'there are no changes to release' if ctx.run.round == 0
                            else 'the rework produced no changes')
    title = ctx.issue.get('title', '')
    src = [p for p in changed if p != ctx.test_file]
    tests = [p for p in changed if p == ctx.test_file]
    if ctx.run.round == 0:
        ctx.git.commit(f'autose(#{issue_no}): {title}\n\nImplementation for #{issue_no}, AutoSE run '
                       f'{ctx.run.run_id or "local"}.', src)
        ctx.git.commit(f'autose(#{issue_no}): acceptance tests\n\nOne or more tests per acceptance criterion.',
                       tests)
    else:
        ctx.git.commit(f'autose(#{issue_no}): address review round {ctx.run.round}', changed)
    head = ctx.git.head()
    spec = ctx.state.read_data('spec') or {}
    design = ctx.state.read_data('design') or {}
    tests_eval = ctx.state.read_data('test_results') or {}
    if ctx.gh is None:
        body = render.pr_md(ctx, spec, design, tests_eval, impl_summary, None)
        ctx.state.write_artifact('pr.md', body + '\n\n---\n_Local dry run: committed, not pushed._\n')
        ctx.state.data.update(head_sha=head)
        return StageOutcome('passed', f'local dry run: committed {head[:12]}', {'head_sha': head})
    ctx.git.push(ctx.run.branch, force=ctx.run.round == 0)
    ctx.events.emit('pushed', branch=ctx.run.branch, head=head)
    default_branch = ctx.state.data.get('default_branch') or ctx.gh.get_repo()['default_branch']
    pr_title = f'AutoSE #{issue_no}: {title}'[:200] + (f' [{ctx.run.variant}]' if ctx.run.variant != 'baseline'
                                                     else '')
    body = render.pr_md(ctx, spec, design, tests_eval, impl_summary, None)
    pr = ctx.gh.find_pr(ctx.run.branch)
    pr = (ctx.gh.update_pr(pr['number'], title=pr_title, body=body) if pr
          else ctx.gh.create_pr(ctx.run.branch, default_branch, pr_title, body))
    for label in ['autose', 'autose:review-pending']:
        ctx.gh.ensure_label(label, '1d76db', 'AutoSE')
    for label in VERDICT_LABELS:
        ctx.gh.remove_label(pr['number'], label)
    ctx.gh.add_labels(pr['number'], ['autose', 'autose:review-pending'])
    ctx.state.data.update(pr_number=pr['number'], pr_url=pr['html_url'], head_sha=head)
    ctx.state.save()
    ci = run_ci(ctx, head)
    ctx.state.data.setdefault('ci_runs', []).append({'round': ctx.run.round, 'url': ci.get('url', ''),
                                                     'conclusion': ci.get('conclusion')})
    body = render.pr_md(ctx, spec, design, tests_eval, impl_summary, ci)
    pr = ctx.gh.update_pr(pr['number'], body=body)
    ctx.state.write_artifact('pr.md', body + f"\n\n---\nPull request: {pr['html_url']}\n",
                             tag=f'r{ctx.run.round}')
    data = {'pr': pr['number'], 'pr_url': pr['html_url'], 'head_sha': head, 'ci': ci}
    if pr.get('state') != 'open':
        return StageOutcome('failed', f"the pull request is {pr.get('state')}", data)
    if ci['conclusion'] not in ('success', 'skipped'):
        feedback = (f"CI on the pull request ended with `{ci['conclusion']}` ({ci.get('url', '')}).\n"
                    + (f"```\n{ci.get('log_tail', '')[-4000:]}\n```" if ci.get('log_tail') else ''))
        return StageOutcome('ci_failed', f"CI {ci['conclusion']}", {**data, 'feedback': feedback})
    return StageOutcome('passed', f"PR #{pr['number']} is open, CI {ci['conclusion']}", data)
