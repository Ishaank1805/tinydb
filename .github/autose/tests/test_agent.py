import pytest

from autose.agent import Agent
from autose.config import Config
from autose.events import EventLog
from autose.llm import ScriptedClient
from autose.schema import obj, string
from autose.tools import Tool, ToolOutput
from autose.usage import Budget, BudgetExceeded, UsageLedger


def make_agent(tmp_path, responses, tools, cfg=None, max_steps=10):
    queue = list(responses)
    client = ScriptedClient(lambda **_: queue.pop(0) if queue else {'text': 'nothing left'})
    ledger = UsageLedger(tmp_path / 'usage.json')
    cfg = cfg or Config({'variants': {'baseline': {}}})
    return Agent(role='tester', conversation=client.conversation('sys', tools), tools=tools, max_steps=max_steps,
                 ledger=ledger, budget=Budget(cfg, ledger), events=EventLog(tmp_path / 'e.jsonl', 'test'),
                 transcript=tmp_path / 't.jsonl')


def submit_tool():
    return Tool('submit', 'Submit.', obj({'value': string(minLength=2)}, ['value']),
                lambda a: ToolOutput('accepted', done=True, outcome='accepted', payload=a['value']))


def test_nudges_unknown_tools_bad_arguments_then_finishes(tmp_path):
    agent = make_agent(tmp_path, [
        {'text': 'I will think first.'},
        {'tool_calls': [('does_not_exist', {})]},
        {'tool_calls': [('submit', {'value': 1})]},
        {'tool_calls': [('submit', {'value': 'ok'}), ('submit', {'value': 'twice'})]},
    ], [submit_tool()])
    result = agent.run('task', stage='s', finish_hint='submit')
    assert (result.outcome, result.payload, result.steps) == ('accepted', 'ok', 4)
    history = agent.conv.messages
    tool_msgs = [m for m in history if m['role'] == 'tool']
    assert 'Unknown tool' in tool_msgs[0]['results'][0]['content']
    assert 'Invalid arguments' in tool_msgs[1]['results'][0]['content']
    assert tool_msgs[2]['results'][1]['content'].startswith('Not executed')
    assert any(m['role'] == 'user' and 'continue by calling a tool' in m['content'] for m in history)


def test_repeating_the_same_call_ends_the_session_as_stuck(tmp_path):
    echo = Tool('echo', 'Echo.', obj({'text': string()}, ['text']), lambda a: ToolOutput(a['text']))
    agent = make_agent(tmp_path, [{'tool_calls': [('echo', {'text': 'x'})]}] * 10, [echo, submit_tool()])
    assert agent.run('task').outcome == 'stuck'


def test_the_token_budget_stops_the_agent(tmp_path):
    cfg = Config({'budgets': {'max_tokens_per_job': 1500}, 'variants': {'baseline': {}}})
    echo = Tool('echo', 'Echo.', obj({'text': string()}, ['text']), lambda a: ToolOutput(a['text']))
    agent = make_agent(tmp_path, [{'tool_calls': [('echo', {'text': str(i)})]} for i in range(5)],
                       [echo], cfg=cfg)
    with pytest.raises(BudgetExceeded):
        agent.run('task')
