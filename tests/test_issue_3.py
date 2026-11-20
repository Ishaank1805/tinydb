from tinydb import TinyDB, where, operations


def test_ac1_update_multiple_generator_dict(tmp_path):
    path = tmp_path / 'db.json'
    db = TinyDB(path)
    db.insert_multiple([
        {'sku': 'A1', 'price': 5},
        {'sku': 'A2', 'price': 15},
        {'sku': 'A3', 'price': 25}
    ])

    res = db.update_multiple(
        ({'price': price}, where('sku') == sku)
        for sku, price in [('A1', 10), ('A2', 20), ('A3', 30)]
    )

    assert len(res) == 3
    assert db.get(where('sku') == 'A1')['price'] == 10
    assert db.get(where('sku') == 'A2')['price'] == 20
    assert db.get(where('sku') == 'A3')['price'] == 30


def test_ac2_update_multiple_generator_operation(tmp_path):
    path = tmp_path / 'db.json'
    db = TinyDB(path)
    db.insert_multiple([
        {'sku': 'A1', 'price': 5},
        {'sku': 'A2', 'price': 15},
        {'sku': 'A3', 'price': 25}
    ])

    res = db.update_multiple(
        (operations.set('price', price), where('sku') == sku)
        for sku, price in [('A1', 10), ('A2', 20), ('A3', 30)]
    )

    assert len(res) == 3
    assert db.get(where('sku') == 'A1')['price'] == 10
    assert db.get(where('sku') == 'A2')['price'] == 20
    assert db.get(where('sku') == 'A3')['price'] == 30


def test_ac3_update_multiple_list(tmp_path):
    path = tmp_path / 'db.json'
    db = TinyDB(path)
    db.insert_multiple([
        {'char': 'a', 'int': 1},
        {'char': 'b', 'int': 1}
    ])

    res = db.update_multiple([
        ({'int': 2}, where('char') == 'a'),
        ({'int': 4}, where('char') == 'b')
    ])

    assert res == [1, 2]
    assert db.get(where('char') == 'a')['int'] == 2
    assert db.get(where('char') == 'b')['int'] == 4
