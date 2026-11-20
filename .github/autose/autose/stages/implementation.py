"""Stage 3 · Implementation: change the code on the branch -> diff.patch. Check: the code builds."""
from __future__ import annotations

from typing import Any

from .. import checks, prompts
from ..pytest_runner import collect
from ..tools import ToolOutput, artifact_handler, command_handler, file_handlers, matches
from . import StageOutcome, agent_failure, enforce, snapshot


def run_build(ctx: Any) -> checks.CheckResult:
    build = ctx.sandbox.run(ctx.cfg.req('target.build'), timeout=600, max_output=8000)
    _, collect_ok, collect_out = collect(ctx.sandbox, ctx.cfg.req('target.collect'),
                                         extra=f'--ignore={ctx.test_file}')
    problems = []
    if build.exit_code != 0:
        problems.append(f'the build command failed (exit {build.exit_code})')
    if not collect_ok:
        problems.append('pytest cannot collect the existing test suite (import or syntax error?)')
    output = build.render() + ('\n\n' + collect_out[-3000:] if not collect_ok else '')
    return checks.CheckResult(not problems, problems, output=output)


def source_diff(ctx: Any) -> str:
    editable = ctx.source_globs + ctx.doc_globs
    paths = [p for p in ctx.git.changed_files(ctx.base_sha) if matches(p, editable)]
    return ctx.git.diff(ctx.base_sha, paths) if paths else ''


def run(ctx: Any, attempt: int, feedback: str = '', tests_exist: bool = False) -> StageOutcome:
    allowed = ctx.source_globs + ctx.doc_globs
    ws = ctx.workspace(writable=allowed)
    snap = snapshot(ctx)
    finishes = {'n': 0}

    def finish_implementation(args: dict) -> ToolOutput:
        finishes['n'] += 1
        reverted = enforce(ctx, snap, allowed)
        build = run_build(ctx)
        ctx.events.emit('check', stage='implementation', passed=build.passed, attempt=attempt)
        note = (f'\nNote: changes outside the files you may change were reverted: {", ".join(reverted)}.'
                if reverted else '')
        if build.passed:
            return ToolOutput('Build check passed.' + note, done=True, outcome='accepted',
                              payload={'summary': args['summary'], 'reverted': reverted})
        if finishes['n'] >= 3:
            return ToolOutput('The build check failed again; the stage stops.\n' + build.output[-4000:],
                              True, True, 'build_failed')
        return ToolOutput('The build check failed:' + note + '\n' + build.output[-6000:]
                          + '\nFix it and call finish_implementation again.', True)

    def request_redesign(args: dict) -> ToolOutput:
        return ToolOutput('Sending the work back to the design stage.', done=True, outcome='redesign',
                          payload=args)

    def dispute_test(args: dict) -> ToolOutput:
        return ToolOutput('The test author will re-check that test.', done=True, outcome='dispute',
                          payload=args)

    handlers = {**file_handlers(ws, write=True), 'run_command': command_handler(ctx.sandbox),
                'read_artifact': artifact_handler(ctx.artifact_paths()),
                'finish_implementation': finish_implementation, 'request_redesign': request_redesign}
    if tests_exist:
        handlers['dispute_test'] = dispute_test
    tests_note = (f'The acceptance tests in `{ctx.test_file}` were written by a separate test author; you '
                  'may read and run them but not change them (use dispute_test if one contradicts the spec).'
                  if tests_exist else
                  f'A separate test author will write acceptance tests in `{ctx.test_file}` after you finish.')
    task = '\n\n'.join(filter(None, [
        f'Implement the design for issue #{ctx.run.issue} (implementation attempt {attempt}).',
        '## spec.md\n' + (ctx.state.read_artifact('spec.md') or ''),
        '## design.md\n' + (ctx.state.read_artifact('design.md') or ''),
        f'## Feedback you must address\n{feedback}' if feedback else '',
        f'Files you may change: {", ".join(allowed)}. {tests_note}',
        'Run the relevant existing tests yourself, then call finish_implementation.',
    ]))
    result = ctx.run_agent('implementer', 'implementation', prompts.system('implementer'), handlers,
                           'finish_implementation', task)
    enforce(ctx, snap, allowed)
    ctx.state.write_artifact('diff.patch', source_diff(ctx) or '# (no source changes yet)\n',
                             tag=f'r{ctx.run.round}-a{attempt}')
    if result.outcome == 'accepted':
        return StageOutcome('passed', result.payload['summary'][:300], {'summary': result.payload['summary']})
    if result.outcome == 'redesign':
        return StageOutcome('redesign', result.payload['reason'])
    if result.outcome == 'dispute':
        return StageOutcome('dispute', result.payload['reason'], dict(result.payload))
    if result.outcome == 'build_failed':
        return StageOutcome('failed', 'the code does not build after three tries')
    return StageOutcome('failed', agent_failure(result, 'implementer'))
