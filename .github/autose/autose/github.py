"""Minimal GitHub REST client used by the deterministic (non-agent) steps.

It deliberately has no method that merges a pull request: the merge decision
belongs to a person. ``tests/test_policy.py`` checks that this stays true.
"""
from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import quote

import requests


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class GitHub:
    def __init__(self, token: str, repo: str, api_url: str | None = None, session: Any = None):
        if not token:
            raise GitHubError(0, 'no GitHub token available for this step')
        self.repo = repo
        self.owner = repo.split('/')[0]
        self.api = (api_url or os.environ.get('GITHUB_API_URL') or 'https://api.github.com').rstrip('/')
        self.session = session or requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'autose-harness',
        })

    def request(self, method: str, path: str, *, params: dict | None = None, json: Any = None,
                accept: str | None = None, raw: bool = False, ok: tuple = (200, 201, 204)) -> Any:
        url = path if path.startswith('http') else f'{self.api}{path}'
        headers = {'Accept': accept} if accept else None
        resp = None
        for attempt in range(5):
            resp = self.session.request(method, url, params=params, json=json, headers=headers, timeout=60)
            limited = resp.status_code in (403, 429) and (
                'retry-after' in resp.headers or 'rate limit' in resp.text.lower())
            if resp.status_code in (500, 502, 503, 504) or limited:
                wait = int(resp.headers.get('retry-after') or 0) or 5 * 2 ** attempt
                time.sleep(min(wait, 60))
                continue
            break
        assert resp is not None
        if resp.status_code not in ok:
            raise GitHubError(resp.status_code, f'{method} {path} -> {resp.status_code}: {resp.text[:400]}')
        if raw:
            return resp.text
        if not resp.content:
            return {}
        return resp.json()

    @property
    def _r(self) -> str:
        return f'/repos/{self.repo}'

    # --- repository -----------------------------------------------------
    def get_repo(self) -> dict:
        return self.request('GET', self._r)

    def get_branch(self, name: str) -> dict:
        return self.request('GET', f'{self._r}/branches/{quote(name, safe="")}')

    def get_workflow(self, file_name: str) -> dict:
        return self.request('GET', f'{self._r}/actions/workflows/{quote(file_name, safe="")}')

    # --- issues, comments, labels -----------------------------------------
    def get_issue(self, number: int) -> dict:
        return self.request('GET', f'{self._r}/issues/{number}')

    def create_comment(self, number: int, body: str) -> dict:
        return self.request('POST', f'{self._r}/issues/{number}/comments', json={'body': body[:65000]})

    def get_comment(self, comment_id: int | str) -> dict:
        return self.request('GET', f'{self._r}/issues/comments/{comment_id}')

    def update_comment(self, comment_id: int | str, body: str) -> dict:
        return self.request('PATCH', f'{self._r}/issues/comments/{comment_id}', json={'body': body[:65000]})

    def add_reaction(self, comment_id: int | str, content: str = 'eyes') -> None:
        self.request('POST', f'{self._r}/issues/comments/{comment_id}/reactions', json={'content': content})

    def ensure_label(self, name: str, color: str = '5319e7', description: str = '') -> None:
        try:
            self.request('GET', f'{self._r}/labels/{quote(name, safe="")}')
        except GitHubError as exc:
            if exc.status != 404:
                raise
            self.request('POST', f'{self._r}/labels',
                         json={'name': name, 'color': color, 'description': description[:100]})

    def add_labels(self, number: int, labels: list[str]) -> None:
        if labels:
            self.request('POST', f'{self._r}/issues/{number}/labels', json={'labels': labels})

    def remove_label(self, number: int, label: str) -> None:
        self.request('DELETE', f'{self._r}/issues/{number}/labels/{quote(label, safe="")}',
                     ok=(200, 204, 404))

    def permission(self, login: str) -> str:
        data = self.request('GET', f'{self._r}/collaborators/{quote(login, safe="")}/permission')
        return data.get('role_name') or data.get('permission') or 'none'

    # --- pull requests ----------------------------------------------------
    def find_pr(self, branch: str, state: str = 'open') -> dict | None:
        pulls = self.request('GET', f'{self._r}/pulls',
                             params={'head': f'{self.owner}:{branch}', 'state': state, 'per_page': 10})
        return pulls[0] if pulls else None

    def create_pr(self, head: str, base: str, title: str, body: str) -> dict:
        return self.request('POST', f'{self._r}/pulls',
                            json={'title': title, 'head': head, 'base': base, 'body': body[:65000]})

    def update_pr(self, number: int, **fields: Any) -> dict:
        if 'body' in fields and fields['body']:
            fields['body'] = fields['body'][:65000]
        return self.request('PATCH', f'{self._r}/pulls/{number}', json=fields)

    def get_pr(self, number: int) -> dict:
        return self.request('GET', f'{self._r}/pulls/{number}')

    def pr_diff(self, number: int) -> str:
        return self.request('GET', f'{self._r}/pulls/{number}', accept='application/vnd.github.diff', raw=True)

    def create_review(self, number: int, body: str, commit_id: str | None = None) -> dict:
        # Always a COMMENT review: the workflow's own identity opened the PR, and
        # GitHub does not let an identity approve or reject its own PR anyway.
        payload: dict[str, Any] = {'body': body[:65000], 'event': 'COMMENT'}
        if commit_id:
            payload['commit_id'] = commit_id
        return self.request('POST', f'{self._r}/pulls/{number}/reviews', json=payload)

    # --- actions ----------------------------------------------------------
    def dispatch(self, workflow: str, ref: str, inputs: dict[str, str]) -> None:
        self.request('POST', f'{self._r}/actions/workflows/{quote(workflow, safe="")}/dispatches',
                     json={'ref': ref, 'inputs': inputs}, ok=(200, 204))

    def workflow_runs(self, workflow: str, **params: Any) -> list[dict]:
        params.setdefault('per_page', 30)
        data = self.request('GET', f'{self._r}/actions/workflows/{quote(workflow, safe="")}/runs',
                            params=params)
        return data.get('workflow_runs', [])

    def get_run(self, run_id: int | str) -> dict:
        return self.request('GET', f'{self._r}/actions/runs/{run_id}')

    def run_jobs(self, run_id: int | str) -> list[dict]:
        return self.request('GET', f'{self._r}/actions/runs/{run_id}/jobs').get('jobs', [])

    def job_log(self, job_id: int | str) -> str:
        return self.request('GET', f'{self._r}/actions/jobs/{job_id}/logs', raw=True)
