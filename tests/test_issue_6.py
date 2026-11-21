import pytest
from tinydb.table import Document
from tinydb import TinyDB
from tinydb.storages import MemoryStorage


def test_ac1_document_repr():
    doc = Document({'a': 1}, doc_id=42)
    assert repr(doc) == "Document({'a': 1}, doc_id=42)"


def test_ac2_db_all_repr():
    db = TinyDB(storage=MemoryStorage)
    db.insert({'a': 1})
    res = db.all()
    assert "Document" in repr(res[0]) and "doc_id=1" in repr(res[0]) and "{'a': 1}" in repr(res[0])
