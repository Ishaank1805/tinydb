import pytest
from tinydb import TinyDB


def test_ac1_rename_table_preserves_documents_and_ids(db: TinyDB):
    db.drop_tables()
    table = db.table('users')
    doc_id = table.insert({'name': 'Alice'})

    db.rename_table('users', 'customers')

    assert 'users' not in db.tables()
    assert 'customers' in db.tables()

    docs = db.table('customers').all()
    assert len(docs) == 1
    assert docs[0] == {'name': 'Alice'}
    assert docs[0].doc_id == doc_id


def test_ac2_rename_table_refuses_existing_target(db: TinyDB):
    db.drop_tables()
    db.table('users').insert({'name': 'Alice'})
    db.table('customers').insert({'name': 'Bob'})

    with pytest.raises(ValueError):
        db.rename_table('users', 'customers')

    assert 'users' in db.tables()
    assert 'customers' in db.tables()
    assert db.table('users').all()[0]['name'] == 'Alice'
    assert db.table('customers').all()[0]['name'] == 'Bob'


def test_ac3_rename_table_updates_table_cache_and_stale_results(db: TinyDB):
    db.drop_tables()
    users_table = db.table('users')
    doc_id = users_table.insert({'name': 'Alice'})

    db.rename_table('users', 'customers')

    assert users_table.all()[0] == {'name': 'Alice'}
    assert users_table.all()[0].doc_id == doc_id
    assert db.table('customers').all()[0] == {'name': 'Alice'}


def test_ac4_rename_table_raises_key_error_on_missing_source(db: TinyDB):
    db.drop_tables()
    db.table('customers').insert({'name': 'Bob'})

    with pytest.raises(KeyError):
        db.rename_table('nonexistent', 'customers')

    assert 'nonexistent' not in db.tables()
    assert 'customers' in db.tables()
