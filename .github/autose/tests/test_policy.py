import inspect
import os

import pytest

import autose.github as github_module
from autose.config import Config
from autose.sandbox import Sandbox
from autose.schema import obj, string, validate
from autose.secretstore import Secrets
from autose.tools import PolicyError, Workspace, matches

HIDDEN = ['.git/**', '.github/autose/**']
PROTECTED = ['.github/**', '.git/**']


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'
    (root / '.github' / 'autose').mkdir(parents=True)
    (root / '.github' / 'autose' / 'secret_prompt.md').write_text('x')
    (root / 'tinydb').mkdir()
    (root / 'tinydb' / 'a.py').write_text('x = 1\n')
    (root / 'tests').mkdir()
    return root


def test_workspace_hides_the_harness_and_protects_paths(repo):
    ws = Workspace(repo, HIDDEN, PROTECTED, ['tinydb/**'])
    with pytest.raises(PolicyError):
        ws.resolve('.github/autose/secret_prompt.md')
    with pytest.raises(PolicyError):
        ws.resolve('../outside.txt')
    with pytest.raises(PolicyError):
        ws.resolve('.github/workflows/evil.yml', write=True)
    with pytest.raises(PolicyError):
        ws.resolve('tests/test_x.py', write=True)
    assert ws.resolve('tinydb/a.py', write=True)[1] == 'tinydb/a.py'


def test_symlinks_cannot_escape(repo):
    os.symlink('/etc', repo / 'tinydb' / 'etc')
    ws = Workspace(repo, HIDDEN, PROTECTED, ['tinydb/**'])
    with pytest.raises(PolicyError):
        ws.resolve('tinydb/etc/hostname')


def test_scratch_is_writable_but_nothing_else(repo, tmp_path):
    ws = Workspace(repo, HIDDEN, PROTECTED, [], scratch=tmp_path / 'scratch')
    path, rel = ws.resolve('scratch/probe.py', write=True)
    assert rel == 'scratch/probe.py' and str(path).startswith(str(tmp_path / 'scratch'))
    with pytest.raises(PolicyError):
        ws.resolve('tinydb/a.py', write=True)
    with pytest.raises(PolicyError):
        ws.resolve('scratch/../../repo/tinydb/a.py', write=True)


def test_glob_semantics():
    assert matches('tinydb/x/y.py', ['tinydb/**'])
    assert not matches('tinydbx/y.py', ['tinydb/**'])
    assert matches('tests/test_issue_3.py', ['tests/test_issue_3.py'])
    assert not matches('tests/test_issue_33.py', ['tests/test_issue_3.py'])


def test_sandbox_refuses_dangerous_commands_and_has_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-do-not-leak-123456')
    deny = Config.load().get('sandbox.deny_patterns')
    sandbox = Sandbox(tmp_path, deny_patterns=deny)
    for cmd in ['git push origin main', 'git commit -am x', 'curl https://example.com', 'gh pr merge 1',
                'cat /proc/1/environ', 'pip install requests', 'python -m pip install x',
                'cat .github/autose/prompts/reviewer.md', 'sudo ls', 'rm -rf /']:
        assert sandbox.run(cmd).denied, cmd
    assert sandbox.run('git status').denied is None
    out = sandbox.run('env').output
    assert 'sk-ant' not in out and 'ANTHROPIC' not in out


def test_sandbox_timeout_kills_the_process_group(tmp_path):
    result = Sandbox(tmp_path).run('sleep 30 & sleep 30', timeout=1)
    assert result.timed_out and result.duration < 20


def test_secrets_come_from_files_that_are_deleted(tmp_path, monkeypatch):
    folder = tmp_path / 'secrets'
    folder.mkdir()
    (folder / 'ANTHROPIC_API_KEY').write_text('sk-test-key-0000\n')
    (folder / 'AUTOSE_GITHUB_TOKEN').write_text('ghs_token_1234')
    monkeypatch.setenv('SOME_PASSWORD', 'x')
    secrets = Secrets.load(str(folder))
    assert secrets.get('ANTHROPIC_API_KEY') == 'sk-test-key-0000'
    assert secrets.github_token == 'ghs_token_1234'
    assert not folder.exists()
    assert 'SOME_PASSWORD' not in os.environ
    assert secrets.redact('key sk-test-key-0000 here') == 'key *** here'


def test_the_github_client_has_no_merge_operation():
    source = inspect.getsource(github_module)
    assert '/merge' not in source and 'def merge' not in source


def test_schema_validation_messages():
    schema = obj({'a': string(minLength=3), 'b': {'type': 'integer'}}, ['a'])
    assert validate({'a': 'abc', 'b': 1}, schema) == []
    errors = validate({'b': 'x', 'c': 1}, schema)
    assert any('a: is required' in e for e in errors)
    assert any('expected integer' in e for e in errors)
    assert any('unexpected property' in e for e in errors)


def test_every_variant_of_the_shipped_config_is_valid_apart_from_the_owner_placeholder():
    for name in Config.load().variants():
        problems = Config.load(variant=name).validate()
        assert all('OWNER placeholder' in p for p in problems), (name, problems)


def test_reviewer_uses_the_second_key_and_falls_back_to_the_first():
    from autose.llm import make_client
    cfg = Config.load()
    both = Secrets({'GEMINI_API_KEY': 'key-mine-0001', 'GEMINI_API_KEY_2': 'key-teammate-02'})
    assert make_client(cfg, 'reviewer', both).api_key == 'key-teammate-02'
    assert make_client(cfg, 'implementer', both).api_key == 'key-mine-0001'
    only_mine = Secrets({'GEMINI_API_KEY': 'key-mine-0001'})
    assert make_client(cfg, 'reviewer', only_mine).api_key == 'key-mine-0001'


def test_command_handler_resolves_scratch_paths(tmp_path):
    from autose.tools import command_handler
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    (scratch / 'hello.txt').write_text('hi from scratch\n')
    sandbox = Sandbox(tmp_path, extra_env={'SCRATCH': str(scratch)})
    cmd = command_handler(sandbox)
    res = cmd({'command': 'cat scratch/hello.txt'})
    assert 'hi from scratch' in res.content
    res2 = cmd({'command': 'cat ./scratch/hello.txt'})
    assert 'hi from scratch' in res2.content

