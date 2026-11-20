"""Stage 4 · Testing: acceptance tests per criterion + the full suite -> test_report.md.

Check: all new and existing tests pass, every criterion has a test, every test
that existed on the base commit is still collected. The fail-to-pass probe
re-runs the new tests against the base source files to show they detect the change.
"""
from __future__ import annotations

from typing import Any

from .. import checks, prompts, render
from ..pytest_runner import base_sources, collect, run_tests
from ..tools import ToolOutput, artifact_handler, command_handler, file_handlers, matches
from . import StageOutcome, agent_failure, enforce, snapshot


def write_tests(ctx: Any, reason: str = '', dispute: dict | None = None) -> StageOutcome:
    spec = ctx.state.read_data('spec')
    design = ctx.state.read_data('design') or {}
    allowed = [ctx.test_file]
    ws = ctx.workspace(writable=allowed)
    snap = snapshot(ctx)
    seen = {'n': 0}

    def submit_tests(args: dict) -> ToolOutput:
        seen['n'] += 1
        result = checks.check_test_file(ctx.repo_dir / ctx.test_file, spec)
        ids, ok, out = collect(ctx.sandbox, ctx.cfg.req('target.collect'), [ctx.test_file])
        if not ok or not ids:
            result.passed = False
            result.problems.append('pytest cannot collect the file:\n' + out[-2000:])
        ctx.events.emit('check', stage='testing', what='test file', passed=result.passed, submission=seen['n'])
        if result.passed:
            return ToolOutput('Tests accepted.', done=True, outcome='accepted', payload=args)
        if seen['n'] >= 3:
            return ToolOutput('The test file is still not acceptable; stopping.\n' + result.render(),
                              True, True, 'invalid')
        return ToolOutput('The test file is not acceptable yet:\n' + result.render(), True)

    criteria = '\n'.join(f"- **{ac['id']}**: given {ac['given']}; when {ac['when']}; then {ac['then']}. "
                         f"Verify: {ac['verification']}" for ac in spec['acceptance_criteria'])
    planned = '\n'.join(f"- `{t['name']}` covers {', '.join(t['covers'])}: {t['description']}"
                        for t in design.get('tests') or [])
    handlers = {**file_handlers(ws, write=True), 'run_command': command_handler(ctx.sandbox),
                'read_artifact': artifact_handler(ctx.artifact_paths()), 'submit_tests': submit_tests}
    task = '\n\n'.join(filter(None, [
        f'Write the acceptance tests for issue #{ctx.run.issue} in `{ctx.test_file}` (the only file you may '
        'write).',
        '## Acceptance criteria\n' + criteria,
        '## Tests planned in design.md\n' + (planned or '_none_'),
        f'## Why you are asked again\n{reason}' if reason else '',
        (f"## Disputed test\nThe implementer disputes `{dispute.get('test_name')}`: {dispute.get('reason')}\n"
         'Re-read the spec. Change the test only if it really contradicts the spec; otherwise keep it.')
        if dispute else '',
        'Run your tests (they may fail if the implementation is wrong, but they must be collected), then call '
        'submit_tests.',
    ]))
    result = ctx.run_agent('test_author', 'testing', prompts.system('test_author', test_file=ctx.test_file),
                           handlers, 'submit_tests', task)
    enforce(ctx, snap, allowed)
    if result.outcome == 'accepted':
        return StageOutcome('passed', 'acceptance tests written')
    if result.outcome == 'invalid':
        return StageOutcome('tests_invalid', 'the test author could not produce a valid test file')
    return StageOutcome('tests_invalid', agent_failure(result, 'test author'))


def evaluate(ctx: Any) -> dict:
    spec = ctx.state.read_data('spec')
    test_cmd = ctx.cfg.req('target.test')
    suite = run_tests(ctx.sandbox, test_cmd, ctx.scratch / 'junit-full.xml')
    collected, collect_ok, _ = collect(ctx.sandbox, ctx.cfg.req('target.collect'))
    baseline = set(ctx.state.data.get('baseline_tests') or [])
    missing = sorted(baseline - collected) if collect_ok else ['(collection of the suite failed)']
    names = checks.test_functions(ctx.repo_dir / ctx.test_file) or []
    by_ac = checks.tests_by_ac(names)
    module = ctx.test_file[:-3].replace('/', '.')
    new_cases = [c for c in suite.cases if c.classname == module or c.classname.startswith(module + '.')]
    ac_rows = []
    for cid in checks.ac_ids(spec):
        tests = []
        for name in by_ac.get(cid, []):
            outcomes = [c.outcome for c in new_cases if c.name.split('[')[0] == name]
            tests.append({'name': name, 'outcomes': outcomes or ['not run']})
        ac_rows.append({'ac': cid, 'tests': tests})
    mode = ctx.cfg.get('pipeline.fail_to_pass', 'soft')
    f2p: dict[str, Any] = {'mode': mode, 'failing_on_base': None, 'cases': {}}
    if mode != 'off' and new_cases:
        changed = [p for p in ctx.git.changed_files(ctx.base_sha) if matches(p, ctx.source_globs)]
        with base_sources(ctx.git, ctx.base_sha, changed, ctx.repo_dir):
            base_run = run_tests(ctx.sandbox, test_cmd, ctx.scratch / 'junit-base.xml', [ctx.test_file])
        f2p['cases'] = {c.name: c.outcome for c in base_run.cases}
        if not base_run.cases and base_run.exit_code != 0:
            f2p['collection_error_on_base'] = True
            f2p['failing_on_base'] = len(new_cases)
        else:
            f2p['failing_on_base'] = sum(1 for o in f2p['cases'].values() if o in ('failed', 'error'))
    problems, notes = [], []
    if not suite.ok:
        problems.append(f'test suite: {suite.summary()}' + (' (timed out)' if suite.timed_out else ''))
    if suite.exit_code != 0 and not suite.cases:
        problems.append('pytest produced no results (collection error?)')
    if missing:
        problems.append(f'{len(missing)} existing test(s) are no longer collected')
    uncovered = [r['ac'] for r in ac_rows if not r['tests']]
    if uncovered:
        problems.append('no acceptance test for ' + ', '.join(uncovered))
    not_run = [t['name'] for r in ac_rows for t in r['tests'] if t['outcomes'] == ['not run']]
    if not_run:
        problems.append('acceptance tests that did not run: ' + ', '.join(not_run))
    if f2p['failing_on_base'] == 0:
        (problems if mode == 'hard' else notes).append('no new test fails on the base code, so the tests do '
                                                       'not show that the change is needed')
    return {'passed': not problems, 'problems': problems, 'notes': notes, 'summary': suite.summary(),
            'exit_code': suite.exit_code, 'ac_rows': ac_rows, 'missing_baseline': missing,
            'fail_to_pass': f2p, 'output_tail': suite.output[-6000:],
            'failing': [{'key': c.key, 'message': c.message} for c in suite.failing()][:20]}


def failure_feedback(ev: dict) -> str:
    lines = ['The testing stage failed:'] + [f'- {p}' for p in ev['problems']]
    for f in ev.get('failing', [])[:10]:
        lines.append(f"\n### {f['key']}\n```\n{f['message'][:1500]}\n```")
    if ev.get('missing_baseline'):
        lines.append('\nTests that disappeared: ' + ', '.join(ev['missing_baseline'][:20]))
    lines.append('\nEnd of the pytest output:\n```\n' + ev.get('output_tail', '')[-2500:] + '\n```')
    return '\n'.join(lines)


def run(ctx: Any, need_tests: bool, attempt: int, reason: str = '', dispute: dict | None = None) -> StageOutcome:
    if need_tests:
        written = write_tests(ctx, reason, dispute)
        if not written.passed:
            ev = {'passed': False, 'problems': [written.message], 'summary': 'not run'}
            ctx.state.write_artifact('test_report.md', render.test_report_md(ctx, ev, attempt, written.message))
            return StageOutcome('tests_invalid', written.message)
    ev = evaluate(ctx)
    if (ev['fail_to_pass']['failing_on_base'] == 0 and ctx.cfg.get('pipeline.fail_to_pass', 'soft') != 'off'
            and not ctx.state.data.get('f2p_retry_done')):
        ctx.state.data['f2p_retry_done'] = True
        ctx.state.save()
        retry = write_tests(ctx, reason='None of your tests fails when the source files are reset to the base '
                                        'commit, so they do not demonstrate the change. Strengthen the tests '
                                        'for the changed behaviour so that they fail without the implementation.')
        if retry.passed:
            ev = evaluate(ctx)
    ctx.state.write_data('test_results', ev)
    ctx.state.write_artifact('test_report.md', render.test_report_md(ctx, ev, attempt),
                             tag=f'r{ctx.run.round}-a{attempt}')
    if ev['passed']:
        return StageOutcome('passed', ev['summary'], {'evaluation': ev})
    return StageOutcome('failed', '; '.join(ev['problems'])[:400], {'feedback': failure_feedback(ev)})
