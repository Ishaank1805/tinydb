from tinydb import TinyDB, Query
from tinydb.storages import MemoryStorage


def test_ac1_fragment_equality_and_search():
    Order = Query()
    q_billing = Order.billing.fragment({'country': 'DE'})
    q_shipping = Order.shipping.fragment({'country': 'DE'})

    assert q_billing != q_shipping
    assert hash(q_billing) != hash(q_shipping)

    db = TinyDB(storage=MemoryStorage)
    db.insert({'billing': {'country': 'DE'}, 'shipping': {'country': 'FR'}})

    results = db.search(q_shipping)
    assert results == []

    billing_results = db.search(q_billing)
    assert len(billing_results) == 1


def test_ac2_nested_fragment_search_returns_empty():
    db = TinyDB(storage=MemoryStorage)
    Order = Query()

    db.insert({'billing': {'country': 'DE'}, 'shipping': {'country': 'FR'}})
    db.insert({'billing': {'country': 'FR'}, 'shipping': {'country': 'FR'}})

    # First search with billing fragment
    res1 = db.search(Order.billing.fragment({'country': 'DE'}))
    assert len(res1) == 1

    # Second search with shipping fragment should not be cached from billing search
    res2 = db.search(Order.shipping.fragment({'country': 'DE'}))
    assert res2 == []


def test_ac3_nested_fragment_count():
    db = TinyDB(storage=MemoryStorage)
    Order = Query()

    db.insert({'billing': {'country': 'DE'}, 'shipping': {'country': 'FR'}})
    db.insert({'billing': {'country': 'FR'}, 'shipping': {'country': 'FR'}})

    # Count billing matching DE
    count1 = db.count(Order.billing.fragment({'country': 'DE'}))
    assert count1 == 1

    # Count shipping matching DE should return 0, not cached result from billing count
    count2 = db.count(Order.shipping.fragment({'country': 'DE'}))
    assert count2 == 0
