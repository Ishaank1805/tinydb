"""Facts about a pull request, computed by the harness from the diff alone.

The reviewer starts from what changed, not from what anyone says changed. Everything
here comes from the PR diff and the repository at the PR head, which is exactly what the
reviewer may see: the pre-change tree is rebuilt by reverse-applying the diff to a copy
of the head checkout, so no other commit is ever checked out.
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .tools import matches

COMPARE_SCRIPT = '''#!/usr/bin/env bash
# Run one probe script on the PR head and on the code before the change (rebuilt from the diff).
# Any difference in behaviour must be one the issue asks for.
target="$1"
if [ "$target" = "python" ] || [ "$target" = "python3" ]; then
    shift
    target="$1"
fi
probe=""
for cand in "$target" "$SCRATCH/$target" "$SCRATCH/${target#scratch/}" "$SCRATCH/${target#./scratch/}"; do
    if [ -n "$cand" ] && [ -f "$cand" ]; then
        probe="$cand"
        break
    fi
done
if [ -z "$probe" ]; then
    echo "probe file not found: $target" >&2
    exit 2
fi
echo "=== PR head ==="
PYTHONPATH="$REPO" python "$probe"; echo "[exit code $?]"
echo "=== before the change ==="
PYTHONPATH="$BASE" python "$probe"; echo "[exit code $?]"
'''


@dataclass
class FileDiff:
    path: str
    new_file: bool = False
    deleted: bool = False
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)


def parse_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    in_hunk = False
    for line in text.splitlines():
        if line.startswith('diff --git '):
            m = re.match(r'diff --git a/(.+?) b/(.+)$', line)
            current = FileDiff(m.group(2) if m else line.split()[-1][2:])
            files.append(current)
            in_hunk = False
        elif current is None:
            continue
        elif line.startswith('@@'):
            in_hunk = True
        elif not in_hunk:
            if line.startswith('new file mode'):
                current.new_file = True
            elif line.startswith('deleted file mode'):
                current.deleted = True
        elif line.startswith('+'):
            current.added.append(line[1:])
        elif line.startswith('-'):
            current.removed.append(line[1:])
    return files


def build_base_tree(repo: Path, diff_path: Path, dest: Path) -> str | None:
    """Copy the head checkout to ``dest`` and reverse-apply the diff. Returns an error or None."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(repo, dest, ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache', '.venv'))
    shutil.rmtree(dest / '.github' / 'autose', ignore_errors=True)        # the harness stays hidden
    proc = subprocess.run(['git', 'apply', '-R', '--whitespace=nowarn', str(diff_path)], cwd=dest,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        return (proc.stderr.strip() or 'git apply -R failed')[:300]
    return None


def write_compare_script(scratch: Path) -> Path:
    path = scratch / 'compare.sh'
    path.write_text(COMPARE_SCRIPT, encoding='utf-8')
    path.chmod(0o755)
    return path


def _functions(source: str) -> dict[str, str] | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    out: dict[str, str] = {}

    def visit(nodes: list, prefix: str) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[prefix + node.name] = ast.dump(node)
                visit(node.body, prefix + node.name + '.')
            elif isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + '.')

    visit(tree.body, '')
    module = [ast.dump(n) for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    out['<module-level code>'] = '\n'.join(module)
    return out


def compute(diff_text: str, head: Path, base: Path | None, issue_text: str, source_globs: list[str],
            test_dir: str = 'tests', doc_globs: list[str] | tuple = ()) -> dict:
    files = parse_diff(diff_text)
    kinds: dict[str, list[FileDiff]] = {'source': [], 'tests': [], 'docs': [], 'other': []}
    for f in files:
        if f.path.startswith(test_dir.rstrip('/') + '/'):
            kinds['tests'].append(f)
        elif matches(f.path, source_globs):
            kinds['source'].append(f)
        elif matches(f.path, doc_globs):
            kinds['docs'].append(f)
        else:
            kinds['other'].append(f)
    functions = []
    for f in kinds['source'] + kinds['tests']:
        if not f.path.endswith('.py'):
            continue
        head_src = (head / f.path).read_text(encoding='utf-8') if (head / f.path).is_file() else ''
        if base is not None and (base / f.path).is_file():
            base_src = (base / f.path).read_text(encoding='utf-8')
        elif f.new_file:
            base_src = ''
        else:
            continue
        new, old = _functions(head_src) or {}, _functions(base_src) or {}
        for name in sorted(set(new) | set(old)):
            if name == '<module-level code>' and not old.get(name) and not new.get(name):
                continue
            if name not in old or (name == '<module-level code>' and not old.get(name)):
                change = 'added'
            elif name not in new:
                change = 'removed'
            elif new[name] != old[name]:
                change = 'changed'
            else:
                continue
            functions.append({'path': f.path, 'name': name, 'change': change})
    edited_tests = []
    for f in kinds['tests']:
        if f.new_file:
            continue
        touched = [x['name'] for x in functions if x['path'] == f.path and x['change'] in ('changed', 'removed')
                   and x['name'] != '<module-level code>']
        assertions = sum(1 for line in f.removed if re.search(r'\bassert\b|pytest\.raises', line))
        edited_tests.append({'path': f.path, 'tests': touched, 'assertion_lines_removed_or_changed': assertions})
    words = issue_text.lower()
    source_paths = {f.path for f in kinds['source']}
    beyond = [f"{x['path']}: {x['name']}" for x in functions if x['path'] in source_paths and x['change'] != 'added'
              and x['name'] != '<module-level code>' and x['name'].split('.')[-1].lower() not in words]
    return {'files': {k: [f.path for f in v] for k, v in kinds.items()}, 'functions': functions,
            'edited_tests': edited_tests, 'changed_not_named_in_issue': beyond}


def markdown(facts: dict, base_status: str) -> str:
    def names(change: str, tests: bool = False) -> str:
        items = [f"`{x['name']}` ({x['path']})" for x in facts['functions'] if x['change'] == change
                 and (x['path'] in facts['files']['tests']) == tests]
        return ', '.join(items) or 'none'
    files = facts['files']
    lines = ['## Facts computed by the harness from the diff (not the author\'s claims)', '',
             f"- Files changed: source {files['source'] or 'none'}; tests {files['tests'] or 'none'}; "
             f"docs {files['docs'] or 'none'}; other {files['other'] or 'none'}",
             f"- Source functions changed: {names('changed')}; added: {names('added')}; removed: {names('removed')}"]
    edited = [t for t in facts['edited_tests'] if t['tests'] or t['assertion_lines_removed_or_changed']]
    appended = [t['path'] for t in facts['edited_tests'] if t not in edited]
    if appended:
        lines.append(f"- New tests added to existing test files {appended}; no existing test in them changed.")
    if edited:
        for t in edited:
            lines.append(f"- ⚠️ Existing test file edited: `{t['path']}`; tests changed or removed: "
                         f"{', '.join(t['tests']) or 'none detected'}; assertion lines removed or changed: "
                         f"{t['assertion_lines_removed_or_changed']}")
    elif not appended:
        lines.append('- No existing test file is touched (new test files only).')
    if facts['changed_not_named_in_issue']:
        lines.append('- Changed functions the issue text does not name (check that each change is needed): '
                     + ', '.join(facts['changed_not_named_in_issue']))
    lines.append(f'- Code before the change: {base_status}')
    return '\n'.join(lines)
