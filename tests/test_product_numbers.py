"""Product numbers use persisted identity, independent of title/source/order."""
import pytest

from erp.mercadolibre_collection_store import _json_safe_row, product_number, collection_product_number


@pytest.mark.parametrize('product_id,expected', [(1, '100000001'), (123456, '100123456'),
                                                (799999999, '899999999')])
def test_number_is_fixed_width(product_id, expected):
    assert product_number(product_id) == expected


@pytest.mark.parametrize('product_id', [0, -1, 800000000, 900000000])
def test_out_of_range_never_wraps(product_id):
    with pytest.raises(ValueError):
        product_number(product_id)


def test_all_sources_keep_unique_numbers_after_edits():
    rows = [dict(id=i, collection_item_id=None, source_type=source, title='同名产品')
            for i, source in enumerate(['collected', 'pulled', 'zying', 'ai_original'], 1)]
    numbers = [_json_safe_row(row)['product_number'] for row in rows]
    assert len(set(numbers)) == len(rows)
    assert [_json_safe_row(dict(row, title='新标题'))['product_number']
            for row in reversed(rows)] == numbers[::-1]
    assert _json_safe_row(dict(id=1, task_id=1))['product_number'] == '900000001'


@pytest.mark.parametrize('collection_id', [1, 123456, 99999999])
def test_claim_keeps_collection_number_with_different_product_id(collection_id):
    collection = _json_safe_row(dict(id=collection_id, task_id=27))
    product = _json_safe_row(dict(id=854, collection_item_id=collection_id,
                                 source_type='collected', title='加入后编辑标题'))
    assert collection['product_number'] == product['product_number']
    assert len(product['product_number']) == 9
    assert product['product_number'].isdigit()
    assert product['product_number'] != product_number(854)


@pytest.mark.parametrize('collection_id', [0, -1, 100000000])
def test_collection_number_never_wraps(collection_id):
    with pytest.raises(ValueError):
        collection_product_number(collection_id)
