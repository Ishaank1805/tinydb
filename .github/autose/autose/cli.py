"""Entry point: ``python -m autose <command>`` (each workflow step runs one command)."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import events as gha
from .config import Config, ConfigError
from .secretstore import Secrets


def setup_target(cfg: Config) -> int:
    """Create the target project's own virtualenv and install it (no agent involved)."""
    import subprocess

    from .sandbox import Sandbox
    venv = Path(os.environ.get('AUTOSE_TARGET_VENV') or 'target-venv')
    repo = Path(os.environ.get('AUTOSE_REPO_DIR') or 'repo')
    subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
    result = Sandbox(repo, venv, timeout=1500, max_output=20000).run(cfg.req('target.setup'), timeout=1500)
    print(result.render())
    return 0 if result.exit_code == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='autose')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('gate', 'setup-target', 'build', 'review-prepare', 'review', 'review-post', 'report'):
        sub.add_parser(name)
    doctor = sub.add_parser('doctor')
    doctor.add_argument('--skip-models', action='store_true')
    doctor.add_argument('--skip-tests', action='store_true')
    local = sub.add_parser('local', help='dry run against a local checkout (no push, no PR, no CI)')
    local.add_argument('--repo', required=True)
    local.add_argument('--issue-file', required=True, help='JSON with number, title, body')
    local.add_argument('--state', default='.autose-local')
    args = parser.parse_args(argv)

    secrets = Secrets.load()          # first: delete the secret files, scrub the environment
    try:
        cfg = Config.load()
    except ConfigError as exc:
        gha.error(str(exc))
        return 1
    repo = os.environ.get('GITHUB_REPOSITORY')
    if repo and args.command != 'doctor' and repo not in (cfg.get('repository.allowed') or []):
        gha.error(f'{repo} is not in repository.allowed of autose.config.yml; refusing to run.')
        return 1

    if args.command == 'gate':
        from .gate import run_gate
        return run_gate(cfg, secrets)
    if args.command == 'setup-target':
        return setup_target(cfg)
    if args.command == 'build':
        from .context import build_ctx
        from .orchestrator import run_job
        job = 'build-r0' if int(os.environ.get('AUTOSE_ROUND') or 0) == 0 else f"rework-r{os.environ['AUTOSE_ROUND']}"
        return run_job(build_ctx(job, cfg, secrets))
    if args.command == 'review-prepare':
        from .review import prepare
        return prepare(cfg, secrets)
    if args.command == 'review':
        from .review import review
        return review(cfg, secrets)
    if args.command == 'review-post':
        from .review import post
        return post(cfg, secrets)
    if args.command == 'report':
        from .report import run_report
        return run_report(cfg, secrets)
    if args.command == 'doctor':
        from .doctor import run_doctor
        return run_doctor(cfg, secrets, args.skip_models, args.skip_tests)
    if args.command == 'local':
        import json
        import shutil

        from .context import build_ctx
        from .orchestrator import run_job
        state = Path(args.state).resolve()
        shutil.rmtree(state, ignore_errors=True)
        inbox = state.parent / (state.name + '-in') / 'issue'
        inbox.mkdir(parents=True, exist_ok=True)
        issue = json.loads(Path(args.issue_file).read_text())
        issue.setdefault('sanitize_note', 'local run')
        (inbox / 'issue.json').write_text(json.dumps(issue))
        os.environ.setdefault('AUTOSE_ISSUE', str(issue['number']))
        os.environ.setdefault('AUTOSE_BRANCH', f"{cfg.get('repository.branch_prefix')}{issue['number']}")
        ctx = build_ctx('build-r0', cfg, secrets, repo_dir=args.repo, state_dir=state, input_dir=inbox.parent,
                        use_github=False)
        return run_job(ctx)
    return 2
