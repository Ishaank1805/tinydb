"""Deterministic stage checks, plus the optional LLM testability judge."""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tools import matches

AC_ID = re.compile(r'^AC-(\d+)$')
TEST_NAME = re.compile(r'^test_ac(\d+)(?:_|$)')
VAGUE = ['user-friendly', 'user friendly', 'intuitive', 'properly', 'as expected', 'works well', 'should work',
         'seamless', 'seamlessly', 'robust', 'efficient', 'efficiently', 'quickly', 'fast', 'nice', 'better',
         'etc', 'and so on', 'appropriate', 'appropriately', 'reasonable', 'easy to', 'helpful', 'good']


@dataclass
class CheckResult:
    passed: bool
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    output: str = ''

    def render(self) -> str:
        lines = ['✅ passed' if self.passed else '❌ failed']
        lines += [f'- ❌ {p}' for p in self.problems]
        lines += [f'- ℹ️ {n}' for n in self.notes]
        return '\n'.join(lines)


def ac_ids(spec: dict) -> list[str]:
    return [str(ac.get('id', '')).strip() for ac in spec.get('acceptance_criteria') or []]


def check_spec(spec: dict) -> CheckResult:
    problems: list[str] = []
    acs = spec.get('acceptance_criteria') or []
    if not acs:
        problems.append('there are no acceptance criteria')
    if len(acs) > 12:
        problems.append('more than 12 acceptance criteria: tighten the scope')
    seen: set[str] = set()
    for index, ac in enumerate(acs, 1):
        cid = str(ac.get('id', '')).strip()
        if not AC_ID.match(cid):
            problems.append(f'criterion {index}: the id must look like AC-{index}')
        elif cid in seen:
            problems.append(f'{cid}: duplicate id')
        seen.add(cid)
        label = cid or f'criterion {index}'
        for part in ('given', 'when', 'then', 'verification'):
            if len(str(ac.get(part, '')).strip()) < 3:
                problems.append(f'{label}: "{part}" is empty')
        then = str(ac.get('then', ''))
        vague = [w for w in VAGUE if re.search(rf'\b{re.escape(w)}\b', then, re.I)]
        if vague:
            problems.append(f'{label}: "then" uses vague wording ({", ".join(vague)}); state a concrete, '
                            'observable result')
        verification = str(ac.get('verification', ''))
        if not re.search(r'[A-Za-z_][\w.]*\(|\b(assert|raises?|returns?|equals?|==|contains?|is|are|'
                         r'prints?|shows?|has|have)\b', verification, re.I):
            problems.append(f'{label}: "verification" must name the call to make and what to assert')
    return CheckResult(not problems, problems)


def parse_json_object(text: str) -> dict | None:
    text = (text or '').strip()
    fence = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def judge_testability(make_llm: Any, ledger: Any, spec: dict, prompt: str) -> dict[str, tuple[bool, str]] | None:
    """Ask a separate, tool-less model whether each criterion is testable."""
    client = make_llm('judge')
    conv = client.conversation(prompt, [])
    items = [{k: ac.get(k) for k in ('id', 'given', 'when', 'then', 'verification')}
             for ac in spec.get('acceptance_criteria') or []]
    conv.add_user('Classify these acceptance criteria. Reply with JSON only.\n'
                  + json.dumps(items, indent=2))
    response = conv.send()
    ledger.add('judge', 'requirements', response.model, response.usage)
    data = parse_json_object(response.text)
    if not data or not isinstance(data.get('criteria'), list):
        return None
    return {str(c.get('id')): (bool(c.get('testable')), str(c.get('reason', '')))
            for c in data['criteria'] if isinstance(c, dict)}


def python_symbols(path: Path) -> set[str] | None:
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return None
    names: set[str] = set()

    def visit(nodes: list, prefix: str) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f'{prefix}{node.name}'
                names.add(name)
                if isinstance(node, ast.ClassDef):
                    visit(node.body, name + '.')
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        names.add(f'{prefix}{target.id}')
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                visit(getattr(node, 'body', []), prefix)
                visit(getattr(node, 'orelse', []), prefix)

    visit(tree.body, '')
    return names


def normalize_symbol(name: str, known: set[str]) -> str:
    name = re.sub(r'\(.*\)$', '', (name or '').strip()).strip('` ')
    if name in known:
        return name
    parts = name.split('.')
    for i in range(1, len(parts)):          # tolerate module prefixes: tinydb.table.Table.update
        candidate = '.'.join(parts[i:])
        if candidate in known:
            return candidate
    return name


def check_design(design: dict, spec: dict, repo: Path, editable_globs: list[str], protected: list[str],
                 test_file: str, test_dir: str, doc_globs: list[str] | tuple = ()) -> CheckResult:
    problems: list[str] = []
    notes: list[str] = []
    code_symbols = 0
    paths: list[str] = []
    for change in design.get('changes') or []:
        path = str(change.get('path', '')).strip()
        path = path[2:] if path.startswith('./') else path
        if not path:
            problems.append('a change has no path')
            continue
        paths.append(path)
        if matches(path, protected):
            problems.append(f'{path}: protected path')
            continue
        if path == test_file or path.startswith(test_dir.rstrip('/') + '/'):
            problems.append(f'{path}: tests are planned in "tests", not in "changes"')
            continue
        if not matches(path, editable_globs):
            problems.append(f'{path}: not a file this pipeline may change ({", ".join(editable_globs)})')
            continue
        full = repo / path
        if change.get('new_file'):
            if full.exists():
                problems.append(f'{path}: marked new_file but it already exists')
        elif not full.is_file():
            problems.append(f'{path}: does not exist (set new_file: true if it should be created)')
            continue
        symbols = change.get('symbols') or []
        if not path.endswith('.py'):
            continue
        if not symbols:
            problems.append(f'{path}: name the functions, classes or methods to change')
            continue
        known = python_symbols(full) if full.exists() else set()
        if known is None:
            problems.append(f'{path}: cannot be parsed')
            continue
        for symbol in symbols:
            name = normalize_symbol(symbol.get('name', ''), known)
            if not name:
                problems.append(f'{path}: a symbol has no name')
                continue
            code_symbols += 1
            if symbol.get('new'):
                parent = name.rsplit('.', 1)[0] if '.' in name else None
                if parent and parent not in known:
                    problems.append(f'{path}: new symbol {name} needs an existing class {parent}')
                if name in known:
                    notes.append(f'{path}: {name} is marked new but already exists')
            elif name not in known:
                sample = ', '.join(sorted(known)[:30])
                problems.append(f'{path}: {name} does not exist (mark it new if it should be added). '
                                f'Existing names include: {sample}')
    docs_only = bool(paths) and all(matches(p, doc_globs) and not p.endswith('.py') for p in paths)
    if code_symbols == 0 and docs_only:
        notes.append('documentation-only change: there are no functions to name')
    elif code_symbols == 0:
        problems.append('the plan must name at least one function, class or method in a Python file')
    ids = ac_ids(spec)
    covered: set[str] = set()
    for test in design.get('tests') or []:
        name = str(test.get('name', ''))
        covers = [str(c) for c in test.get('covers') or []]
        m = TEST_NAME.match(name)
        if not m:
            problems.append(f'planned test {name!r} must be named test_ac<N>_<what_it_checks>')
            continue
        if f'AC-{m.group(1)}' not in covers:
            problems.append(f'planned test {name}: its number must match a criterion it covers')
        for cid in covers:
            if cid not in ids:
                problems.append(f'planned test {name} covers unknown criterion {cid}')
            covered.add(cid)
    missing = [cid for cid in ids if cid not in covered]
    if missing:
        problems.append('no planned test for ' + ', '.join(missing))
    return CheckResult(not problems, problems, notes)


def test_functions(path: Path) -> list[str] | None:
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return None
    return [n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith('test')]


def tests_by_ac(names: list[str]) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for name in names:
        m = TEST_NAME.match(name)
        if m:
            mapping.setdefault(f'AC-{m.group(1)}', []).append(name)
    return mapping


def check_test_file(path: Path, spec: dict) -> CheckResult:
    if not path.is_file():
        return CheckResult(False, [f'{path.name} does not exist'])
    names = test_functions(path)
    if names is None:
        return CheckResult(False, [f'{path.name} has a syntax error'])
    if not names:
        return CheckResult(False, ['the file contains no test functions'])
    mapping = tests_by_ac(names)
    ids = ac_ids(spec)
    problems = [f'no test named test_ac{cid.split("-")[1]}_... for {cid}' for cid in ids if cid not in mapping]
    notes = [f'tests for unknown criterion {cid}' for cid in mapping if cid not in ids]
    unnamed = [n for n in names if not TEST_NAME.match(n)]
    if unnamed:
        notes.append('tests not tied to a criterion: ' + ', '.join(unnamed))
    return CheckResult(not problems, problems, notes)
