"""Run agent commands with a scrubbed environment, timeouts and output caps.

This is not a container: it is one layer of several. Agents get no
credentials (see secretstore.py), refused commands never start, and every
file change is re-validated against the path policy before it is committed.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

SYSTEM_PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'


def truncate_middle(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit // 4
    tail = limit - head
    return f'{text[:head]}\n[... {len(text) - limit} characters omitted ...]\n{text[-tail:]}'


@dataclass
class CmdResult:
    command: str
    exit_code: int
    output: str
    duration: float
    timed_out: bool = False
    denied: str | None = None

    def render(self) -> str:
        if self.denied:
            return f'$ {self.command}\n[refused by the sandbox: {self.denied}]'
        status = 'timed out (killed)' if self.timed_out else f'exit code {self.exit_code}'
        return f'$ {self.command}\n[{status}, {self.duration:.1f}s]\n{self.output}'.rstrip()


class Sandbox:
    def __init__(self, cwd: str | Path, venv: str | Path | None = None, timeout: int = 300,
                 max_output: int = 12000, deny_patterns: list[str] | tuple = (),
                 home: str | Path | None = None, extra_env: dict | None = None):
        self.cwd = Path(cwd)
        self.venv = Path(venv) if venv else None
        self.timeout = timeout
        self.max_output = max_output
        self.deny = [re.compile(p) for p in deny_patterns]
        self.home = Path(home) if home else Path(tempfile.mkdtemp(prefix='autose-home-'))
        self.home.mkdir(parents=True, exist_ok=True)
        self.extra_env = extra_env or {}

    def env(self) -> dict:
        env = {'HOME': str(self.home), 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
               'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1', 'PIP_NO_INPUT': '1',
               'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'TERM': 'dumb', 'NO_COLOR': '1', 'CI': 'true',
               'GIT_TERMINAL_PROMPT': '0', 'TMPDIR': str(self.home / 'tmp')}
        path = SYSTEM_PATH
        if self.venv:
            env['VIRTUAL_ENV'] = str(self.venv)
            path = f'{self.venv}/bin:{path}'
        env['PATH'] = path
        env.update(self.extra_env)
        return env

    def check(self, command: str) -> str | None:
        for pattern in self.deny:
            if pattern.search(command):
                return f'it matches the deny rule {pattern.pattern!r}'
        return None

    def run(self, command: str, timeout: int | None = None, cwd: str | Path | None = None,
            max_output: int | None = None) -> CmdResult:
        denied = self.check(command)
        if denied:
            return CmdResult(command, -1, '', 0.0, denied=denied)
        (self.home / 'tmp').mkdir(parents=True, exist_ok=True)
        limit = timeout or self.timeout
        start = time.time()
        proc = subprocess.Popen(['bash', '-c', command], cwd=str(cwd or self.cwd), env=self.env(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
        timed_out = False
        try:
            out, _ = proc.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                out, _ = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                out = b''
        text = out.decode('utf-8', errors='replace') if out else ''
        cap = self.max_output if max_output is None else max_output
        return CmdResult(command, -9 if timed_out else proc.returncode, truncate_middle(text, cap),
                         time.time() - start, timed_out)
