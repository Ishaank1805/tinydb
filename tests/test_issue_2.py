import pytest
from tinydb import TinyDB, Query
from tinydb.storages import MemoryStorage

@pytest.fixture
def db():
    db = TinyDB(storage=MemoryStorage)
    db.insert({'billing': {'country': 'DE'}, 'shipping': {'country': 'FR'}})
    return db

def test_ac1_fragment_equality():
    Order = Query()
    q1 = Order.billing.fragment({'country': 'DE'})
    q2 = Order.shipping.fragment({'country': 'DE'})
    
    # AC-1: Order.billing.fragment({'country': 'DE'}) != Order.shipping.fragment({'country': 'DE'}) is True
    assert q1 != q2
    assert (q1 == q2) is False

def test_ac2_shipping_fragment_search(db):
    Order = Query()
    
    # AC-2: db.search(Order.shipping.fragment({'country': 'DE'})) returns []
    # Verify that searching with a non-matching shipping fragment query returns empty list
    # even after searching a billing fragment query that matches.
    
    # Matching query
    results_billing = db.search(Order.billing.fragment({'country': 'DE'}))
    assert len(results_billing) == 1
    
    # Non-matching query
    results_shipping = db.search(Order.shipping.fragment({'country': 'DE'}))
    assert results_shipping == []

def test_ac3_shipping_fragment_count(db):
    Order = Query()
    
    # AC-3: db.count(Order.shipping.fragment({'country': 'DE'})) returns 0
    count = db.count(Order.shipping.fragment({'country': 'DE'}))
    assert count == 0
