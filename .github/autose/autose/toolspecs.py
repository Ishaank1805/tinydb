"""Names, descriptions and JSON schemas of every tool an agent can be given.

Which tools a given agent actually receives (and what their handlers are
allowed to touch) is decided per stage in ``autose/stages``; this file only
describes them. Keeping all schemas in one place also lets the single-agent
variant declare one stable tool list for its whole session.
"""
from __future__ import annotations

from .schema import array, boolean, integer, obj, string

AC = obj({
    'id': string('AC-1, AC-2, ...'),
    'given': string('precondition with concrete data', minLength=3),
    'when': string('the action, naming the public API call', minLength=3),
    'then': string('the concrete, observable outcome', minLength=3),
    'verification': string('how an automated pytest test checks it: which call to make and what to '
                           'assert', minLength=15),
}, ['id', 'given', 'when', 'then', 'verification'])

SYMBOL = obj({'name': string('function, Class or Class.method'),
              'new': boolean('true if it does not exist yet')}, ['name', 'new'])

CHANGE = obj({'path': string('repository-relative path'), 'new_file': boolean(),
              'symbols': array(SYMBOL), 'description': string(minLength=10)},
             ['path', 'new_file', 'symbols', 'description'])

PLANNED_TEST = obj({'name': string('pytest function name, test_ac<N>_<what_it_checks>'),
                    'covers': array(string('AC id'), minItems=1),
                    'description': string(minLength=10)}, ['name', 'covers', 'description'])

CHECK_ITEM = obj({'requirement': string('a requirement you derived from the issue', minLength=5),
                  'status': string(enum=['met', 'not_met', 'unclear']),
                  'evidence': string('what you ran or read that shows it', minLength=5)},
                 ['requirement', 'status', 'evidence'])

FINDING = obj({'severity': string(enum=['blocker', 'major', 'minor', 'nit']),
               'category': string(enum=['correctness', 'regression', 'tests', 'scope', 'design',
                                        'security', 'docs', 'style']),
               'file': string(), 'line': integer('0 if not applicable'),
               'description': string(minLength=10), 'suggestion': string()},
              ['severity', 'category', 'file', 'description'])

SPECS: dict[str, tuple[str, dict]] = {
    'list_files': (
        'List files and directories inside the repository (recursively, up to max_depth).',
        obj({'path': string('directory relative to the repository root, default "."'),
             'max_depth': integer('1-6, default 2')})),
    'read_file': (
        'Read a text file with line numbers. Long files: use start_line/end_line (max 600 lines per call).',
        obj({'path': string(), 'start_line': integer(), 'end_line': integer()}, ['path'])),
    'search_code': (
        'Search file contents with a Python regular expression; returns path:line: text matches.',
        obj({'pattern': string(), 'path': string('directory or file, default "."'),
             'glob': string('file name filter such as "*.py"'), 'max_results': integer()}, ['pattern'])),
    'write_file': (
        'Create or overwrite a file. Only paths you are allowed to change can be written; the harness '
        'refuses everything else.',
        obj({'path': string(), 'content': string()}, ['path', 'content'])),
    'edit_file': (
        'Replace an exact snippet in a file. old_text must occur exactly once unless replace_all is true. '
        'Prefer this to write_file for small changes.',
        obj({'path': string(), 'old_text': string(), 'new_text': string(), 'replace_all': boolean()},
            ['path', 'old_text', 'new_text'])),
    'run_command': (
        'Run a bash command in the repository root with the project virtualenv on PATH, for example '
        '"python -m pytest -q tests/test_tables.py" or a short "python -c ..." experiment. There are no '
        'credentials, git history operations are refused, and commands are stopped after 300 s.',
        obj({'command': string(), 'timeout_seconds': integer()}, ['command'])),
    'read_artifact': (
        'Read an earlier pipeline artifact by name (for example spec.md or design.md).',
        obj({'name': string()}, ['name'])),
    'read_skill_file': (
        'Read a file that belongs to the spec-writing skill (SKILL.md may refer to such files).',
        obj({'path': string()}, ['path'])),
    'get_diff': (
        'Read the pull request diff, paged by line numbers (max 800 lines per call).',
        obj({'start_line': integer(), 'end_line': integer()})),
    'submit_spec': (
        'Submit the specification. The harness checks that every acceptance criterion is testable '
        '(structure + an independent judge); if not, it tells you what to fix.',
        obj({'title': string(minLength=5), 'summary': string(minLength=20),
             'kind': string(enum=['bug', 'feature', 'other']),
             'problem': string('the user-visible problem or need, in your own words', minLength=20),
             'scope_in': array(string()), 'scope_out': array(string()),
             'assumptions': array(string('assumptions you made because you cannot ask the reporter')),
             'open_questions': array(string()),
             'acceptance_criteria': array(AC, minItems=1, maxItems=12),
             'reproduction': string('bugs: what you ran and what it printed')},
            ['title', 'summary', 'kind', 'problem', 'assumptions', 'acceptance_criteria'])),
    'cannot_specify': (
        'Stop the run because the issue cannot be turned into testable acceptance criteria, even with '
        'explicit assumptions. The questions are posted for the maintainer.',
        obj({'reason': string(minLength=20), 'questions': array(string(), minItems=1)},
            ['reason', 'questions'])),
    'submit_design': (
        'Submit the change plan. The harness checks that every named file and function exists (or is '
        'marked new) and that every acceptance criterion has a planned test.',
        obj({'summary': string(minLength=20), 'approach': string(minLength=40),
             'changes': array(CHANGE, minItems=1), 'tests': array(PLANNED_TEST, minItems=1),
             'risks': array(string()), 'alternatives_considered': array(string()),
             'architecture_notes': string('what in ARCHITECTURE.md this relies on or affects'),
             'changes_since_previous': string('only when revising an earlier design')},
            ['summary', 'approach', 'changes', 'tests', 'risks'])),
    'cannot_design': (
        'Stop the run because no change fitting this repository can satisfy the spec. Explain why.',
        obj({'reason': string(minLength=20)}, ['reason'])),
    'finish_implementation': (
        'Call when the code change is complete. The harness re-checks which files changed and runs the '
        'build check; if it fails you get the output and can continue.',
        obj({'summary': string('what you changed and why', minLength=10)}, ['summary'])),
    'request_redesign': (
        'Send the work back to the design stage because the design is wrong or infeasible. Say exactly '
        'what is wrong.',
        obj({'reason': string(minLength=20)}, ['reason'])),
    'dispute_test': (
        'Claim that an acceptance test contradicts the spec. The test author re-checks it (at most once).',
        obj({'test_name': string(), 'reason': string(minLength=20)}, ['test_name', 'reason'])),
    'submit_tests': (
        'Submit the acceptance tests. The harness checks naming, AC coverage and that pytest can collect '
        'the file.',
        obj({'mapping': array(obj({'ac_id': string(), 'tests': array(string(), minItems=1)},
                                  ['ac_id', 'tests']), minItems=1),
             'notes': string()}, ['mapping'])),
    'submit_review': (
        'Submit your review. Approval is only accepted after you ran the test suite yourself and marked '
        'every requirement you derived from the issue as met.',
        obj({'verdict': string(enum=['approve', 'request_changes']),
             'target_stage': string('where the work must go back to; "none" when approving',
                                    enum=['none', 'requirements', 'design', 'implementation']),
             'summary': string(minLength=20),
             'issue_checklist': array(CHECK_ITEM, minItems=1),
             'findings': array(FINDING),
             'probes': string('what you ran yourself beyond the existing tests, and what happened',
                              minLength=10)},
            ['verdict', 'target_stage', 'summary', 'issue_checklist', 'findings', 'probes'])),
}

# One stable tool list for the single-agent (shared context) variant.
SHARED_TOOLS = ['list_files', 'read_file', 'search_code', 'write_file', 'edit_file', 'run_command',
                'read_artifact', 'read_skill_file', 'submit_spec', 'cannot_specify', 'submit_design',
                'cannot_design', 'finish_implementation', 'request_redesign', 'dispute_test',
                'submit_tests']
