"""Run pytest deterministically and read its JUnit XML report."""
from __future__ import annotations

import re
import shlex
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Case:
    classname: str
    name: str
    outcome: str
    message: str = ''

    @property
    def key(self) -> str:
        return f'{self.classname}::{self.name}'


@dataclass
class SuiteResult:
    exit_code: int
    cases: list[Case] = field(default_factory=list)
    output: str = ''
    timed_out: bool = False
    command: str = ''

    def count(self, outcome: str) -> int:
        return sum(1 for c in self.cases if c.outcome == outcome)

    @property
    def ok(self) -> bool:
        return (self.exit_code == 0 and not self.timed_out and self.count('failed') == 0
                and self.count('error') == 0)

    def summary(self) -> str:
        return (f"{self.count('passed')} passed, {self.count('failed')} failed, "
                f"{self.count('error')} errors, {self.count('skipped')} skipped")

    def failing(self) -> list[Case]:
        return [c for c in self.cases if c.outcome in ('failed', 'error')]


def parse_junit(path: str | Path) -> list[Case]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    cases = []
    for tc in root.iter('testcase'):
        outcome, message = 'passed', ''
        for tag, name in (('failure', 'failed'), ('error', 'error'), ('skipped', 'skipped')):
            element = tc.find(tag)
            if element is not None:
                outcome = name
                message = ((element.get('message') or '') + '\n' + (element.text or '')).strip()[:3000]
                break
        cases.append(Case(tc.get('classname', ''), tc.get('name', ''), outcome, message))
    return cases


def run_tests(sandbox: Any, test_cmd: str, junit_path: str | Path, targets: tuple | list = (),
              timeout: int | None = None) -> SuiteResult:
    junit_path = Path(junit_path)
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    junit_path.unlink(missing_ok=True)
    cmd = f'{test_cmd} --junitxml={shlex.quote(str(junit_path))}' + ''.join(
        ' ' + shlex.quote(str(t)) for t in targets)
    result = sandbox.run(cmd, timeout=timeout or 900, max_output=20000)
    return SuiteResult(result.exit_code, parse_junit(junit_path), result.output, result.timed_out, cmd)


_NODE = re.compile(r'^(\S+\.py::\S.*)$')


def collect(sandbox: Any, collect_cmd: str, targets: tuple | list = (), extra: str = '') -> tuple[set[str], bool, str]:
    cmd = collect_cmd + (f' {extra}' if extra else '') + ''.join(' ' + shlex.quote(str(t)) for t in targets)
    result = sandbox.run(cmd, timeout=600, max_output=5_000_000)
    ids = {m.group(1).strip() for line in result.output.splitlines() if (m := _NODE.match(line.strip()))}
    return ids, result.exit_code == 0, result.output


@contextmanager
def base_sources(git: Any, base_sha: str, paths: list[str], repo_dir: Path):
    """Temporarily put the base-commit version of ``paths`` back in the work tree."""
    saved: dict[str, bytes | None] = {}
    try:
        for rel in paths:
            saved[rel] = git.worktree_bytes(rel)
            original = git.show_file(base_sha, rel)
            target = repo_dir / rel
            if original is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(original)
        yield
    finally:
        for rel, content in saved.items():
            target = repo_dir / rel
            if content is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
