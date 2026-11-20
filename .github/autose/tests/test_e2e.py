"""End-to-end: the real orchestrator, stages, checks, sandbox and git operations
run against a tinydb checkout. Only the model and GitHub are replaced: a
scripted "model" plays every role and a fake GitHub records PR/CI calls.

The scenario is experiment issue 2 (update_multiple and generators). The
first implementation attempt is deliberately wrong, so the run must loop from
testing back to implementation before it releases. The implementer also tries
to write outside its allowed files, which the harness must refuse or revert.

Set AUTOSE_E2E_REPO to a local tinydb clone to avoid cloning from GitHub.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from autose.config import Config
from autose.context import build_ctx
from autose.llm import ScriptedClient
from autose.orchestrator import run_job
from autose.report import run_report
from autose.review import review as run_review
from autose.secretstore import Secrets

PINNED = 'edff96a'
ISSUE = {'number': 2, 'title': 'update_multiple() only updates the first record when given a generator',
         'body': 'Passing a generator to update_multiple only updates the first document.', 'author': 'user',
         'sanitize_note': 'nothing removed', 'trigger': {'event': 'issues', 'actor': 'maintainer', 'comment': ''}}
TEST_FILE = '''from tinydb import TinyDB, where
from tinydb.storages import MemoryStorage


def _db():
    db = TinyDB(storage=MemoryStorage)
    db.insert_multiple({'sku': f'A{i}', 'price': i} for i in range(1, 4))
    return db


def test_ac1_generator_updates_every_document():
    db = _db()
    db.update_multiple(({'price': 0}, where('sku') == s) for s in ['A1', 'A2', 'A3'])
    assert [d['price'] for d in db.all()] == [0, 0, 0]


def test_ac2_generator_returns_all_ids():
    db = _db()
    assert sorted(db.update_multiple(({'price': 0}, where('sku') == s) for s in ['A2', 'A3'])) == [2, 3]


def test_ac3_list_input_still_works():
    db = _db()
    db.update_multiple([({'price': 9}, where('sku') == 'A1')])
    assert db.get(where('sku') == 'A1')['price'] == 9
'''
SPEC = {'title': 'update_multiple accepts generators', 'kind': 'bug',
        'summary': 'update_multiple must apply every update when it is given a generator.',
        'problem': 'A generator passed to update_multiple is exhausted after the first document.',
        'assumptions': ['any iterable of (fields, cond) pairs is accepted'], 'open_questions': [],
        'scope_in': ['Table.update_multiple'], 'scope_out': ['other update APIs'],
        'reproduction': 'python -c ... printed [1]',
        'acceptance_criteria': [
            {'id': 'AC-1', 'given': 'three documents A1-A3', 'when': 'calling db.update_multiple(generator)',
             'then': 'all three documents have price 0',
             'verification': 'assert [d["price"] for d in db.all()] == [0, 0, 0]'},
            {'id': 'AC-2', 'given': 'three documents A1-A3', 'when': 'update_multiple(generator) for A2 and A3',
             'then': 'the returned ids are 2 and 3', 'verification': 'assert sorted(result) == [2, 3]'},
            {'id': 'AC-3', 'given': 'a list of updates', 'when': 'calling db.update_multiple([...])',
             'then': 'the A1 document has price 9', 'verification': 'assert db.get(where("sku") == "A1")["price"] == 9'}]}
DESIGN = {'summary': 'Materialise the updates iterable once in Table.update_multiple.',
          'approach': 'Convert updates to a list before the per-document loop so a generator is not exhausted.',
          'changes': [{'path': 'tinydb/table.py', 'new_file': False, 'description': 'materialise updates',
                       'symbols': [{'name': 'Table.update_multiple', 'new': False}]}],
          'tests': [{'name': 'test_ac1_generator_updates_every_document', 'covers': ['AC-1'], 'description': 'every document is updated'},
                    {'name': 'test_ac2_generator_returns_all_ids', 'covers': ['AC-2'], 'description': 'all updated ids are returned'},
                    {'name': 'test_ac3_list_input_still_works', 'covers': ['AC-3'], 'description': 'list input keeps working'}],
          'risks': ['memory for huge iterables'], 'alternatives_considered': ['itertools.tee'],
          'architecture_notes': 'Table owns document updates.'}
ANCHOR = '        # Perform the update operation for documents specified by a query\n\n        # Collect affected doc_ids\n'
WRONG = '                for fields, cond in updates:\n'
PROBE = ("from tinydb import TinyDB, where\nfrom tinydb.storages import MemoryStorage\n"
         "db = TinyDB(storage=MemoryStorage)\ndb.insert_multiple([{'a': 1}, {'a': 2}])\n"
         "print('updated ids', sorted(db.update_multiple((({'a': 9}, where('a') == i) for i in (1, 2)))))\n")

REVIEW = {'verdict': 'approve', 'target_stage': 'none', 'summary': 'The generator is materialised once; behaviour '
          'for lists is unchanged.', 'findings': [], 'probes': 'ran a generator probe in scratch',
          'issue_checklist': [{'requirement': 'generator input updates every matching document', 'status': 'met',
                               'evidence': 'full suite and a probe pass'}]}


def role_of(system, messages):
    task = messages[0]['content']
    text = system
    if 'Role: one engineer' in system:          # shared-context variant: role is in the stage message
        task = text = next(m['content'] for m in reversed(messages) if m['role'] == 'user'
                           and isinstance(m.get('content'), str) and m['content'].startswith('# Stage:'))
    for marker, role in [('Role: requirements analyst', 'req'), ('Role: software designer', 'design'),
                         ('Role: implementer', 'impl'), ('Role: test author', 'tests'),
                         ('Role: independent code reviewer', 'review'), ('check acceptance criteria', 'judge')]:
        if marker in text:
            return role, task
    raise AssertionError('unknown role')


def responder(system, messages, tools):
    role, task = role_of(system, messages)
    start = 0
    for i, m in enumerate(messages):
        if m['role'] == 'user' and isinstance(m.get('content'), str) and (
                m['content'].startswith('# Stage:') or i == 0):
            start = i
    step = sum(1 for m in messages[start:] if m['role'] == 'assistant')
    if role == 'judge':
        return {'text': json.dumps({'criteria': [{'id': f'AC-{i}', 'testable': True, 'reason': 'concrete'}
                                                 for i in (1, 2, 3)]})}
    plans = {
        'req': [[('run_command', {'command': 'python -c "print(1)"'})], [('submit_spec', SPEC)]],
        'design': [[('read_file', {'path': 'tinydb/table.py', 'start_line': 520, 'end_line': 600})],
                   [('submit_design', DESIGN)]],
        'tests': [[('write_file', {'path': 'tests/test_issue_2.py', 'content': TEST_FILE})],
                  [('run_command', {'command': 'python -m pytest -q -p no:cacheprovider -o addopts= tests/test_issue_2.py'})],
                  [('submit_tests', {'mapping': [{'ac_id': f'AC-{i}', 'tests': ['x']} for i in (1, 2, 3)]})]],
        'review': [[('submit_review', REVIEW)],
                   [('run_command', {'command': 'python -m pytest -q -p no:cacheprovider -o addopts='})],
                   [('write_file', {'path': 'scratch/probe.py', 'content': PROBE})],
                   [('run_command', {'command': 'bash $SCRATCH/compare.sh probe.py'})],
                   [('get_diff', {})], [('submit_review', REVIEW)]],
    }
    if role == 'impl':
        if 'implementation attempt 1)' in task:
            plans['impl'] = [
                [('run_command', {'command': 'echo stray > stray.txt'})],
                [('write_file', {'path': 'tests/test_issue_2.py', 'content': 'tampered'})],
                [('edit_file', {'path': 'tinydb/table.py', 'old_text': WRONG,
                                'new_text': '                # generators: TODO\n' + WRONG})],
                [('finish_implementation', {'summary': 'first attempt (deliberately wrong)'})]]
        else:
            plans['impl'] = [[('edit_file', {'path': 'tinydb/table.py', 'old_text': ANCHOR,
                                             'new_text': '        updates = list(updates)\n\n' + ANCHOR})],
                             [('finish_implementation', {'summary': 'materialise updates with list()'})]]
    plan = plans[role]
    return {'tool_calls': plan[min(step, len(plan) - 1)]}


class FakeGitHub:
    def __init__(self):
        self.calls, self.comments, self.cid = [], {1: ''}, None

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {}
        return record

    def get_repo(self):
        return {'default_branch': 'main'}

    def find_pr(self, branch, state='open'):
        return None

    def create_pr(self, head, base, title, body):
        self.calls.append(('create_pr', (head, base, title), {}))
        return {'number': 101, 'html_url': 'https://example/pr/101', 'state': 'open'}

    def update_pr(self, number, **fields):
        self.calls.append(('update_pr', (number,), fields))
        return {'number': number, 'html_url': 'https://example/pr/101', 'state': 'open'}

    def dispatch(self, workflow, ref, inputs):
        self.cid = inputs['correlation_id']
        self.calls.append(('dispatch', (workflow, ref), inputs))

    def workflow_runs(self, workflow, **params):
        return [{'id': 7, 'display_title': f'CI · {self.cid}', 'status': 'completed', 'conclusion': 'success',
                 'html_url': 'https://example/ci/7'}]

    def get_comment(self, comment_id):
        return {'body': self.comments.get(int(comment_id), '')}

    def update_comment(self, comment_id, body):
        self.comments[int(comment_id)] = body


@pytest.fixture(scope='module')
def source_repo(tmp_path_factory):
    local = os.environ.get('AUTOSE_E2E_REPO')
    dest = tmp_path_factory.mktemp('src') / 'tinydb'
    try:
        subprocess.run(['git', 'clone', '-q', local or 'https://github.com/msiemens/tinydb.git', str(dest)],
                       check=True, capture_output=True, timeout=180)
        subprocess.run(['git', 'checkout', '-q', PINNED], cwd=dest, check=True, capture_output=True)
    except (subprocess.SubprocessError, OSError) as exc:
        pytest.skip(f'tinydb checkout not available: {exc}')
    return dest


@pytest.fixture(scope='module')
def venv(tmp_path_factory):
    # A target venv that can import the harness interpreter's pytest without
    # network access: a .pth file points at this interpreter's site-packages.
    import site
    path = tmp_path_factory.mktemp('venv') / 'v'
    subprocess.run([sys.executable, '-m', 'venv', str(path)], check=True)
    target_site = next(path.glob('lib/python*/site-packages'))
    (target_site / 'autose_e2e.pth').write_text('\n'.join(site.getsitepackages()) + '\n')
    return path


@pytest.mark.parametrize('variant', ['baseline', 'single-agent'])
def test_issue_to_reviewed_pr(tmp_path, monkeypatch, source_repo, venv, variant):
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'clone', '-q', str(source_repo), str(repo)], check=True)
    subprocess.run(['git', 'checkout', '-q', PINNED], cwd=repo, check=True)
    for key in [k for k in os.environ if k.startswith(('AUTOSE_', 'GITHUB_'))]:
        monkeypatch.delenv(key)
    branch = 'autose/issue-2' + ('' if variant == 'baseline' else f'-{variant}')
    monkeypatch.setenv('AUTOSE_ISSUE', '2')
    monkeypatch.setenv('AUTOSE_BRANCH', branch)
    monkeypatch.setenv('AUTOSE_TARGET_VENV', str(venv))
    monkeypatch.setenv('AUTOSE_SCRATCH_DIR', str(tmp_path / 'scratch'))
    monkeypatch.setenv('AUTOSE_STATUS_COMMENT_ID', '1')
    monkeypatch.setenv('AUTOSE_CI_POLL_SECONDS', '0')
    inbox = tmp_path / 'in'
    (inbox / 'issue').mkdir(parents=True)
    (inbox / 'issue' / 'issue.json').write_text(json.dumps(ISSUE))
    monkeypatch.setenv('AUTOSE_INPUT_DIR', str(inbox))

    cfg = Config.load(variant=variant)
    cfg.data['repository']['allowed'] = ['local/repo']
    client = ScriptedClient(responder)
    gh = FakeGitHub()
    ctx = build_ctx('build-r0', cfg, Secrets({}), repo_dir=repo, state_dir=tmp_path / 'state', input_dir=inbox,
                    gh=gh, make_llm=lambda role: client)
    assert run_job(ctx) == 0
    data = ctx.state.data
    assert data['status'] == 'released', data.get('stop_reason')
    assert data['ci_runs'] == [{'round': 0, 'url': 'https://example/ci/7', 'conclusion': 'success'}]
    arts = tmp_path / 'state' / 'artifacts'
    for name in ('spec.md', 'design.md', 'diff.patch', 'test_report.md', 'pr.md'):
        assert (arts / name).exists(), name
    assert 'updates = list(updates)' in (arts / 'diff.patch').read_text()
    report = (arts / 'test_report.md').read_text()
    assert 'all new and existing tests pass' in report and 'Fail-to-pass' in report
    assert list((tmp_path / 'state' / 'history').glob('test_report.*'))      # attempt 1 failed and was kept
    assert not (repo / 'stray.txt').exists()                                 # reverted by the policy
    assert (repo / 'tests' / 'test_issue_2.py').read_text() == TEST_FILE     # implementer could not tamper
    log = subprocess.run(['git', 'log', '--format=%s', f'{PINNED}..HEAD'], cwd=repo, capture_output=True,
                         text=True).stdout.splitlines()
    assert len(log) == 2 and log[1].startswith('autose(#2)')
    pushed = subprocess.run(['git', 'branch', '--list', branch], cwd=source_repo, capture_output=True,
                            text=True).stdout
    assert branch in pushed
    names = [c[0] for c in gh.calls]
    assert 'create_pr' in names and 'dispatch' in names and 'merge' not in ' '.join(names)
    assert '<!-- autose:section build-r0 -->' in gh.comments[1]
    roles = {r['role'] for r in ctx.ledger.records}
    expected = {'generalist', 'judge'} if variant == 'single-agent' else {'planner', 'implementer', 'test_author', 'judge'}
    assert expected <= roles

    # --- stage 6 in its own "job": issue + diff + repo only -------------------
    review_in = tmp_path / 'review-in'
    review_in.mkdir()
    diff = subprocess.run(['git', 'diff', PINNED, 'HEAD'], cwd=repo, capture_output=True, text=True).stdout
    (review_in / 'diff.patch').write_text(diff)
    (review_in / 'pr.json').write_text(json.dumps({'number': 101, 'head_sha': data['head_sha'], 'title': 'AutoSE #2',
                                                   'changed_files': 2, 'additions': 20, 'deletions': 0}))
    monkeypatch.setenv('AUTOSE_REVIEW_DIR', str(review_in))
    monkeypatch.setenv('AUTOSE_STATE_DIR', str(tmp_path / 'review-state'))
    monkeypatch.setenv('AUTOSE_REPO_DIR', str(repo))
    monkeypatch.setenv('AUTOSE_ROUND', '1')
    assert run_review(cfg, Secrets({}), make_llm=lambda role: client) == 0
    verdict = json.loads((tmp_path / 'review-state' / 'review.json').read_text())
    assert verdict['verdict'] == 'approve' and verdict['facts']['reviewer_tests_passed']
    review_md = (tmp_path / 'review-state' / 'artifacts' / 'review.md').read_text()
    assert 'Evidence recorded by the harness' in review_md
    transcripts = ''.join(p.read_text() for p in (tmp_path / 'review-state' / 'transcripts').glob('*'))
    assert 'run the test suite yourself before approving' in transcripts     # evidence gate fired first
    assert 'run at least one probe of your own' in transcripts               # reviewer v2: probe required
    # the pre-change tree rebuilt from the diff really is the old code: the bug shows only there
    head_out, base_out = transcripts.split('=== before the change ===')[:2]
    assert 'updated ids [1, 2]' in head_out and 'updated ids [1]' in base_out
    assert {'path': 'tinydb/table.py', 'name': 'Table.update_multiple', 'change': 'changed'} in verdict['diff_facts']['functions']
    assert verdict['diff_facts']['edited_tests'] == [] and 'Facts the harness gave the reviewer' in review_md

    # --- final report job --------------------------------------------------------
    artifacts = tmp_path / 'artifacts'
    shutil.copytree(tmp_path / 'state', artifacts / 'autose-state-r0')
    shutil.copytree(tmp_path / 'review-state', artifacts / 'autose-review-r1')
    needs = {'gate': {'result': 'success', 'outputs': {'mode': 'full', 'issue': '2', 'variant': variant}},
             'build': {'result': 'success', 'outputs': {'status': 'released', 'pr': '101'}},
             'review_1': {'result': 'success', 'outputs': {'verdict': 'approve'}}}
    monkeypatch.setenv('AUTOSE_NEEDS_JSON', json.dumps(needs))
    monkeypatch.setenv('AUTOSE_ARTIFACTS_DIR', str(artifacts))
    monkeypatch.setenv('AUTOSE_STATE_DIR', str(tmp_path / 'summary'))
    assert run_report(cfg, Secrets({})) == 0
    summary = json.loads((tmp_path / 'summary' / 'run_summary.json').read_text())
    assert summary['outcome'] == 'approved' and summary['tokens']['total'] > 0
    assert summary['ci_runs'][0]['url'] == 'https://example/ci/7'
    sequence = (tmp_path / 'summary' / 'sequence.mmd').read_text()
    assert sequence.startswith('sequenceDiagram') and 'R-->>H' in sequence
