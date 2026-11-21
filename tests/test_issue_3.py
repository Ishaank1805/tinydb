import pytest
from tinydb import TinyDB, where
from tinydb.storages import MemoryStorage
from tinydb.operations import set

@pytest.fixture
def db():
    db = TinyDB(storage=MemoryStorage)
    db.insert_multiple([
        {'sku': 'A1', 'price': 10},
        {'sku': 'A2', 'price': 20},
        {'sku': 'A3', 'price': 30}
    ])
    return db

def test_ac1_update_multiple_generator(db):
    new_prices = {'A1': 11, 'A2': 22, 'A3': 33}
    
    # AC-1: update_multiple with a generator yielding (dict, query) tuples
    updated_ids = db.update_multiple(({'price': p}, where('sku') == s) for s, p in new_prices.items())
    
    assert len(updated_ids) == 3
    for s, p in new_prices.items():
        res = db.get(where('sku') == s)
        assert res['price'] == p
        assert res.doc_id in updated_ids

def test_ac2_update_multiple_generator_operation(db):
    # AC-2: update_multiple with a generator yielding (operation_callable, query) tuples
    # Define operations: increment price by 5
    def increment_price(doc):
        doc['price'] += 5
        
    operations = {
        'A1': increment_price,
        'A2': increment_price,
        'A3': increment_price
    }
    
    updated_ids = db.update_multiple((operations[s], where('sku') == s) for s in ['A1', 'A2', 'A3'])
    
    assert len(updated_ids) == 3
    for s in ['A1', 'A2', 'A3']:
        res = db.get(where('sku') == s)
        # Original prices were 10, 20, 30. Now should be 15, 25, 35
        assert res['price'] == (10 if s == 'A1' else 20 if s == 'A2' else 30) + 5
        assert res.doc_id in updated_ids
