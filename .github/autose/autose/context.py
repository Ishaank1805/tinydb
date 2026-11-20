"""Everything a stage needs, assembled once per job."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import prompts
from .agent import Agent, AgentResult
from .config import Config
from .events import EventLog
from .github import GitHub
from .gitops import Git
from .llm import make_client
from .sandbox import Sandbox
from .secretstore import Secrets
from .state import State
from .status import StatusBoard, stage_table
from .tools import Workspace, make_tool
from .toolspecs import SHARED_TOOLS
from .usage import Budget, UsageLedger

ARTIFACTS = ('spec.md', 'design.md', 'diff.patch', 'test_report.md', 'pr.md', 'review.md')


@dataclass
class RunInfo:
    repo: str
    issue: int
    round: int
    variant: str
    branch: str
    start_stage: str
    run_id: str
    run_url: str
    status_comment_id: str
    job: str

    @classmethod
    def from_env(cls, job: str) -> 'RunInfo':
        env = os.environ
        repo = env.get('GITHUB_REPOSITORY', 'local/repo')
        run_id = env.get('GITHUB_RUN_ID', '')
        server = env.get('GITHUB_SERVER_URL', 'https://github.com')
        return cls(repo=repo, issue=int(env.get('AUTOSE_ISSUE') or 0), round=int(env.get('AUTOSE_ROUND') or 0),
                   variant=env.get('AUTOSE_VARIANT') or 'baseline', branch=env.get('AUTOSE_BRANCH') or '',
                   start_stage=env.get('AUTOSE_START_STAGE') or 'requirements', run_id=run_id,
                   run_url=f'{server}/{repo}/actions/runs/{run_id}' if run_id else '',
                   status_comment_id=env.get('AUTOSE_STATUS_COMMENT_ID') or '', job=job)


@dataclass
class Ctx:
    cfg: Config
    run: RunInfo
    state: State
    events: EventLog
    ledger: UsageLedger
    budget: Budget
    secrets: Secrets
    gh: GitHub | None
    git: Git
    repo_dir: Path
    sandbox: Sandbox
    issue: dict
    make_llm: Callable[[str], Any]
    status: StatusBoard
    scratch: Path
    shared_agent: Agent | None = None
    progress: dict = field(default_factory=dict)

    @property
    def test_file(self) -> str:
        return self.cfg.req('target.new_test_file').format(issue=self.run.issue)

    @property
    def source_globs(self) -> list[str]:
        return list(self.cfg.get('target.source_globs') or [])

    @property
    def doc_globs(self) -> list[str]:
        return list(self.cfg.get('target.doc_globs') or [])

    @property
    def protected_globs(self) -> list[str]:
        return list(self.cfg.get('target.protected_globs') or [])

    @property
    def base_sha(self) -> str:
        return self.state.data['base_sha']

    def workspace(self, writable: list[str] | tuple = ()) -> Workspace:
        return Workspace(self.repo_dir, self.cfg.get('target.hidden_globs') or [], self.protected_globs, writable)

    def artifact_paths(self) -> dict[str, Path]:
        return {name: self.state.artifact_path(name) for name in ARTIFACTS}

    def run_agent(self, role: str, stage: str, system_text: str, handlers: dict, finish_hint: str,
                  task: str) -> AgentResult:
        steps = int(self.cfg.agent(role).get('max_steps', 30))
        if self.cfg.get('pipeline.shared_context') and role in ('planner', 'implementer', 'test_author'):
            if self.shared_agent is None:
                tools = [make_tool(name, None) for name in SHARED_TOOLS]
                conv = self.make_llm('generalist').conversation(prompts.system('generalist'), tools)
                self.shared_agent = self._agent('generalist', conv, tools, steps, stage)
            self.shared_agent.bind(handlers, stage, finish_hint)
            task = f'# Stage: {stage}\n\n## Instructions for this stage\n{system_text}\n\n## Task\n{task}'
            return self.shared_agent.run(task, stage=stage, finish_hint=finish_hint, max_steps=steps)
        tools = [make_tool(name, handler) for name, handler in handlers.items()]
        conv = self.make_llm(role).conversation(system_text, tools)
        agent = self._agent(role, conv, tools, steps, stage)
        return agent.run(task, stage=stage, finish_hint=finish_hint)

    def _agent(self, role: str, conv: Any, tools: list, steps: int, stage: str) -> Agent:
        return Agent(role=role, conversation=conv, tools=tools, max_steps=steps, ledger=self.ledger,
                     budget=self.budget, events=self.events, transcript=self.state.transcript(role, stage),
                     output_limit=int(self.cfg.get('budgets.max_tool_output_chars', 12000)),
                     redact=self.secrets.redact)

    def mark(self, stage: str, status: str, note: str = '') -> None:
        item = self.progress.setdefault(stage, {})
        item.update(status=status, note=note)
        self.events.emit('stage', stage=stage, status=status, round=self.run.round, note=note[:300])
        if status == 'running':
            self.state.data['current_stage'] = stage
        self.state.data.setdefault('progress', {})[f'r{self.run.round}'] = self.progress
        self.state.save()
        title = ('Build · stages 1-5' if self.run.round == 0 else f'Rework · round {self.run.round}') + \
            (f' · [run]({self.run.run_url})' if self.run.run_url else '')
        self.status.update(stage_table(self.progress, title))


def build_ctx(job: str, cfg: Config, secrets: Secrets, *, repo_dir: str | Path | None = None,
              state_dir: str | Path | None = None, input_dir: str | Path | None = None,
              gh: Any = None, make_llm: Callable[[str], Any] | None = None, use_github: bool = True) -> Ctx:
    env = os.environ
    run = RunInfo.from_env(job)
    state_dir = Path(state_dir or env.get('AUTOSE_STATE_DIR') or Path.cwd() / '.autose-state')
    input_dir = Path(input_dir or env.get('AUTOSE_INPUT_DIR') or state_dir.parent / 'autose-in')
    state = State.init_from(state_dir, input_dir / 'state')
    events = EventLog(state.root / 'events' / f'{job}.jsonl', job)
    ledger = UsageLedger(state.root / 'usage' / f'{job}.json', cfg.get('pricing') or {}, job)
    repo_dir = Path(repo_dir or env.get('AUTOSE_REPO_DIR') or 'repo').resolve()
    if gh is None and use_github and secrets.github_token and run.repo in (cfg.get('repository.allowed') or []):
        gh = GitHub(secrets.github_token, run.repo)
    scratch = Path(env.get('AUTOSE_SCRATCH_DIR') or tempfile.mkdtemp(prefix='autose-scratch-'))
    scratch.mkdir(parents=True, exist_ok=True)
    sandbox = Sandbox(repo_dir, env.get('AUTOSE_TARGET_VENV') or None,
                      timeout=int(cfg.get('budgets.command_timeout_seconds', 300)),
                      max_output=int(cfg.get('budgets.max_tool_output_chars', 12000)),
                      deny_patterns=cfg.get('sandbox.deny_patterns') or [], home=scratch / 'home')
    issue = state.read_data('issue') or {}
    snapshot = input_dir / 'issue' / 'issue.json'
    if snapshot.exists():
        import json
        issue = json.loads(snapshot.read_text(encoding='utf-8'))
        state.write_data('issue', issue)
    if not run.issue and issue.get('number'):
        run.issue = int(issue['number'])
    return Ctx(cfg=cfg, run=run, state=state, events=events, ledger=ledger, budget=Budget(cfg, ledger),
               secrets=secrets, gh=gh, git=Git(repo_dir, secrets.github_token), repo_dir=repo_dir,
               sandbox=sandbox, issue=issue, make_llm=make_llm or (lambda role: make_client(cfg, role, secrets)),
               status=StatusBoard(gh, run.status_comment_id, job, events), scratch=scratch)
