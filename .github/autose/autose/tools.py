"""Tool handlers and the path policy that every file tool goes through."""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from .sandbox import truncate_middle
from .toolspecs import SPECS

SKIP_DIRS = {'.git', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'node_modules',
             '.venv', 'venv', '.tox', '.eggs', '.idea'}


class PolicyError(Exception):
    """A tool refused an action because of the path policy."""


@dataclass
class ToolOutput:
    content: str
    is_error: bool = False
    done: bool = False          # ends the agent session
    outcome: str = ''           # e.g. accepted, redesign, cannot_specify
    payload: Any = None


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[dict], ToolOutput] | None = None


def make_tool(name: str, handler: Callable[[dict], ToolOutput] | None) -> Tool:
    description, schema = SPECS[name]
    return Tool(name, description, schema, handler)


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern:
    out, i = '', 0
    while i < len(pattern):
        if pattern.startswith('**/', i):
            out, i = out + '(?:.*/)?', i + 3
        elif pattern.startswith('**', i):
            out, i = out + '.*', i + 2
        elif pattern[i] == '*':
            out, i = out + '[^/]*', i + 1
        elif pattern[i] == '?':
            out, i = out + '[^/]', i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r'\Z')


def matches(rel: str, globs: list[str] | tuple) -> bool:
    return any(_glob_regex(g).match(rel) for g in (globs or ()))


class Workspace:
    """Resolves agent-supplied paths and enforces hidden / protected / writable rules."""

    def __init__(self, root: str | Path, hidden: list[str] | tuple = (), protected: list[str] | tuple = (),
                 writable: list[str] | tuple = (), scratch: str | Path | None = None):
        self.root = Path(root).resolve()
        self.hidden = list(hidden or [])
        self.protected = list(protected or [])
        self.writable = list(writable or [])
        self.scratch = Path(scratch).resolve() if scratch else None
        if self.scratch:
            self.scratch.mkdir(parents=True, exist_ok=True)

    def is_hidden(self, rel: str) -> bool:
        return matches(rel, self.hidden) or matches(rel.rstrip('/') + '/x', self.hidden)

    def display(self, path: Path) -> str:
        if self.scratch and (path == self.scratch or self.scratch in path.parents):
            sub = path.relative_to(self.scratch).as_posix()
            return 'scratch' if sub == '.' else f'scratch/{sub}'
        return '.' if path == self.root else path.relative_to(self.root).as_posix()

    def resolve(self, path: str | None, write: bool = False) -> tuple[Path, str]:
        raw = (path or '.').strip()
        if self.scratch and (raw == 'scratch' or raw.startswith('scratch/')):
            target = (self.scratch / raw[len('scratch'):].lstrip('/')).resolve()
            if target != self.scratch and self.scratch not in target.parents:
                raise PolicyError(f'{raw}: escapes the scratch directory')
            return target, self.display(target)
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        target = candidate.resolve()
        if target != self.root and self.root not in target.parents:
            raise PolicyError(f'{raw}: outside the repository')
        rel = self.display(target)
        if rel != '.' and self.is_hidden(rel):
            raise PolicyError(f'{rel}: not accessible to agents')
        if write:
            if rel == '.' or matches(rel, self.protected):
                raise PolicyError(f'{rel}: protected path')
            if not matches(rel, self.writable):
                allowed = ', '.join(self.writable) or ('files under scratch/' if self.scratch else 'nothing')
                raise PolicyError(f'{rel}: in this stage you may only change {allowed}')
        return target, rel

    def iter_files(self, base: Path):
        if base.is_file():
            yield base
            return
        for dirpath, dirnames, filenames in os.walk(base):
            current = Path(dirpath)
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS
                                 and not self.is_hidden(self.display(current / d)))
            for name in sorted(filenames):
                path = current / name
                if not self.is_hidden(self.display(path)):
                    yield path


def _read_text(path: Path, limit: int = 3_000_000) -> str | None:
    data = path.read_bytes()
    if len(data) > limit or b'\0' in data[:8192]:
        return None
    return data.decode('utf-8', errors='replace')


def file_handlers(ws: Workspace, write: bool = False) -> dict[str, Callable[[dict], ToolOutput]]:
    def list_files(args: dict) -> ToolOutput:
        base, rel = ws.resolve(args.get('path') or '.')
        if not base.exists():
            return ToolOutput(f'{rel}: does not exist', True)
        if base.is_file():
            return ToolOutput(rel)
        depth = max(1, min(int(args.get('max_depth') or 2), 6))
        out: list[str] = []

        def walk(folder: Path, level: int) -> None:
            try:
                entries = sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name))
            except OSError:
                return
            for entry in entries:
                if len(out) >= 500 or entry.name in SKIP_DIRS:
                    continue
                shown = ws.display(entry)
                if ws.is_hidden(shown):
                    continue
                if entry.is_dir():
                    out.append(shown + '/')
                    if level < depth:
                        walk(entry, level + 1)
                else:
                    out.append(shown)

        walk(base, 1)
        if len(out) >= 500:
            out.append('[... listing truncated at 500 entries ...]')
        return ToolOutput('\n'.join(out) or '(empty directory)')

    def read_file(args: dict) -> ToolOutput:
        path, rel = ws.resolve(args['path'])
        if not path.exists():
            return ToolOutput(f'{rel}: no such file', True)
        if path.is_dir():
            return ToolOutput(f'{rel} is a directory; use list_files', True)
        text = _read_text(path)
        if text is None:
            return ToolOutput(f'{rel}: binary or too large to read', True)
        lines = text.splitlines()
        if not lines:
            return ToolOutput(f'{rel}: (empty file)')
        start = max(1, int(args.get('start_line') or 1))
        if start > len(lines):
            return ToolOutput(f'{rel} has only {len(lines)} lines', True)
        end = int(args.get('end_line') or start + 399)
        end = min(len(lines), max(start, end), start + 599)
        body = '\n'.join(f'{i:>5}| {lines[i - 1]}' for i in range(start, end + 1))
        more = (f'\n[... {len(lines) - end} more lines; call again with start_line={end + 1} ...]'
                if end < len(lines) else '')
        return ToolOutput(f'{rel} (lines {start}-{end} of {len(lines)})\n{body}{more}')

    def search_code(args: dict) -> ToolOutput:
        try:
            pattern = re.compile(args['pattern'])
        except re.error as exc:
            return ToolOutput(f'invalid regular expression: {exc}', True)
        base, _ = ws.resolve(args.get('path') or '.')
        if not base.exists():
            return ToolOutput('path does not exist', True)
        name_glob = args.get('glob')
        limit = max(1, min(int(args.get('max_results') or 80), 300))
        hits: list[str] = []
        for path in ws.iter_files(base):
            if name_glob and not fnmatch.fnmatch(path.name, name_glob):
                continue
            text = _read_text(path, 1_000_000)
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    hits.append(f'{ws.display(path)}:{number}: {line.strip()[:200]}')
                    if len(hits) >= limit:
                        return ToolOutput('\n'.join(hits) + f'\n[... stopped at {limit} matches ...]')
        return ToolOutput('\n'.join(hits) or 'no matches')

    handlers: dict[str, Callable[[dict], ToolOutput]] = {
        'list_files': list_files, 'read_file': read_file, 'search_code': search_code}

    if write:
        def write_file(args: dict) -> ToolOutput:
            path, rel = ws.resolve(args['path'], write=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            content = args['content']
            path.write_text(content, encoding='utf-8')
            return ToolOutput(f'wrote {rel} ({len(content.splitlines())} lines)')

        def edit_file(args: dict) -> ToolOutput:
            path, rel = ws.resolve(args['path'], write=True)
            if not path.is_file():
                return ToolOutput(f'{rel}: no such file (use write_file to create it)', True)
            text = path.read_text(encoding='utf-8')
            old, new = args['old_text'], args['new_text']
            if not old:
                return ToolOutput('old_text must not be empty', True)
            count = text.count(old)
            if count == 0:
                return ToolOutput(f'old_text not found in {rel}; read the file again and copy the exact '
                                  'text, including indentation', True)
            if count > 1 and not args.get('replace_all'):
                return ToolOutput(f'old_text occurs {count} times in {rel}; include more context or set '
                                  'replace_all', True)
            path.write_text(text.replace(old, new) if args.get('replace_all') else text.replace(old, new, 1),
                            encoding='utf-8')
            return ToolOutput(f'edited {rel} ({count if args.get("replace_all") else 1} replacement(s))')

        handlers.update(write_file=write_file, edit_file=edit_file)
    return handlers


def command_handler(sandbox: Any, recorder: list | None = None) -> Callable[[dict], ToolOutput]:
    def run_command(args: dict) -> ToolOutput:
        timeout = args.get('timeout_seconds')
        # Agents may shorten the configured limit (budgets.command_timeout_seconds), never extend it.
        timeout = max(5, min(int(timeout), sandbox.timeout)) if timeout else None
        result = sandbox.run(args['command'], timeout)
        if recorder is not None:
            recorder.append(result)
        return ToolOutput(result.render(), is_error=bool(result.denied or result.timed_out))
    return run_command


def artifact_handler(paths: dict[str, Path]) -> Callable[[dict], ToolOutput]:
    def read_artifact(args: dict) -> ToolOutput:
        path = paths.get(args['name'])
        if not path or not path.exists():
            available = sorted(name for name, p in paths.items() if p.exists())
            return ToolOutput(f'unknown artifact {args["name"]!r}; available: {available}', True)
        return ToolOutput(truncate_middle(path.read_text(encoding='utf-8'), 40000))
    return read_artifact


def skill_handler(folder: Path) -> Callable[[dict], ToolOutput]:
    root = folder.resolve()

    def read_skill_file(args: dict) -> ToolOutput:
        target = (root / args['path']).resolve()
        if target != root and root not in target.parents:
            return ToolOutput('outside the skill folder', True)
        if not target.is_file():
            files = sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file())
            return ToolOutput(f'no such skill file; files: {files}', True)
        return ToolOutput(truncate_middle(target.read_text(encoding='utf-8', errors='replace'), 40000))
    return read_skill_file


def diff_handler(diff_path: Path) -> Callable[[dict], ToolOutput]:
    def get_diff(args: dict) -> ToolOutput:
        lines = diff_path.read_text(encoding='utf-8', errors='replace').splitlines()
        if not lines:
            return ToolOutput('(empty diff)')
        start = max(1, int(args.get('start_line') or 1))
        end = min(len(lines), int(args.get('end_line') or start + 799), start + 799)
        body = '\n'.join(lines[start - 1:end])
        more = f'\n[... {len(lines) - end} more lines; call again with start_line={end + 1} ...]' \
            if end < len(lines) else ''
        return ToolOutput(f'diff lines {start}-{end} of {len(lines)}\n{body}{more}')
    return get_diff
