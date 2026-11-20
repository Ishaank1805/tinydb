from tinydb import where
from tinydb.operations import append, delete, increment, decrement, add, subtract, set


def test_delete(db):
    db.update(delete('int'), where('char') == 'a')
    assert 'int' not in db.get(where('char') == 'a')


def test_add_int(db):
    db.update(add('int', 5), where('char') == 'a')
    assert db.get(where('char') == 'a')['int'] == 6


def test_add_str(db):
    db.update(add('char', 'xyz'), where('char') == 'a')
    assert db.get(where('char') == 'axyz')['int'] == 1


def test_subtract(db):
    db.update(subtract('int', 5), where('char') == 'a')
    assert db.get(where('char') == 'a')['int'] == -4


def test_set(db):
    db.update(set('char', 'xyz'), where('char') == 'a')
    assert db.get(where('char') == 'xyz')['int'] == 1


def test_increment(db):
    db.update(increment('int'), where('char') == 'a')
    assert db.get(where('char') == 'a')['int'] == 2


def test_decrement(db):
    db.update(decrement('int'), where('char') == 'a')
    assert db.get(where('char') == 'a')['int'] == 0


def test_append_to_existing_list(db):
    db.update({'tags': ['a']}, where('char') == 'a')
    db.update(append('tags', 'b'), where('char') == 'a')
    assert db.get(where('char') == 'a')['tags'] == ['a', 'b']


def test_append_creates_missing_field(db):
    db.update(append('tags', 'x'), where('char') == 'a')
    assert db.get(where('char') == 'a')['tags'] == ['x']


def test_append_turns_a_single_value_into_a_list(db):
    db.update({'tags': 'a'}, where('char') == 'a')
    db.update(append('tags', 'b'), where('char') == 'a')
    assert db.get(where('char') == 'a')['tags'] == ['a', 'b']
