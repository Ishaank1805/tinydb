from autose.checks import check_design, check_spec, check_test_file, parse_json_object
from autose.gate import linked_issue, parse_command


def ac(i, then='the result is exactly [{"a": 2}]', verification='call db.search(Query().a == 2) and assert the result'):
    return {'id': f'AC-{i}', 'given': 'a table with two documents', 'when': 'searching', 'then': then,
            'verification': verification}


def test_spec_check_accepts_concrete_criteria_and_flags_vague_ones():
    assert check_spec({'acceptance_criteria': [ac(1), ac(2)]}).passed
    result = check_spec({'acceptance_criteria': [ac(1, then='printing is more helpful'), ac(1)]})
    assert not result.passed
    assert any('vague' in p for p in result.problems) and any('duplicate' in p for p in result.problems)
    assert not check_spec({'acceptance_criteria': []}).passed


def test_design_check_verifies_files_and_functions_against_the_code(tmp_path):
    (tmp_path / 'tinydb').mkdir()
    (tmp_path / 'tinydb' / 'table.py').write_text('class Table:\n    def update(self):\n        pass\n')
    spec = {'acceptance_criteria': [ac(1), ac(2)]}
    good = {'changes': [{'path': 'tinydb/table.py', 'new_file': False, 'description': 'change update',
                         'symbols': [{'name': 'Table.update', 'new': False}, {'name': 'Table.rename', 'new': True}]}],
            'tests': [{'name': 'test_ac1_x', 'covers': ['AC-1'], 'description': 'checks x'},
                      {'name': 'test_ac2_y', 'covers': ['AC-2'], 'description': 'checks y'}]}
    args = (spec, tmp_path, ['tinydb/**', 'docs/**'], ['.github/**'], 'tests/test_issue_1.py', 'tests')
    assert check_design(good, *args).passed, check_design(good, *args).problems
    bad = {'changes': [{'path': 'tinydb/table.py', 'new_file': False, 'description': 'x' * 12,
                        'symbols': [{'name': 'Table.nope', 'new': False}, {'name': 'Ghost.new_one', 'new': True}]},
                       {'path': '.github/workflows/ci.yml', 'new_file': False, 'symbols': [], 'description': 'x' * 12},
                       {'path': 'tests/test_issue_1.py', 'new_file': True, 'symbols': [], 'description': 'x' * 12}],
           'tests': [{'name': 'test_ac1_x', 'covers': ['AC-1'], 'description': 'checks x'}]}
    problems = ' | '.join(check_design(bad, *args).problems)
    for expected in ('Table.nope does not exist', 'existing class Ghost', 'protected path', 'tests are planned',
                     'no planned test for AC-2'):
        assert expected in problems


def test_test_file_check_maps_tests_to_criteria(tmp_path):
    path = tmp_path / 'test_issue_1.py'
    path.write_text('def test_ac1_a():\n    pass\n\ndef test_ac10_b():\n    pass\n')
    result = check_test_file(path, {'acceptance_criteria': [ac(1), ac(2)]})
    assert not result.passed and 'AC-2' in result.problems[0]
    assert any('AC-10' in n for n in result.notes)


def test_json_extraction_from_judge_replies():
    assert parse_json_object('```json\n{"criteria": []}\n```') == {'criteria': []}
    assert parse_json_object('Sure! {"a": 1} hope that helps') == {'a': 1}
    assert parse_json_object('no json') is None


def test_command_parsing():
    assert parse_command('/autose')['sub'] == ''
    cmd = parse_command('/autose review #12 variant=single-agent\nplease focus on JSON storage')
    assert (cmd['sub'], cmd['issue'], cmd['variant']) == ('review', 12, 'single-agent')
    assert cmd['notes'] == 'please focus on JSON storage'
    assert parse_command('please /autose') is None and parse_command('/autosex') is None
    assert parse_command('/autose bogus')['unknown'] == ['bogus']


def test_linked_issue_resolution():
    assert linked_issue({'head': {'ref': 'autose/issue-7-single-agent'}}, None, 'autose/issue-') == 7
    assert linked_issue({'head': {'ref': 'seeded/s1'}, 'body': 'Fixes #9'}, None, 'autose/issue-') == 9
    assert linked_issue({'head': {'ref': 'autose/issue-7'}}, 3, 'autose/issue-') == 3
    assert linked_issue({'head': {'ref': 'x'}, 'body': ''}, None, 'autose/issue-') is None
