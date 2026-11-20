import subprocess

from autose import difffacts

BASE_SRC = 'def total(items):\n    return sum(items)\n\n\nclass Box:\n    def put(self, x):\n        return [x]\n'
HEAD_SRC = ('def total(items):\n    return sum(items) + 0\n\n\nclass Box:\n    def put(self, x):\n        return [x]\n\n'
            '    def take(self):\n        return None\n')
TEST_BASE = 'from pkg.core import total\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n    assert total([]) == 0\n'
TEST_HEAD = 'from pkg.core import total\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n'


def make_repo(tmp_path):
    repo = tmp_path / 'repo'
    (repo / 'pkg').mkdir(parents=True)
    (repo / 'tests').mkdir()
    (repo / 'pkg' / '__init__.py').write_text('')
    (repo / 'pkg' / 'core.py').write_text(BASE_SRC)
    (repo / 'tests' / 'test_core.py').write_text(TEST_BASE)
    run = lambda *a: subprocess.run(['git', *a], cwd=repo, check=True, capture_output=True)  # noqa: E731
    run('init', '-q')
    run('add', '-A')
    run('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'base')
    (repo / 'pkg' / 'core.py').write_text(HEAD_SRC)
    (repo / 'tests' / 'test_core.py').write_text(TEST_HEAD)
    (repo / 'tests' / 'test_new.py').write_text('def test_new():\n    assert True\n')
    run('add', '-N', 'tests/test_new.py')
    diff = subprocess.run(['git', 'diff'], cwd=repo, check=True, capture_output=True, text=True).stdout
    path = tmp_path / 'diff.patch'
    path.write_text(diff)
    return repo, path, diff


def test_base_tree_is_rebuilt_from_the_diff_and_facts_are_computed(tmp_path):
    repo, diff_path, diff = make_repo(tmp_path)
    base = tmp_path / 'scratch' / 'base'
    base.parent.mkdir()
    assert difffacts.build_base_tree(repo, diff_path, base) is None
    assert (base / 'pkg' / 'core.py').read_text() == BASE_SRC
    assert (base / 'tests' / 'test_core.py').read_text() == TEST_BASE
    assert not (base / 'tests' / 'test_new.py').exists() and not (base / '.git').exists()
    facts = difffacts.compute(diff, repo, base, 'The total is wrong for empty lists', ['pkg/**'])
    changes = {(f['path'], f['name'], f['change']) for f in facts['functions']}
    assert ('pkg/core.py', 'total', 'changed') in changes and ('pkg/core.py', 'Box.take', 'added') in changes
    assert ('pkg/core.py', 'Box.put', 'changed') not in changes
    edited = facts['edited_tests']
    assert edited == [{'path': 'tests/test_core.py', 'tests': ['test_total'], 'assertion_lines_removed_or_changed': 1}]
    assert facts['files']['tests'] == ['tests/test_core.py', 'tests/test_new.py']
    text = difffacts.markdown(facts, 'rebuilt')
    assert 'Existing test file edited: `tests/test_core.py`' in text and '`total`' in text


def test_a_diff_that_does_not_apply_leaves_no_base_tree(tmp_path):
    repo, diff_path, _ = make_repo(tmp_path)
    diff_path.write_text('diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n')
    error = difffacts.build_base_tree(repo, diff_path, tmp_path / 'base')
    assert error and not (tmp_path / 'base').exists()


def test_compare_script_runs_a_probe_on_both_versions(tmp_path):
    repo, diff_path, _ = make_repo(tmp_path)
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    base = scratch / 'base'
    assert difffacts.build_base_tree(repo, diff_path, base) is None
    script = difffacts.write_compare_script(scratch)
    (scratch / 'probe.py').write_text('from pkg.core import Box\nprint(hasattr(Box, "take"))\n')
    env = {'PATH': '/usr/bin:/bin', 'SCRATCH': str(scratch), 'REPO': str(repo), 'BASE': str(base)}
    for invocation in [['probe.py'], ['scratch/probe.py'], ['python', 'scratch/probe.py'], ['python', 'probe.py']]:
        out = subprocess.run(['bash', str(script), *invocation], env=env, capture_output=True, text=True).stdout
        head, before = out.split('=== before the change ===')
        assert 'True' in head and 'False' in before
