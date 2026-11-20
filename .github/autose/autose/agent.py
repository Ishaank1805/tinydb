"""The tool-using agent loop (ReAct style) with budgets, nudges and stuck detection."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .events import now_iso
from .llm import ToolResult
from .sandbox import truncate_middle
from .schema import validate
from .tools import PolicyError, Tool, ToolOutput
from .usage import BudgetExceeded

LONG_OUTPUT_TOOLS = {'read_file', 'get_diff', 'read_artifact', 'read_skill_file'}


@dataclass
class AgentResult:
    outcome: str          # accepted | <terminal tool outcome> | max_steps | no_tool_calls | refused | stuck
    payload: Any = None
    steps: int = 0
    detail: str = ''


class Agent:
    def __init__(self, *, role: str, conversation: Any, tools: list[Tool], max_steps: int, ledger: Any,
                 budget: Any, events: Any, transcript: Path, output_limit: int = 12000,
                 redact: Callable[[str], str] | None = None):
        self.role = role
        self.conv = conversation
        self.specs = {t.name: t for t in tools}
        self.handlers = {t.name: t.handler for t in tools if t.handler}
        self.max_steps = max_steps
        self.ledger = ledger
        self.budget = budget
        self.events = events
        self.transcript = Path(transcript)
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.output_limit = output_limit
        self.redact = redact or (lambda s: s)
        self.stage = ''
        self.finish_hint = 'your submit tool'

    def bind(self, handlers: dict, stage: str, finish_hint: str) -> None:
        """Shared-context mode: same conversation, new stage handlers."""
        self.handlers = dict(handlers)
        self.stage = stage
        self.finish_hint = finish_hint

    def _log(self, record: dict) -> None:
        record = {'ts': now_iso(), 'role': self.role, 'stage': self.stage, **record}
        text = self.redact(json.dumps(record, default=str))
        with open(self.transcript, 'a', encoding='utf-8') as fh:
            fh.write(text + '\n')

    def run(self, task: str, stage: str | None = None, finish_hint: str | None = None,
            max_steps: int | None = None) -> AgentResult:
        if stage:
            self.stage = stage
        if finish_hint:
            self.finish_hint = finish_hint
        steps_allowed = max_steps or self.max_steps
        self.conv.add_user(task)
        self._log({'type': 'task', 'text': task[:20000]})
        self.events.emit('agent_started', role=self.role, stage=self.stage, max_steps=steps_allowed)
        nudges = repeats = 0
        last_signature = None
        for step in range(1, steps_allowed + 1):
            self.budget.check()
            response = self.conv.send()
            self.ledger.add(self.role, self.stage, response.model, response.usage)
            self._log({'type': 'assistant', 'step': step, 'text': response.text[:20000],
                       'stop_reason': response.stop_reason, 'usage': response.usage.as_dict(),
                       'tool_calls': [{'name': c.name, 'arguments': c.arguments} for c in response.tool_calls]})
            if response.stop_reason == 'refusal':
                return self._end(AgentResult('refused', steps=step, detail=response.text[:500]))
            if not response.tool_calls:
                nudges += 1
                if nudges > 3:
                    return self._end(AgentResult('no_tool_calls', steps=step, detail=response.text[:500]))
                self.conv.add_user('Please continue by calling a tool. You work only through tools; '
                                   f'when you are finished, call {self.finish_hint}.')
                continue
            results: list[ToolResult] = []
            final: ToolOutput | None = None
            for call in response.tool_calls:
                if final is not None:
                    results.append(ToolResult(call.id, call.name, 'Not executed: the session already ended '
                                              'with the previous tool call.', True))
                    continue
                out = self._execute(call)
                results.append(ToolResult(call.id, call.name, out.content, out.is_error))
                self._log({'type': 'tool', 'step': step, 'name': call.name, 'is_error': out.is_error,
                           'output': out.content[:4000]})
                if out.done:
                    final = out
            self.conv.add_tool_results(results)
            if final is not None:
                return self._end(AgentResult(final.outcome or 'accepted', final.payload, step,
                                             final.content[:500]))
            signature = json.dumps([(c.name, c.arguments) for c in response.tool_calls],
                                   sort_keys=True, default=str)
            repeats = repeats + 1 if signature == last_signature else 0
            last_signature = signature
            if repeats >= 5:
                return self._end(AgentResult('stuck', steps=step,
                                             detail='the same tool call was repeated six times in a row'))
            notes = []
            if repeats == 2:
                notes.append('You made the same tool call three times in a row. Change your approach.')
            if steps_allowed - step == 3:
                notes.append(f'Only 3 tool-call rounds remain in this session: wrap up and call '
                             f'{self.finish_hint}.')
            if notes:
                self.conv.add_user(' '.join(notes))
        return self._end(AgentResult('max_steps', steps=steps_allowed,
                                     detail=f'stopped after {steps_allowed} tool-call rounds'))

    def _end(self, result: AgentResult) -> AgentResult:
        self.events.emit('agent_finished', role=self.role, stage=self.stage, outcome=result.outcome,
                         steps=result.steps)
        return result

    def _execute(self, call: Any) -> ToolOutput:
        if call.parse_error:
            return ToolOutput(f'Could not parse the tool arguments as JSON ({call.parse_error}).', True)
        spec = self.specs.get(call.name)
        if spec is None:
            return ToolOutput(f'Unknown tool {call.name!r}. Available: {sorted(self.handlers)}', True)
        handler = self.handlers.get(call.name)
        if handler is None:
            return ToolOutput(f'{call.name} is not available in the current stage ({self.stage}). '
                              f'Available now: {sorted(self.handlers)}', True)
        errors = validate(call.arguments, spec.schema)
        if errors:
            return ToolOutput('Invalid arguments:\n- ' + '\n- '.join(errors[:15]), True)
        try:
            out = handler(call.arguments)
        except PolicyError as exc:
            return ToolOutput(f'Refused by the harness policy: {exc}', True)
        except BudgetExceeded:
            raise
        except Exception as exc:  # a tool bug must not kill the session
            self.events.emit('tool_error', role=self.role, stage=self.stage, tool=call.name,
                             error=f'{type(exc).__name__}: {exc}')
            return ToolOutput(f'The tool failed: {type(exc).__name__}: {exc}', True)
        limit = 40000 if call.name in LONG_OUTPUT_TOOLS else self.output_limit
        out.content = truncate_middle(self.redact(out.content or '(no output)'), limit)
        return out
