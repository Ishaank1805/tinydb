"""Event log (events.jsonl), GitHub Actions log commands, outputs and summaries."""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _esc(message: object) -> str:
    return str(message).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def notice(message: object) -> None:
    print(f'::notice::{_esc(message)}', flush=True)


def warning(message: object) -> None:
    print(f'::warning::{_esc(message)}', flush=True)


def error(message: object) -> None:
    print(f'::error::{_esc(message)}', flush=True)


@contextmanager
def group(title: str):
    print(f'::group::{title}', flush=True)
    try:
        yield
    finally:
        print('::endgroup::', flush=True)


def set_outputs(**values: object) -> None:
    lines = []
    for key, value in values.items():
        text = '' if value is None else str(value)
        if '\n' in text:
            delim = f'AUTOSE_{uuid.uuid4().hex}'
            lines.append(f'{key}<<{delim}\n{text}\n{delim}')
        else:
            lines.append(f'{key}={text}')
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write('\n'.join(lines) + '\n')
    for line in lines:
        print(f'[output] {line.splitlines()[0]}', flush=True)


def step_summary(markdown: str) -> None:
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(markdown + '\n')


class EventLog:
    """Append-only JSON-lines log of everything the harness decides."""

    def __init__(self, path: str | Path, job: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.job = job

    def emit(self, event: str, **data: object) -> dict:
        record = {'ts': now_iso(), 'job': self.job, 'event': event, **data}
        with open(self.path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, default=str) + '\n')
        brief = ' '.join(f'{k}={v}' for k, v in data.items() if len(str(v)) <= 120)
        print(f'[autose] {event} {brief}'.rstrip(), flush=True)
        return record

    @staticmethod
    def read(path: str | Path) -> list[dict]:
        path = Path(path)
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out
