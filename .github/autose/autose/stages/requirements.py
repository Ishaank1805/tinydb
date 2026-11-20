"""Stage 1 · Requirements: issue -> spec.md. Check: every acceptance criterion is testable."""
from __future__ import annotations

from typing import Any

from .. import checks, prompts, render
from ..tools import ToolOutput, command_handler, file_handlers, skill_handler
from . import StageOutcome, agent_failure, issue_block, top_level


def run(ctx: Any, feedback: str = '') -> StageOutcome:
    ctx.git.reset_to_head()                      # read-only stage: start and end clean
    skill_text, fallback, skill_dir = prompts.skill()
    if fallback:
        ctx.events.emit('warning', stage='requirements', message='A1 skill not installed; using the fallback')
    max_subs = int(ctx.cfg.get('pipeline.requirements_max_submissions', 3))
    judge_prompt = prompts.load('judge')
    seen: dict[str, Any] = {'n': 0, 'spec': None, 'check': None, 'judge': None}

    def submit_spec(args: dict) -> ToolOutput:
        seen['n'] += 1
        spec = dict(args)
        result = checks.check_spec(spec)
        verdicts = None
        if result.passed and ctx.cfg.get('judge.enabled', True):
            try:
                verdicts = checks.judge_testability(ctx.make_llm, ctx.ledger, spec, judge_prompt)
            except Exception as exc:      # the judge is advisory unless judge.required
                ctx.events.emit('judge_error', error=f'{type(exc).__name__}: {exc}'[:300])
            if verdicts is None:
                if ctx.cfg.get('judge.required'):
                    result.problems.append('the testability judge could not be consulted')
                else:
                    result.notes.append('LLM judge unavailable: structural check only')
            else:
                for cid in checks.ac_ids(spec):
                    ok, reason = verdicts.get(cid, (False, 'the judge did not classify it'))
                    if not ok:
                        result.problems.append(f'{cid}: the judge says it is not testable: {reason}')
            result.passed = not result.problems
        seen.update(spec=spec, check=result, judge=verdicts)
        ctx.events.emit('check', stage='requirements', passed=result.passed, submission=seen['n'],
                        problems=len(result.problems))
        if result.passed:
            return ToolOutput('Specification accepted.', done=True, outcome='accepted', payload=spec)
        if seen['n'] >= max_subs:
            return ToolOutput('The testability check failed again; the stage stops.\n' + result.render(),
                              True, True, 'check_failed')
        return ToolOutput('The testability check failed. Fix these problems and call submit_spec again:\n'
                          + result.render(), True)

    def cannot_specify(args: dict) -> ToolOutput:
        return ToolOutput('Recorded: the run stops and your questions are posted for the maintainer.',
                          done=True, outcome='cannot_specify', payload=args)

    handlers = {**file_handlers(ctx.workspace()), 'run_command': command_handler(ctx.sandbox),
                'read_skill_file': skill_handler(skill_dir), 'submit_spec': submit_spec,
                'cannot_specify': cannot_specify}
    task = (f'Write the specification for this GitHub issue.\n\n{issue_block(ctx)}\n\n'
            f'Repository top level: {top_level(ctx)}\n\n'
            + (f'## Feedback from a later stage that you must address\n{feedback}\n\n' if feedback else '')
            + 'When done call submit_spec (or cannot_specify).')
    result = ctx.run_agent('planner', 'requirements', prompts.system('requirements', skill=skill_text),
                           handlers, 'submit_spec', task)
    ctx.git.reset_to_head()
    spec = seen['spec']
    if result.outcome == 'accepted':
        ctx.state.write_data('spec', spec)
        ctx.state.write_artifact('spec.md', render.spec_md(ctx, spec, seen['check'], seen['judge'], fallback),
                                 tag=f'r{ctx.run.round}')
        return StageOutcome('passed', f"{len(spec['acceptance_criteria'])} testable acceptance criteria")
    if result.outcome == 'cannot_specify':
        reason, questions = result.payload['reason'], result.payload['questions']
        ctx.state.write_artifact('spec.md', render.spec_md(ctx, spec, seen['check'], seen['judge'], fallback,
                                                           reason, questions))
        return StageOutcome('failed', f'cannot write testable criteria: {reason}', {'questions': questions})
    reason = (f'acceptance criteria still not testable after {max_subs} submissions'
              if result.outcome == 'check_failed' else agent_failure(result, 'planner'))
    ctx.state.write_artifact('spec.md', render.spec_md(ctx, spec, seen['check'], seen['judge'], fallback, reason))
    return StageOutcome('failed', reason)
