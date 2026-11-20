"""Deterministic git operations.

Agents cannot commit, switch branches or talk to the remote (the sandbox
refuses such commands). Only this module does, from the orchestrator, after
the changed files have been re-checked against the path policy.
"""
from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

BOT_NAME = 'github-actions[bot]'
BOT_EMAIL = '41898282+github-actions[bot]@users.noreply.github.com'


class GitError(Exception):
    pass


class Git:
    def __init__(self, repo_dir: str | Path, token: str | None = None):
        self.repo = Path(repo_dir)
        self.token = token

    def _env(self, auth: bool) -> dict:
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': os.environ.get('HOME', '/tmp'),
               'LANG': 'C.UTF-8', 'GIT_TERMINAL_PROMPT': '0'}
        if auth and self.token:
            basic = base64.b64encode(f'x-access-token:{self.token}'.encode()).decode()
            # Passed through the environment of this one git process only, so the
            # token is neither in argv (visible in `ps`) nor written to .git/config.
            env.update({'GIT_CONFIG_COUNT': '1',
                        'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
                        'GIT_CONFIG_VALUE_0': f'AUTHORIZATION: basic {basic}'})
        return env

    def run(self, *args: str, check: bool = True, auth: bool = False, binary: bool = False):
        proc = subprocess.run(['git', *args], cwd=self.repo, env=self._env(auth), capture_output=True,
                              text=not binary, timeout=600)
        if check and proc.returncode != 0:
            err = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode(errors='replace')
            raise GitError(f'git {" ".join(args[:3])} failed: {err.strip()[:600]}')
        return proc.stdout

    def _ok(self, *args: str) -> bool:
        return subprocess.run(['git', *args], cwd=self.repo, env=self._env(False),
                              capture_output=True).returncode == 0

    def head(self) -> str:
        return self.run('rev-parse', 'HEAD').strip()

    def checkout_branch(self, name: str) -> None:
        self.run('checkout', '-q', '-B', name)

    def has_commit(self, sha: str) -> bool:
        return self._ok('cat-file', '-e', f'{sha}^{{commit}}')

    def ensure_commit(self, sha: str) -> None:
        if not self.has_commit(sha):
            self.run('fetch', '-q', '--depth=1', 'origin', sha, auth=True)

    def exists_at(self, ref: str, rel: str) -> bool:
        return self._ok('cat-file', '-e', f'{ref}:{rel}')

    def show_file(self, ref: str, rel: str) -> bytes | None:
        if not self.exists_at(ref, rel):
            return None
        return self.run('show', f'{ref}:{rel}', binary=True)

    def _untracked(self) -> list[str]:
        return [p for p in self.run('ls-files', '--others', '--exclude-standard', '-z').split('\0') if p]

    def changed_files(self, base: str = 'HEAD') -> list[str]:
        tracked = self.run('diff', '--name-only', '--no-renames', '-z', base).split('\0')
        return sorted({p for p in tracked + self._untracked() if p})

    def diff(self, base: str, paths: list[str] | None = None) -> str:
        untracked = self._untracked()
        if paths is not None:
            wanted = set(paths)
            untracked = [p for p in untracked if p in wanted]
        if untracked:
            self.run('add', '--intent-to-add', '--', *untracked)
        args = ['diff', '--no-color', '--no-renames', base]
        if paths is not None:
            if not paths:
                return ''
            args += ['--', *paths]
        return self.run(*args)

    def worktree_bytes(self, rel: str) -> bytes | None:
        path = self.repo / rel
        return path.read_bytes() if path.is_file() else None

    def restore(self, rel: str, content: bytes | None) -> None:
        """Make ``rel`` contain exactly ``content`` (``None`` = file absent)."""
        path = self.repo / rel
        if content is None:
            if path.exists() or path.is_symlink():
                path.unlink()
            self.run('rm', '-q', '--cached', '--ignore-unmatch', '--', rel, check=False)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def reset_to_head(self) -> None:
        self.run('reset', '-q', '--hard', 'HEAD')
        self.run('clean', '-fdq')

    def commit(self, message: str, paths: list[str]) -> str | None:
        if not paths:
            return None
        self.run('add', '-A', '--', *paths)
        if not self.run('diff', '--cached', '--name-only').strip():
            return None
        self.run('-c', f'user.name={BOT_NAME}', '-c', f'user.email={BOT_EMAIL}',
                 'commit', '-q', '-m', message)
        return self.head()

    def push(self, branch: str, force: bool = False) -> None:
        args = ['push', '-q'] + (['--force'] if force else []) + ['origin', f'HEAD:refs/heads/{branch}']
        self.run(*args, auth=True)
