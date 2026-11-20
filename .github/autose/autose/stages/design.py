"""Stage 2 · Design: spec.md + ARCHITECTURE.md -> design.md. Check: names real files and functions."""
from __future__ import annotations

from typing import Any

from .. import checks, prompts, render
from ..sandbox import truncate_middle
from ..tools import ToolOutput, artifact_handler, command_handler, file_handlers
from . import StageOutcome, agent_failure, issue_block


def run(ctx: Any, feedback: str = '') -> StageOutcome:
    ctx.git.reset_to_head()
    spec = ctx.state.read_data('spec')
    arch_rel = ctx.cfg.get('target.architecture_doc') or 'ARCHITECTURE.md'
    arch_path = ctx.repo_dir / arch_rel
    arch_text = arch_path.read_text(encoding='utf-8', errors='replace') if arch_path.is_file() else ''
    if not arch_text:
        ctx.events.emit('warning', stage='design', message=f'{arch_rel} not found in the repository')
    editable = ctx.source_globs + ctx.doc_globs
    test_dir = ctx.cfg.get('target.test_dir') or 'tests'
    max_subs = int(ctx.cfg.get('pipeline.design_max_submissions', 3))
    seen: dict[str, Any] = {'n': 0, 'design': None, 'check': None}

    def submit_design(args: dict) -> ToolOutput:
        seen['n'] += 1
        design = dict(args)
        result = checks.check_design(design, spec, ctx.repo_dir, editable, ctx.protected_globs,
                                     ctx.test_file, test_dir)
        seen.update(design=design, check=result)
        ctx.events.emit('check', stage='design', passed=result.passed, submission=seen['n'],
                        problems=len(result.problems))
        if result.passed:
            return ToolOutput('Design accepted.', done=True, outcome='accepted', payload=design)
        if seen['n'] >= max_subs:
            return ToolOutput('The plan check failed again; the stage stops.\n' + result.render(),
                              True, True, 'check_failed')
        return ToolOutput('The plan check failed. Fix these problems and call submit_design again:\n'
                          + result.render(), True)

    def cannot_design(args: dict) -> ToolOutput:
        return ToolOutput('Recorded: the run stops.', done=True, outcome='cannot_design', payload=args)

    previous = ctx.state.read_artifact('design.md')
    handlers = {**file_handlers(ctx.workspace()), 'run_command': command_handler(ctx.sandbox),
                'read_artifact': artifact_handler(ctx.artifact_paths()),
                'submit_design': submit_design, 'cannot_design': cannot_design}
    task = '\n\n'.join(filter(None, [
        f'Plan the change for issue #{ctx.run.issue}.',
        issue_block(ctx),
        '## spec.md\n' + (ctx.state.read_artifact('spec.md') or ''),
        f'## {arch_rel} (architecture documentation from A2)\n'
        + (truncate_middle(arch_text, 40000) if arch_text else '_not available in this repository_'),
        '## Constraints the harness enforces\n'
        f'- files you may plan to change: {", ".join(editable)}\n'
        f'- acceptance tests go in `{ctx.test_file}`, named test_ac<N>_<what>, one or more per criterion\n'
        f'- never changed: {", ".join(ctx.protected_globs)}',
        f'## Feedback you must address\n{feedback}' if feedback else '',
        f'## Previous design (revise it)\n{truncate_middle(previous, 12000)}' if previous and feedback else '',
        'When done call submit_design (or cannot_design).',
    ]))
    result = ctx.run_agent('planner', 'design', prompts.system('design', test_file=ctx.test_file), handlers,
                           'submit_design', task)
    ctx.git.reset_to_head()
    design = seen['design']
    if result.outcome == 'accepted':
        ctx.state.write_data('design', design)
        ctx.state.write_artifact('design.md', render.design_md(ctx, design, seen['check'], bool(arch_text)),
                                 tag=f'r{ctx.run.round}')
        files = ', '.join(c['path'] for c in design['changes'])
        return StageOutcome('passed', f'plan touches {files}')
    if result.outcome == 'cannot_design':
        reason = result.payload['reason']
    elif result.outcome == 'check_failed':
        reason = f'the plan still failed its check after {max_subs} submissions'
    else:
        reason = agent_failure(result, 'planner')
    ctx.state.write_artifact('design.md', render.design_md(ctx, design, seen['check'], bool(arch_text), reason))
    return StageOutcome('failed', reason)
