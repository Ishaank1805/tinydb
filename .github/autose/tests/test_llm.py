import json

import pytest

from autose.llm import AnthropicClient, LLMError, OpenAIClient, ToolResult
from autose.schema import obj, string
from autose.tools import Tool

ECHO = Tool('echo', 'Echo text.', obj({'text': string()}, ['text']))


class Resp:
    def __init__(self, status, data=None, headers=None):
        self.status_code = status
        self._data = data or {}
        self.text = json.dumps(self._data)
        self.headers = headers or {}

    def json(self):
        return self._data


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.payloads.append(json)
        return self.responses.pop(0)


def anthropic(session, **options):
    return AnthropicClient('claude-test', 'key', 'https://api.anthropic.com', 1000, options or {'effort': 'medium'},
                           session=session, sleep=lambda s: None)


def test_anthropic_payload_caching_and_thinking_blocks_round_trip():
    thinking = {'type': 'thinking', 'thinking': '', 'signature': 'sig-1'}
    session = Session([
        Resp(200, {'content': [thinking, {'type': 'tool_use', 'id': 'tu1', 'name': 'echo', 'input': {'text': 'ok'}}],
                   'stop_reason': 'tool_use', 'model': 'claude-test',
                   'usage': {'input_tokens': 10, 'output_tokens': 5, 'cache_read_input_tokens': 3,
                             'cache_creation_input_tokens': 7}}),
        Resp(200, {'content': [{'type': 'text', 'text': 'done'}], 'stop_reason': 'end_turn',
                   'usage': {'input_tokens': 1, 'output_tokens': 1}}),
    ])
    conv = anthropic(session).conversation('system text', [ECHO])
    conv.add_user('hi')
    first = conv.send()
    assert first.tool_calls[0].arguments == {'text': 'ok'}
    assert (first.usage.input, first.usage.cache_read, first.usage.cache_write) == (10, 3, 7)
    conv.add_tool_results([ToolResult('tu1', 'echo', 'ok')])
    conv.add_user('next')
    assert conv.send().text == 'done'
    p1, p2 = session.payloads
    for p in (p1, p2):
        assert 'temperature' not in p and 'tool_choice' not in p and 'top_p' not in p
    assert p1['output_config'] == {'effort': 'medium'}
    assert p1['system'][0]['cache_control'] == {'type': 'ephemeral'}
    assert p1['tools'][0]['input_schema']['required'] == ['text']
    assert p2['messages'][1]['content'][0] == thinking            # passed back unchanged
    user = p2['messages'][2]['content']
    assert user[0]['type'] == 'tool_result' and user[0]['tool_use_id'] == 'tu1' and user[1]['type'] == 'text'
    markers = sum('cache_control' in b for m in p2['messages'] for b in m['content'])
    assert markers == 1                                          # no stale breakpoints accumulate
    assert 'cache_control' not in conv.messages[-1]['content'][-1] if conv.messages[-1]['role'] == 'user' else True


def test_anthropic_retries_overload_and_fails_fast_on_bad_requests():
    ok = Resp(200, {'content': [{'type': 'text', 'text': 'x'}], 'stop_reason': 'end_turn', 'usage': {}})
    session = Session([Resp(529, {'error': 'overloaded'}), ok])
    conv = anthropic(session).conversation('s', [])
    conv.add_user('hi')
    assert conv.send().text == 'x'
    bad = Session([Resp(400, {'error': {'message': 'temperature is not supported'}})])
    conv = anthropic(bad).conversation('s', [])
    conv.add_user('hi')
    with pytest.raises(LLMError) as err:
        conv.send()
    assert err.value.status == 400 and len(bad.payloads) == 1


def test_openai_compatible_keeps_provider_extras_and_formats_tool_messages():
    message = {'role': 'assistant', 'content': None, 'refusal': None, 'annotations': [],
               'tool_calls': [{'id': 'c1', 'type': 'function',
                               'function': {'name': 'echo', 'arguments': '{"text": "ok"}'},
                               'extra_content': {'google': {'thought_signature': 'abc'}}}]}
    session = Session([
        Resp(200, {'choices': [{'message': message, 'finish_reason': 'tool_calls'}],
                   'usage': {'prompt_tokens': 100, 'completion_tokens': 5, 'prompt_tokens_details': {'cached_tokens': 40}}}),
        Resp(200, {'choices': [{'message': {'role': 'assistant', 'content': 'done'}, 'finish_reason': 'stop'}],
                   'usage': {}}),
    ])
    client = OpenAIClient('gpt-test', 'key', 'https://api.example.com/v1', 500,
                          {'max_tokens_param': 'max_completion_tokens'}, session=session, sleep=lambda s: None)
    conv = client.conversation('sys', [ECHO])
    conv.add_user('hi')
    first = conv.send()
    assert first.tool_calls[0].arguments == {'text': 'ok'}
    assert (first.usage.input, first.usage.cache_read) == (60, 40)
    conv.add_tool_results([ToolResult('c1', 'echo', 'ok')])
    conv.send()
    p2 = session.payloads[1]
    assert p2['max_completion_tokens'] == 500 and p2['messages'][0]['role'] == 'system'
    assistant = p2['messages'][2]
    assert assistant['tool_calls'][0]['extra_content'] == {'google': {'thought_signature': 'abc'}}
    assert 'refusal' not in assistant and 'annotations' not in assistant
    assert p2['messages'][3] == {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'}


def test_openai_bad_tool_arguments_are_reported_not_raised():
    message = {'role': 'assistant', 'tool_calls': [{'id': 'c1', 'function': {'name': 'echo', 'arguments': '{bad'}}]}
    session = Session([Resp(200, {'choices': [{'message': message, 'finish_reason': 'tool_calls'}]})])
    conv = OpenAIClient('m', 'k', 'https://x/v1', 100, {}, session=session).conversation('s', [ECHO])
    conv.add_user('hi')
    call = conv.send().tool_calls[0]
    assert call.parse_error and call.arguments == {}


def test_gemini_dialect_strips_schema_keywords_but_not_property_names_and_names_tool_results():
    message = {'role': 'assistant', 'content': None,
               'tool_calls': [{'id': 'c1', 'type': 'function',
                               'function': {'name': 'search_code', 'arguments': '{"pattern": "def"}'}}]}
    session = Session([Resp(200, {'choices': [{'message': message, 'finish_reason': 'tool_calls'}], 'usage': {}}),
                       Resp(200, {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]})])
    options = {'tool_message_name': True, 'strip_schema_keys': ['additionalProperties', 'minLength', 'pattern']}
    client = OpenAIClient('gemini-test', 'k', 'https://gemini.test/v1beta/openai', 100, options, session=session,
                          sleep=lambda s: None)
    tool = Tool('search_code', 'Search.', obj({'pattern': string(minLength=1), 'path': string()}, ['pattern']))
    conv = client.conversation('sys', [tool])
    conv.add_user('hi')
    conv.send()
    conv.add_tool_results([ToolResult('c1', 'search_code', 'found')])
    conv.send()
    params = session.payloads[0]['tools'][0]['function']['parameters']
    assert 'additionalProperties' not in params and set(params['properties']) == {'pattern', 'path'}
    assert 'minLength' not in params['properties']['pattern'] and params['required'] == ['pattern']
    assert session.payloads[1]['messages'][-1] == {'role': 'tool', 'tool_call_id': 'c1', 'content': 'found',
                                                   'name': 'search_code'}


def test_daily_quota_is_reported_at_once_and_not_retried():
    body = {'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED',
                      'details': [{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}}
    session = Session([Resp(429, body)])
    conv = OpenAIClient('m', 'k', 'https://quota.test/v1', 100, {}, session=session,
                        sleep=lambda s: None).conversation('s', [])
    conv.add_user('hi')
    with pytest.raises(LLMError) as err:
        conv.send()
    assert 'daily request quota' in str(err.value) and len(session.payloads) == 1


def test_requests_to_one_model_are_spaced_out():
    waits = []
    ok = Resp(200, {'choices': [{'message': {'role': 'assistant', 'content': 'x'}, 'finish_reason': 'stop'}]})
    client = OpenAIClient('m', 'k', 'https://pace.test/v1', 100, {'min_interval_seconds': 5},
                          session=Session([ok, ok]), sleep=waits.append)
    conv = client.conversation('s', [])
    conv.add_user('a')
    conv.send()
    conv.add_user('b')
    conv.send()
    assert waits and 4 < waits[0] <= 5
