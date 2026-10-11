import threading
import time
from contextlib import contextmanager

import pytest
from bit import knowledge_product_collection as collect


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('collection did not reach expected state')


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('KNOWLEDGE_PRODUCT_COLLECTION_DB', str(tmp_path / 'products.sqlite3'))
    return collect.ProductStore()


def test_fields_zeroes_categories_images_and_persistent_enterprise_boundary(store):
    product = collect.normalize_product('zying', {'Sku': 'MLM123', 'Title': '<b>Product</b>', 'Orders': 0,
        'Price': 0, 'Brand': 'Brand', 'Thumb': '//img.test/1.jpg', 'CateFullName': 'Electronics/Accessories',
        'Uptime': '2026-10-09'}, '1')
    assert product['sales'] == product['price'] == 0
    assert product['subcategory'] == 'Accessories'
    assert product['currency'] == 'MXN'
    assert product['main_image_url'] == 'https://img.test/1.jpg'
    blue = collect.normalize_product('lanjing', {'id': 'MLM123', 'title': 'Blue product', 'sold_his': 10,
        'price': 25, 'pic_big': 'https://img.test/2.jpg', 'start_time': 1791504000000,
        'path_from_root': [{'id': 'a', 'name': 'Parent'}, {'id': 'b', 'name': 'Child'}],
        'attributes': [{'id': 'BRAND', 'value_name': 'Blue Brand'}]}, 'MLM')
    assert blue['brand'] == 'Blue Brand'
    assert blue['subcategory'] == 'Child'
    assert blue['listed_at'] == '2026-10-09'
    store.save('org-a', [product, blue]); store.save('org-b', [dict(product, title='private')])
    store.save('org-a', [dict(product, price=15)])
    assert collect.ProductStore().listing('org-a')['total'] == 2
    assert store.listing('org-b')['rows'][0]['title'] == 'private'
    assert store.listing('org-a', source='zying')['rows'][0]['price'] == 15
    assert store.listing('org-a', search='Child')['total'] == 1
    assert store.listing('org-a', search='%')['total'] == 0
    assert store.listing('org-c')['total'] == 0


@pytest.mark.parametrize('source,response', [
    ('zying', {'code': 200, 'data': {'Datas': [], 'Total': 0}}),
    ('lanjing', {'code': 0, 'data': {'list': [], 'total': 0}}),
    ('lanjing', {'code': 0, 'data': [], 'total': 0}),
])
def test_platform_listing_shapes(source, response):
    assert collect.parse_listing(source, response) == ([], 0)


@pytest.mark.parametrize('response', [{'code': 200, 'data': {}}, {'code': 0, 'data': []},
    {'code': 0, 'data': [], 'total': 1, 'totalMore': True}])
def test_invalid_or_limited_feed_cannot_report_complete(response):
    with pytest.raises(RuntimeError):
        collect.parse_listing('lanjing', response)


class Reader:
    site = '1'
    def __init__(self, pages, *, fail_login_once=False):
        self.pages = pages; self.calls = []; self.prepared = 0
        self.fail_login_once = fail_login_once
    def prepare(self):
        self.prepared += 1
        if self.fail_login_once and self.prepared == 1:
            raise collect.LoginRequired('请先登录')
    def listing(self, number):
        self.calls.append(number)
        return self.pages[number - 1]


def factory(reader):
    @contextmanager
    def open_browser(source):
        yield reader
    return open_browser


def row(number):
    return {'Sku': str(number), 'Title': f'Product {number}', 'Orders': number}


def test_no_product_read_until_confirm_all_pages_and_relogin(store):
    reader = Reader([([row(1), row(2)], 3), ([row(3)], 3)], fail_login_once=True)
    manager = collect.CollectionManager(store)
    owner = ('org-a', '1')
    manager.start(owner, 'zying', factory(reader))
    wait_until(lambda: manager.status(owner)['phase'] == 'waiting_login')
    assert reader.prepared == 0 and not reader.calls
    with pytest.raises(ValueError): manager.confirm(('org-b', '1'))
    manager.confirm(owner)
    wait_until(lambda: manager.status(owner)['phase'] == 'waiting_login' and reader.prepared == 1)
    assert not reader.calls
    manager.confirm(owner)
    wait_until(lambda: not manager.status(owner)['running'])
    assert manager.status(owner)['phase'] == 'completed'
    assert reader.calls == [1, 2]
    assert store.listing('org-a')['total'] == 3


@pytest.mark.parametrize('pages', [ [([row(1)], 2), ([], 2)], [([row(1)], 2), ([row(1)], 2)],
                                   [([row(1)], 2), ([row(2)], 3)] ])
def test_partial_repeated_and_changed_pages_retain_data_and_report_failure(store, pages):
    reader = Reader(pages); manager = collect.CollectionManager(store); owner = ('org-a', '1')
    manager.start(owner, 'zying', factory(reader))
    wait_until(lambda: manager.status(owner)['phase'] == 'waiting_login')
    manager.confirm(owner)
    wait_until(lambda: not manager.status(owner)['running'])
    assert manager.status(owner)['phase'] == 'failed'
    assert store.listing('org-a')['total'] == 1


def test_stop_waiting_login_and_global_browser_lease(store):
    reader = Reader([]); manager = collect.CollectionManager(store); owner = ('org-a', '1')
    manager.start(owner, 'lanjing', factory(reader))
    wait_until(lambda: manager.status(owner)['phase'] == 'waiting_login')
    with pytest.raises(ValueError): manager.start(('org-a', '2'), 'zying', factory(reader))
    manager.stop(owner)
    wait_until(lambda: not manager.status(owner)['running'])
    assert manager.status(owner)['phase'] == 'stopped'
    assert not reader.calls
    assert manager.active is None


def test_blue_brand_array_and_reversed_category_path():
    product = collect.normalize_product('lanjing', {'id': 'MLM1', 'title': 'Product', 'brand_id': ['ACME'],
        'category_id': 'leaf', 'path_from_root': [{'id': 'leaf', 'name': 'Child'}, {'id': 'root', 'name': 'Parent'}]})
    assert product['brand'] == 'ACME'
    assert product['subcategory'] == 'Child'
    assert product['category_path'] == 'Parent > Child'
