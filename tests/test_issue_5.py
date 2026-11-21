import pytest
from tinydb import where
from tinydb.operations import append


def test_ac1_append_missing_field(db):
    db.insert({'title': 'Fix login'})
    db.update(append('tags', 'urgent'), where('title') == 'Fix login')
    assert db.get(where('title') == 'Fix login')['tags'] == ['urgent']


def test_ac2_append_existing_list(db):
    db.insert({'title': 'Fix login', 'tags': ['backend']})
    db.update(append('tags', 'urgent'), where('title') == 'Fix login')
    assert db.get(where('title') == 'Fix login')['tags'] == ['backend', 'urgent']


def test_ac3_append_non_list_raises_type_error(db):
    db.insert({'title': 'Fix login', 'tags': 'backend'})
    with pytest.raises(TypeError):
        db.update(append('tags', 'urgent'), where('title') == 'Fix login')
