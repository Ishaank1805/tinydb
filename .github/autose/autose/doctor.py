"""Pre-flight checks for a fresh setup: config, prompts, secrets, models, repo settings, harness tests."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import events as gha
from . import prompts
from .config import HARNESS_DIR, Config, ConfigError
from .github import GitHub, GitHubError
from .llm import LLMError, ToolResult, make_client
from .schema import obj, string
from .tools import Tool

PROMPTS = ['_security', 'requirements', 'design', 'implementer', 'test_author', 'reviewer', 'judge', 'generalist']


def ping(client: Any) -> str | None:
    """Two-turn tool round trip: proves auth, model id and the tool-use message format."""
    tool = Tool('echo', 'Echo the given text back.', obj({'text': string()}, ['text']))
    conv = client.conversation('You are a connectivity test. Do exactly what the user asks.', [tool])
    conv.add_user('Call the echo tool with the text "ok". Do not reply in text.')
    first = conv.send()
    if not first.tool_calls:
        return f'no tool call in the reply (stop reason {first.stop_reason!r}); tool use may not work with this model'
    conv.add_tool_results([ToolResult(first.tool_calls[0].id, 'echo', 'ok')])
    conv.add_user('Thanks. Now reply with the single word: done')
    conv.send()
    return None


def run_doctor(cfg: Config, secrets: Any, skip_models: bool = False, skip_tests: bool = False) -> int:
    rows: list[tuple[str, str, str]] = []

    def add(status: str, check: str, detail: str = '') -> None:
        rows.append((status, check, detail))
        print(f'{status} {check}: {detail}', flush=True)

    variants = []
    for name in cfg.variants():
        try:
            v = Config.load(variant=name)
            variants.append(v)
            problems = v.validate()
            add('❌' if problems else '✅', f'config ({name})', '; '.join(problems) or 'valid')
        except ConfigError as exc:
            add('❌', f'config ({name})', str(exc))
    repo = os.environ.get('GITHUB_REPOSITORY')
    if repo:
        add('✅' if repo in (cfg.get('repository.allowed') or []) else '❌', 'repository guard',
            f'{repo} vs allowed {cfg.get("repository.allowed")}')
    missing = [p for p in PROMPTS if not (prompts.PROMPT_DIR / f'{p}.md').exists()]
    add('❌' if missing else '✅', 'prompt files', ', '.join(missing) or 'all present')
    _, fallback, _ = prompts.skill()
    add('⚠️' if fallback else '✅', 'A1 spec-writing skill',
        'FALLBACK in use - copy your A1 skill into .github/autose/skills/spec-writer/' if fallback else 'installed')
    repo_dir = Path(os.environ.get('AUTOSE_REPO_DIR') or '.')
    arch = repo_dir / (cfg.get('target.architecture_doc') or 'ARCHITECTURE.md')
    add('✅' if arch.is_file() else '⚠️', 'A2 ARCHITECTURE.md', str(arch) if arch.is_file() else f'{arch} not found')
    workflow = repo_dir / '.github' / 'workflows' / 'autose.yml'
    if workflow.is_file():
        text = workflow.read_text(encoding='utf-8')
        ok = f"'{cfg.get('trigger.label')}'" in text and f"'{cfg.get('trigger.command')}'" in text
        add('✅' if ok else '❌', 'trigger literals in autose.yml', 'match the config' if ok else 'label/command differ')
    pairs = {}
    for v in variants:
        for role in v.roles_in_use():
            spec = v.agent(role)
            pairs.setdefault((spec['provider'], spec['model']), (v, role))
    for (provider, model), (v, role) in sorted(pairs.items()):
        prov = v.provider(provider)
        key = prov.get('key')
        if not secrets.get(key):
            fallback = prov.get('fallback_key')
            if not (fallback and secrets.get(fallback)):
                add('❌', f'secret for {provider}', f'{key} is not set (needed for {model})')
                continue
            add('⚠️', f'secret for {provider}', f'{key} is not set; {fallback} is used instead')
        if skip_models:
            add('⏭️', f'model {model}', 'skipped')
            continue
        try:
            problem = ping(make_client(v, role, secrets))
            add('⚠️' if problem else '✅', f'model {provider}/{model}', problem or 'tool round trip works')
        except LLMError as exc:
            add('❌', f'model {provider}/{model}', str(exc)[:300])
    if repo and secrets.github_token:
        gh = GitHub(secrets.github_token, repo)
        try:
            info = gh.get_repo()
            add('✅' if info.get('has_issues') else '❌', 'issues enabled', str(info.get('has_issues')))
            branch = gh.get_branch(info['default_branch'])
            add('✅' if branch.get('protected') else '⚠️', f"branch protection on {info['default_branch']}",
                str(branch.get('protected')))
            gh.get_workflow(cfg.get('pipeline.ci_workflow') or 'ci.yml')
            add('✅', 'CI workflow', cfg.get('pipeline.ci_workflow'))
        except GitHubError as exc:
            add('❌', 'GitHub settings', str(exc)[:300])
    if not skip_tests:
        proc = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-o', 'addopts=', str(HARNESS_DIR / 'tests')],
                              capture_output=True, text=True, cwd=HARNESS_DIR)
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-1:] or ['no output']
        add('✅' if proc.returncode == 0 else '❌', 'harness unit tests', tail[0])
        if proc.returncode != 0:
            print(proc.stdout[-6000:], proc.stderr[-3000:])
    table = '| | Check | Detail |\n|---|---|---|\n' + '\n'.join(f'| {s} | {c} | {d} |' for s, c, d in rows)
    gha.step_summary('### AutoSE doctor\n\n' + table)
    return 1 if any(s == '❌' for s, _, _ in rows) else 0
