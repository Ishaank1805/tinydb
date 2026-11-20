"""Model clients: the Anthropic Messages API and OpenAI-compatible APIs
(OpenAI, Gemini's OpenAI endpoint, OpenRouter, ...).

Conversations are kept in the provider's native format and are append-only:
assistant messages are stored exactly as returned (including thinking
blocks and provider extras such as Gemini thought signatures), which is what
both APIs require for multi-turn tool use.
"""
from __future__ import annotations

import copy
import json
import random
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import requests

from .usage import Usage

RETRYABLE = {408, 409, 429, 500, 502, 503, 504, 529}
_LAST_CALL: dict = {}                       # (base_url, model) -> time of the last request
_DAILY_QUOTA = re.compile(r'per ?day|PerDay', re.I)


def clean_schema(schema, drop: set):
    """Remove JSON-schema keywords a provider rejects. Property *names* are never touched, and the
    harness still validates every tool call against the full schema."""
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key in drop or (key == 'required' and not value):
            continue
        if key == 'properties' and isinstance(value, dict):
            out[key] = {name: clean_schema(sub, drop) for name, sub in value.items()}
        elif key == 'items':
            out[key] = clean_schema(value, drop)
        else:
            out[key] = value
    return out


class LLMError(Exception):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    parse_error: str | None = None


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    usage: Usage
    model: str


class Conversation:
    """One agent session: system prompt, fixed tool list, growing history."""

    def __init__(self, client: Any, system: str, tools: list):
        self.client = client
        self.system = system
        self.tools = list(tools)
        self.messages: list[dict] = []

    def add_user(self, text: str) -> None:
        self.client.append_user_text(self.messages, text)

    def add_tool_results(self, results: list[ToolResult]) -> None:
        self.client.append_tool_results(self.messages, results)

    def send(self) -> LLMResponse:
        message, response = self.client.complete(self.system, self.messages, self.tools)
        self.messages.append(message)
        return response


class HTTPClient:
    provider = 'http'

    def __init__(self, model: str, api_key: str, base_url: str, max_tokens: int = 16000,
                 options: dict | None = None, timeout: int = 600, session: Any = None,
                 max_attempts: int = 6, sleep: Callable[[float], None] = time.sleep):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.max_tokens = int(max_tokens)
        self.options = options or {}
        self.timeout = timeout
        self.session = session or requests.Session()
        self.max_attempts = max_attempts
        self.sleep = sleep

    def conversation(self, system: str, tools: list) -> Conversation:
        return Conversation(self, system, tools)

    def _throttle(self) -> None:
        """Keep at least min_interval_seconds between requests to one model (free-tier RPM limits)."""
        gap = float(self.options.get('min_interval_seconds') or 0)
        if gap <= 0:
            return
        key = (self.base_url, self.model)
        wait = _LAST_CALL.get(key, 0.0) + gap - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        _LAST_CALL[key] = time.monotonic()

    def _post(self, url: str, headers: dict, payload: dict) -> dict:
        last: LLMError | None = None
        for attempt in range(1, self.max_attempts + 1):
            wait = None
            self._throttle()
            try:
                resp = self.session.post(url, headers=headers, json=payload, timeout=(15, self.timeout))
            except requests.RequestException as exc:
                last = LLMError(f'network error talking to {self.provider}: {exc}', retryable=True)
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        last = LLMError(f'{self.provider} returned invalid JSON', 200, True)
                else:
                    if resp.status_code == 429 and _DAILY_QUOTA.search(resp.text or ''):
                        raise LLMError(f'{self.provider}: the daily request quota of the model provider is exhausted '
                                       f'(it resets at midnight Pacific time): {resp.text[:300]}', 429, False)
                    last = LLMError(f'{self.provider} API error {resp.status_code}: {resp.text[:800]}',
                                    resp.status_code, resp.status_code in RETRYABLE)
                    if not last.retryable:
                        raise last
                    wait = resp.headers.get('retry-after')
            if attempt == self.max_attempts:
                break
            try:
                delay = float(wait) if wait else min(60.0, 2 ** attempt + random.random())
            except ValueError:
                delay = min(60.0, 2 ** attempt + random.random())
            self.sleep(min(delay, 90.0))
        assert last is not None
        raise last


class AnthropicClient(HTTPClient):
    provider = 'anthropic'
    API_VERSION = '2023-06-01'

    @staticmethod
    def tool_specs(tools: list) -> list[dict]:
        return [{'name': t.name, 'description': t.description, 'input_schema': t.schema} for t in tools]

    def append_user_text(self, messages: list[dict], text: str) -> None:
        block = {'type': 'text', 'text': text}
        if messages and messages[-1]['role'] == 'user':
            messages[-1]['content'].append(block)
        else:
            messages.append({'role': 'user', 'content': [block]})

    def append_tool_results(self, messages: list[dict], results: list[ToolResult]) -> None:
        blocks = []
        for r in results:
            block = {'type': 'tool_result', 'tool_use_id': r.call_id, 'content': r.content or '(no output)'}
            if r.is_error:
                block['is_error'] = True
            blocks.append(block)
        if messages and messages[-1]['role'] == 'user':
            messages[-1]['content'].extend(blocks)
        else:
            messages.append({'role': 'user', 'content': blocks})

    def build_payload(self, system: str, messages: list[dict], tools: list, max_tokens: int) -> dict:
        msgs = copy.deepcopy(messages)
        # One moving cache breakpoint at the end of the history (plus one on the
        # system prompt): each turn re-reads the previous turn's prefix from cache.
        if msgs and msgs[-1].get('content'):
            msgs[-1]['content'][-1]['cache_control'] = {'type': 'ephemeral'}
        payload: dict[str, Any] = {
            'model': self.model,
            'max_tokens': max_tokens,
            'system': [{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}],
            'messages': msgs,
        }
        if tools:
            payload['tools'] = self.tool_specs(tools)
        # No temperature/top_p/top_k and no forced tool_choice: current Claude
        # models reject them. Effort/thinking only when configured.
        if self.options.get('effort'):
            payload['output_config'] = {'effort': self.options['effort']}
        if self.options.get('thinking'):
            payload['thinking'] = self.options['thinking']
        return payload

    def complete(self, system: str, messages: list[dict], tools: list):
        headers = {'x-api-key': self.api_key, 'anthropic-version': self.API_VERSION,
                   'content-type': 'application/json'}
        max_tokens = self.max_tokens
        usage = Usage()
        data: dict = {}
        for _ in range(3):
            payload = self.build_payload(system, messages, tools, max_tokens)
            data = self._post(f'{self.base_url}/v1/messages', headers, payload)
            u = data.get('usage') or {}
            usage = usage + Usage(int(u.get('input_tokens') or 0), int(u.get('output_tokens') or 0),
                                  int(u.get('cache_read_input_tokens') or 0),
                                  int(u.get('cache_creation_input_tokens') or 0))
            if data.get('stop_reason') == 'max_tokens' and max_tokens < 64000:
                max_tokens = min(max_tokens * 2, 64000)   # truncated turn: retry with more room
                continue
            break
        content = data.get('content') or []
        text = '\n'.join(b.get('text', '') for b in content if b.get('type') == 'text').strip()
        calls = [ToolCall(b['id'], b['name'], b.get('input') or {})
                 for b in content if b.get('type') == 'tool_use']
        if not content:
            content = [{'type': 'text', 'text': '(no content)'}]
        message = {'role': 'assistant', 'content': content}   # verbatim, incl. thinking blocks
        return message, LLMResponse(text, calls, data.get('stop_reason') or '', usage,
                                    data.get('model') or self.model)


class OpenAIClient(HTTPClient):
    provider = 'openai-compatible'

    def tool_specs(self, tools: list) -> list[dict]:
        drop = set(self.options.get('strip_schema_keys') or [])
        return [{'type': 'function', 'function': {'name': t.name, 'description': t.description,
                                                  'parameters': clean_schema(t.schema, drop) if drop else t.schema}}
                for t in tools]

    def append_user_text(self, messages: list[dict], text: str) -> None:
        messages.append({'role': 'user', 'content': text})

    def append_tool_results(self, messages: list[dict], results: list[ToolResult]) -> None:
        for r in results:
            msg = {'role': 'tool', 'tool_call_id': r.call_id, 'content': r.content or '(no output)'}
            if self.options.get('tool_message_name'):
                msg['name'] = r.name
            messages.append(msg)

    def build_payload(self, system: str, messages: list[dict], tools: list, max_tokens: int) -> dict:
        payload: dict[str, Any] = {'model': self.model,
                                   'messages': [{'role': 'system', 'content': system}, *messages]}
        if tools:
            payload['tools'] = self.tool_specs(tools)
        payload[self.options.get('max_tokens_param') or 'max_tokens'] = max_tokens
        if self.options.get('reasoning_effort'):
            payload['reasoning_effort'] = self.options['reasoning_effort']
        return payload

    def complete(self, system: str, messages: list[dict], tools: list):
        headers = {'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'}
        max_tokens = self.max_tokens
        usage = Usage()
        data: dict = {}
        choice: dict = {}
        for _ in range(3):
            data = self._post(f'{self.base_url}/chat/completions', headers,
                              self.build_payload(system, messages, tools, max_tokens))
            u = data.get('usage') or {}
            cached = int(((u.get('prompt_tokens_details') or {}).get('cached_tokens')) or 0)
            usage = usage + Usage(max(0, int(u.get('prompt_tokens') or 0) - cached),
                                  int(u.get('completion_tokens') or 0), cached, 0)
            choice = (data.get('choices') or [{}])[0]
            if choice.get('finish_reason') == 'length' and max_tokens < 64000:
                max_tokens = min(max_tokens * 2, 64000)
                continue
            break
        msg = dict(choice.get('message') or {})
        calls = []
        for tc in msg.get('tool_calls') or []:
            if not tc.get('id'):
                tc['id'] = f'call_{uuid.uuid4().hex[:12]}'    # some providers omit ids
            fn = tc.get('function') or {}
            raw = fn.get('arguments') or '{}'
            args: Any = {}
            err = None
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(args, dict):
                    err, args = 'arguments must be a JSON object', {}
            except json.JSONDecodeError as exc:
                err = str(exc)
            calls.append(ToolCall(tc['id'], fn.get('name', ''), args, err))
        content = msg.get('content')
        if isinstance(content, list):
            content = '\n'.join(p.get('text', '') for p in content if isinstance(p, dict))
        drop = set(self.options.get('drop_message_keys') or ['annotations'])
        message = {k: v for k, v in msg.items() if v is not None and k not in drop}
        message['role'] = 'assistant'
        if not message.get('tool_calls'):
            message.pop('tool_calls', None)
            message['content'] = content or ''
        stop = choice.get('finish_reason') or ''
        if stop == 'content_filter':
            stop = 'refusal'
        return message, LLMResponse((content or '').strip(), calls, stop, usage,
                                    data.get('model') or self.model)


class ScriptedClient:
    """Deterministic stand-in for a model (harness tests and `--llm fake` dry runs).

    ``responder(system=..., messages=..., tools=[names])`` returns a dict with
    ``text`` and ``tool_calls`` = ``[(name, arguments), ...]``.
    """

    provider = 'scripted'

    def __init__(self, responder: Callable[..., dict], model: str = 'scripted'):
        self.responder = responder
        self.model = model

    def conversation(self, system: str, tools: list) -> Conversation:
        return Conversation(self, system, tools)

    def append_user_text(self, messages: list[dict], text: str) -> None:
        messages.append({'role': 'user', 'content': text})

    def append_tool_results(self, messages: list[dict], results: list[ToolResult]) -> None:
        messages.append({'role': 'tool', 'results': [r.__dict__ for r in results]})

    def complete(self, system: str, messages: list[dict], tools: list):
        out = self.responder(system=system, messages=messages, tools=[t.name for t in tools]) or {}
        calls = [ToolCall(f'call_{uuid.uuid4().hex[:8]}', name, args)
                 for name, args in out.get('tool_calls', [])]
        message = {'role': 'assistant', 'text': out.get('text', ''),
                   'tool_calls': [{'id': c.id, 'name': c.name, 'arguments': c.arguments} for c in calls]}
        stop = out.get('stop_reason') or ('tool_use' if calls else 'end_turn')
        return message, LLMResponse(out.get('text', ''), calls, stop,
                                    Usage(out.get('input', 1000), out.get('output', 200)), self.model)


def make_client(cfg: Any, role: str, secrets: Any) -> HTTPClient:
    """Production factory: build the client configured for ``role``."""
    agent = cfg.agent(role)
    prov = cfg.provider(agent['provider'])
    key = secrets.get(prov['key']) or (secrets.get(prov['fallback_key']) if prov.get('fallback_key') else None)
    if not key:
        raise LLMError(f"repository secret {prov['key']} is not set (needed by the {role} agent)")
    kind = prov.get('kind')
    options = {'max_tokens_param': prov.get('max_tokens_param'),
               'tool_message_name': prov.get('tool_message_name', False),
               'drop_message_keys': prov.get('drop_message_keys', ['annotations']),
               'reasoning_effort': agent.get('reasoning_effort'),
               'strip_schema_keys': prov.get('strip_schema_keys') or [],
               'min_interval_seconds': prov.get('min_interval_seconds', 0)}
    if kind == 'anthropic':
        options.update(effort=agent.get('effort'), thinking=agent.get('thinking'))
        return AnthropicClient(agent['model'], key, prov['base_url'], agent.get('max_tokens', 16000), options)
    if kind == 'openai':
        return OpenAIClient(agent['model'], key, prov['base_url'], agent.get('max_tokens', 16000), options)
    raise LLMError(f'unsupported provider kind {kind!r}')
