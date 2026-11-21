import pytest
from tinydb import TinyDB
from tinydb.storages import MemoryStorage

def test_ac1_rename_table_success():
    db = TinyDB(storage=MemoryStorage)
    users = db.table('users')
    doc_id = users.insert({'name': 'Alice'})
    db.rename_table('users', 'customers')
    assert 'users' not in db.tables()
    assert 'customers' in db.tables()
    assert db.table('customers').get(doc_id=doc_id) == {'name': 'Alice'}

def test_ac2_rename_table_target_exists():
    db = TinyDB(storage=MemoryStorage)
    db.table('users').insert({'a': 1})
    db.table('customers').insert({'b': 2})
    with pytest.raises(ValueError):
        db.rename_table('users', 'customers')
    assert 'users' in db.tables() and 'customers' in db.tables()
    assert db.table('users').all() == [{'a': 1}]
    assert db.table('customers').all() == [{'b': 2}]

def test_ac3_rename_table_source_missing():
    db = TinyDB(storage=MemoryStorage)
    with pytest.raises((KeyError, ValueError)):
        db.rename_table('users', 'customers')

def test_ac4_rename_table_persistence(tmp_path):
    path = tmp_path / 'db.json'
    db = TinyDB(path)
    doc_id = db.table('users').insert({'test': 1})
    db.rename_table('users', 'customers')
    db.close()
    db2 = TinyDB(path)
    assert 'users' not in db2.tables()
    assert db2.table('customers').get(doc_id=doc_id) == {'test': 1}
    db2.close()

def test_ac5_rename_table_cache_correctness():
    db = TinyDB(storage=MemoryStorage)
    u = db.table('users')
    u.insert({'x': 1})
    doc_id = u.all()[0].doc_id
    db.rename_table('users', 'customers')
    # Accessing private _tables to verify internal state as required by AC-5
    assert 'users' not in db._tables
    assert 'customers' not in db._tables
    assert db.table('users').all() == []
    assert db.table('customers').get(doc_id=doc_id) == {'x': 1}
