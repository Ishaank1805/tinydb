"""Build / rework job: a deterministic state machine over stages 1-5.

The orchestrator, not an agent, decides every transition. It uses only check
results, the implementer's explicit requests (redesign / dispute) and the
reviewer's structured verdict, and every loop is bounded by a counter.
"""
from __future__ import annotations

import json
import os
import traceback
from pathlib import Path
from typing import Any

from . import events as gha
from . import render
from .github import GitHubError
from .gitops import GitError
from .llm import LLMError
from .pytest_runner import collect, run_tests
from .stages import StageOutcome, design, implementation, release, requirements, testing
from .usage import BudgetExceeded


def review_feedback(review: dict) -> str:
    body = review.get('review') or {}
    lines = [f"The independent reviewer requested changes (round {review.get('round')}) and routed the work "
             f"back to **{body.get('target_stage')}**.", '', body.get('summary', '')]
    for f in body.get('findings') or []:
        where = f"{f.get('file', '')}" + (f":{f['line']}" if f.get('line') else '')
        lines.append(f"- [{f.get('severity')}/{f.get('category')}] {where}: {f.get('description')} "
                     f"Suggestion: {f.get('suggestion', '-')}")
    unmet = [i for i in body.get('issue_checklist') or [] if i.get('status') != 'met']
    if unmet:
        lines.append('\nRequirements the reviewer considers unmet:')
        lines += [f"- {i.get('requirement')} ({i.get('status')}): {i.get('evidence')}" for i in unmet]
    return '\n'.join(lines)


def preflight(ctx: Any) -> StageOutcome:
    """Record the base commit and prove the existing suite is green before spending tokens."""
    if ctx.run.round == 0:
        ctx.state.data.update(base_sha=ctx.git.head(), issue=ctx.run.issue, branch=ctx.run.branch,
                              variant=ctx.run.variant)
        ids, ok, out = collect(ctx.sandbox, ctx.cfg.req('target.collect'))
        if not ok or not ids:
            return StageOutcome('failed', 'the existing test suite cannot be collected on the base commit:\n'
                                + out[-1500:])
        suite = run_tests(ctx.sandbox, ctx.cfg.req('target.test'), ctx.scratch / 'junit-baseline.xml')
        if not suite.ok:
            return StageOutcome('failed', f'the existing test suite is red on the base commit ({suite.summary()})')
        ctx.state.data['baseline_tests'] = sorted(ids)
    elif not ctx.state.data.get('base_sha'):
        return StageOutcome('failed', 'the state of the previous round is missing (no base commit recorded)')
    else:
        ctx.git.ensure_commit(ctx.base_sha)
    ctx.git.checkout_branch(ctx.run.branch)
    if ctx.gh and not ctx.state.data.get('default_branch'):
        ctx.state.data['default_branch'] = ctx.gh.get_repo().get('default_branch', 'main')
    ctx.state.save()
    return StageOutcome('passed', f"base {ctx.base_sha[:12]}, {len(ctx.state.data.get('baseline_tests', []))} "
                                  'existing tests')


STAGE_ARTIFACT = {'requirements': 'spec.md', 'design': 'design.md', 'implementation': 'diff.patch',
                  'testing': 'test_report.md', 'release': 'pr.md'}


def explain_in_artifact(ctx: Any, stage: str, reason: str) -> None:
    """A stage that stops explains why in its own artifact (checks, budgets, model or CI failures alike)."""
    name = STAGE_ARTIFACT.get(stage)
    if not name:
        return
    path = ctx.state.artifact_path(name)
    current = path.read_text(encoding='utf-8') if path.exists() else ''
    if 'the run stopped here' in current or current.startswith('# AutoSE stopped'):
        return                                   # the stage already explained it
    if name == 'diff.patch':                     # comment lines before the diff keep it a valid patch
        lines = [f'AutoSE stopped at stage {stage} (round {ctx.run.round}).', *reason.splitlines()[:30]]
        ctx.state.write_artifact(name, ''.join(f'# {line}\n' for line in lines) + current, tag='stopped')
        return
    body = current.rstrip() if current else render.header(ctx, f'{name} (stage: {stage})')
    ctx.state.write_artifact(name, body + '\n' + render.stopped(reason), tag='stopped')


def stop(ctx: Any, stage: str, reason: str, questions: list | None = None) -> dict:
    explain_in_artifact(ctx, stage, reason)
    ctx.events.emit('stopped', stage=stage, reason=reason[:500], round=ctx.run.round)
    ctx.state.data.update(status='stopped', stopped_stage=stage, stop_reason=reason)
    ctx.state.save()
    gha.warning(f'AutoSE stopped at {stage}: {reason[:300]}')
    if ctx.gh:
        body = [f'⛔ **AutoSE stopped at stage `{stage}`** (round {ctx.run.round}, variant `{ctx.run.variant}`).',
                '', reason]
        if questions:
            body += ['', '**Questions for the maintainer:**'] + [f'- {q}' for q in questions]
        body += ['', f'The `{stage}` artifact explains why ([run]({ctx.run.run_url})). After clarifying the issue, '
                     'comment `/autose` to try again.']
        try:
            ctx.gh.create_comment(ctx.run.issue, '\n'.join(body))
            ctx.gh.ensure_label('autose:stopped', 'b60205', 'AutoSE stopped; see the comment')
            ctx.gh.add_labels(ctx.run.issue, ['autose:stopped'])
        except GitHubError as exc:
            ctx.events.emit('comment_failed', error=str(exc)[:300])
    return {'status': 'stopped', 'stopped_stage': stage, 'reason': reason}


def build(ctx: Any, review: dict | None = None) -> dict:
    cfg = ctx.cfg
    max_impl = int(cfg.get('pipeline.max_impl_attempts', 3))
    max_redesigns = int(cfg.get('pipeline.max_redesigns', 1))
    max_disputes = int(cfg.get('pipeline.max_test_revisions', 1))
    impl_attempts = redesigns = disputes = 0
    stage = ctx.run.start_stage
    review_fb = review_feedback(review) if review else ''
    design_fb = impl_fb = ''
    need_tests = ctx.run.round == 0 or stage in ('requirements', 'design')
    if review and any(f.get('category') == 'tests' for f in (review.get('review') or {}).get('findings') or []):
        need_tests = True
    test_reason = 'The reviewer found problems with the tests:\n' + review_fb if review and need_tests else ''
    dispute: dict | None = None
    impl_summary = ''

    pre = preflight(ctx)
    if not pre.passed:
        return stop(ctx, 'preflight', pre.message)
    ctx.events.emit('preflight', message=pre.message)

    def joined(*parts: str) -> str:
        return '\n\n'.join(p for p in parts if p)

    while True:
        ctx.budget.check()
        if stage == 'requirements':
            ctx.mark('requirements', 'running')
            out = requirements.run(ctx, review_fb)
            ctx.mark('requirements', 'passed' if out.passed else 'failed', out.message)
            if not out.passed:
                return stop(ctx, 'requirements', out.message, out.data.get('questions'))
            stage, need_tests = 'design', True

        elif stage == 'design':
            ctx.mark('design', 'running', f'redesign {redesigns}' if redesigns else '')
            if ctx.git.changed_files('HEAD'):
                discarded = implementation.source_diff(ctx)
                n = len(list((ctx.state.root / 'history').glob('discarded-*.patch'))) + 1
                (ctx.state.root / 'history' / f'discarded-{n:02d}.patch').write_text(discarded or '')
                ctx.git.reset_to_head()
            out = design.run(ctx, joined(review_fb, design_fb))
            ctx.mark('design', 'passed' if out.passed else 'failed', out.message)
            if not out.passed:
                return stop(ctx, 'design', out.message)
            stage, need_tests, impl_attempts, impl_fb = 'implementation', True, 0, ''

        elif stage == 'implementation':
            if impl_attempts >= max_impl:
                if redesigns < max_redesigns:
                    redesigns += 1
                    design_fb = (f'The implementation failed {impl_attempts} time(s) against the previous design. '
                                 f'Last failure:\n{impl_fb}')
                    ctx.mark('implementation', 'looped', 'attempts used up: back to design')
                    stage = 'design'
                    continue
                return stop(ctx, 'testing', f'still failing after {impl_attempts} implementation attempt(s) and '
                                            f'{redesigns} redesign(s):\n{impl_fb[:1500]}')
            impl_attempts += 1
            ctx.mark('implementation', 'running', f'attempt {impl_attempts}/{max_impl}')
            out = implementation.run(ctx, impl_attempts, joined(review_fb, impl_fb),
                                     tests_exist=(ctx.repo_dir / ctx.test_file).exists())
            if out.status == 'redesign':
                if redesigns < max_redesigns:
                    redesigns += 1
                    design_fb = 'The implementer asked for a redesign:\n' + out.message
                    ctx.mark('implementation', 'looped', 'implementer requested a redesign')
                    stage = 'design'
                    continue
                impl_fb = ('You asked for a redesign, but the redesign budget is used up. Implement the current '
                           'design as well as you can.')
                continue
            if out.status == 'dispute':
                if disputes < max_disputes:
                    disputes += 1
                    dispute, need_tests = out.data, True
                    ctx.mark('implementation', 'looped', f"disputed {out.data.get('test_name')}")
                    stage = 'testing'
                    continue
                impl_fb = 'Your test dispute was not accepted (dispute budget used up). Make the tests pass.'
                continue
            if not out.passed:
                ctx.mark('implementation', 'failed', out.message)
                impl_fb = f'The previous implementation attempt did not finish with a passing build: {out.message}'
                continue
            impl_summary = out.data.get('summary', '')
            ctx.mark('implementation', 'passed', f'attempt {impl_attempts}: the code builds')
            stage = 'testing'

        elif stage == 'testing':
            ctx.mark('testing', 'running')
            out = testing.run(ctx, need_tests, impl_attempts, reason=test_reason, dispute=dispute)
            need_tests, dispute, test_reason = False, None, ''
            if out.status == 'tests_invalid':
                ctx.mark('testing', 'failed', out.message)
                return stop(ctx, 'testing', out.message)
            if out.passed:
                ctx.mark('testing', 'passed', out.message)
                stage = 'release'
                continue
            ctx.mark('testing', 'looped', out.message)
            impl_fb = out.data.get('feedback', out.message)
            stage = 'implementation'

        elif stage == 'release':
            ctx.mark('release', 'running')
            out = release.run(ctx, impl_summary)
            if out.passed:
                ctx.mark('release', 'passed', out.message)
                ctx.state.data.update(status='released', pr_number=out.data.get('pr'),
                                      pr_url=out.data.get('pr_url'), head_sha=out.data.get('head_sha'))
                ctx.state.save()
                return {'status': 'released', **out.data}
            if out.status == 'ci_failed' and impl_attempts < max_impl:
                ctx.mark('release', 'looped', out.message)
                impl_fb, stage = out.data.get('feedback', out.message), 'implementation'
                continue
            ctx.mark('release', 'failed', out.message)
            return stop(ctx, 'release', out.message)
        else:
            return stop(ctx, 'orchestrator', f'unknown start stage {stage!r}')


def run_job(ctx: Any) -> int:
    review = None
    review_file = Path(os.environ.get('AUTOSE_INPUT_DIR') or ctx.state.root.parent / 'autose-in') / 'review' / 'review.json'
    if ctx.run.round > 0 and review_file.exists():
        review = json.loads(review_file.read_text(encoding='utf-8'))
    code, result = 0, {}
    try:
        result = build(ctx, review)
    except BudgetExceeded as exc:
        result = stop(ctx, ctx.state.data.get('current_stage', 'unknown'), f'budget exhausted: {exc}')
    except LLMError as exc:
        result = stop(ctx, ctx.state.data.get('current_stage', 'unknown'), f'model provider error: {exc}')
    except (GitHubError, GitError) as exc:
        ctx.events.emit('error', error=str(exc)[:800])
        gha.error(f'AutoSE infrastructure error: {exc}')
        result, code = {'status': 'error', 'reason': str(exc)[:500]}, 1
    except Exception as exc:  # harness bug: fail loudly so it gets fixed
        ctx.events.emit('error', error=traceback.format_exc()[-3000:])
        gha.error(f'AutoSE internal error: {type(exc).__name__}: {exc}')
        result, code = {'status': 'error', 'reason': f'{type(exc).__name__}: {exc}'[:500]}, 1
    finally:
        ctx.ledger.save()
        ctx.state.save()
    gha.set_outputs(status=result.get('status', 'error'), pr=result.get('pr', ''),
                    pr_url=result.get('pr_url', ''), head_sha=result.get('head_sha', ''),
                    stopped_stage=result.get('stopped_stage', ''),
                    reason=str(result.get('reason', ''))[:300].replace('\n', ' '))
    usage = ctx.ledger.summary()
    gha.step_summary(f"### AutoSE {ctx.run.job}: {result.get('status')}\n\n"
                     f"- issue #{ctx.run.issue} · round {ctx.run.round} · variant `{ctx.run.variant}`\n"
                     f"- tokens: {usage['totals']['total']:,} (est. ${usage['est_cost_usd']})\n"
                     + (f"- PR: {result.get('pr_url')}\n" if result.get('pr_url') else '')
                     + (f"- stopped at `{result.get('stopped_stage')}`: {result.get('reason')}\n"
                        if result.get('status') == 'stopped' else ''))
    return code
