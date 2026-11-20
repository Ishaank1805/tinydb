import subprocess
from types import SimpleNamespace

from autose.events import EventLog
from autose.orchestrator import stop
from autose.review import review_problems
from autose.sandbox import CmdResult
from autose.state import State


def make_ctx(tmp_path):
    run = SimpleNamespace(issue=3, round=0, variant='baseline', job='build-r0', run_url='')
    return SimpleNamespace(state=State(tmp_path / 'state'), run=run, events=EventLog(tmp_path / 'e.jsonl', 't'),
                           gh=None, issue={})


def test_every_stop_is_explained_in_the_stage_artifact(tmp_path):
    ctx = make_ctx(tmp_path)
    stop(ctx, 'release', 'CI ended with timed_out')                 # no pr.md yet: it is created
    assert 'CI ended with timed_out' in ctx.state.read_artifact('pr.md')
    stop(ctx, 'release', 'again')                                   # idempotent
    assert ctx.state.read_artifact('pr.md').count('the run stopped here') == 1
    patch = 'diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n'
    ctx.state.write_artifact('diff.patch', patch)
    stop(ctx, 'implementation', 'budget exhausted: token budget')
    text = ctx.state.read_artifact('diff.patch')
    assert text.startswith('# AutoSE stopped at stage implementation') and text.endswith(patch)
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'x.py').write_text('a\n')
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    check = subprocess.run(['git', 'apply', '--check', str(ctx.state.artifact_path('diff.patch'))], cwd=repo)
    assert check.returncode == 0                                    # still a valid patch
    stop(ctx, 'preflight', 'baseline red')                          # no stage artifact for preflight


def test_the_review_evidence_gate():
    passed = CmdResult('python -m pytest -q', 0, '', 1.0)
    failed = CmdResult('python -m pytest -q', 1, '', 1.0)
    approve = {'verdict': 'approve', 'target_stage': 'none', 'findings': [],
               'issue_checklist': [{'requirement': 'r', 'status': 'met', 'evidence': 'e'}]}
    assert review_problems(dict(approve), [])                       # no tests run
    assert review_problems(dict(approve), [passed, failed])         # last run failed
    assert review_problems(dict(approve), [failed, passed]) == []
    unclear = dict(approve, issue_checklist=[{'requirement': 'r', 'status': 'unclear', 'evidence': 'e'}])
    assert review_problems(unclear, [passed])
    major = {'severity': 'major', 'category': 'regression', 'file': 'x', 'description': 'drops updates'}
    assert review_problems(dict(approve, findings=[major]), [passed])
    change = {'verdict': 'request_changes', 'target_stage': 'design', 'issue_checklist': [], 'findings': []}
    assert review_problems(change, [passed])                        # no blocking finding
    assert review_problems(dict(change, findings=[major]), []) == []
