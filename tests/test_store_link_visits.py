from unittest.mock import Mock
import pytest
from bit.store_link_visits import fetch_total_visits
from erp.mercadolibre_store_link_store import listing_record

@pytest.mark.parametrize('value', [0, 123456789])
def test_valid_visits(value):
    client=Mock();client.request.return_value={'MLM123':value}
    result=fetch_total_visits(client,'MLM123')
    assert result['total_visits']==value
    assert result['visits_updated_at'] and result['visits_error']==''
    client.request.assert_called_once_with('GET','/visits/items',params={'ids':'MLM123'})

@pytest.mark.parametrize('data',[{}, {'MLM123':None},{'MLM123':True},{'MLM123':-1},{'MLM123':'0'},[]])
def test_missing_or_invalid_not_zero(data):
    client=Mock();client.request.return_value=data
    result=fetch_total_visits(client,'MLM123')
    assert result['total_visits'] is None and result['visits_error']

def test_unsupported_and_request_failure():
    client=Mock()
    assert fetch_total_visits(client,'CBT123')['total_visits'] is None
    client.request.assert_not_called()
    client.request.side_effect=RuntimeError('secret must not appear')
    result=fetch_total_visits(client,'MLM123')
    assert result['total_visits'] is None
    assert 'secret' not in result['visits_error']

def test_link_sync_does_not_request_visits():
    from bit.bit_store_link_sync import _marketplace_item_with_quality
    client=Mock()
    client.get_marketplace_item.return_value={'id':'MLM123'}
    client.request.return_value={}
    detail=_marketplace_item_with_quality(client,'MLM123',attributes=('id',))
    client.request.assert_called_once_with('GET','/item/MLM123/performance')
    assert 'total_visits' not in detail


def test_persistence_preserves_zero_and_previous_value_on_failure():
    from erp.store_link_visits_store import save_visits
    cursor=Mock()
    save_visits(cursor,[{'token_id':7,'item_id':'MLM123','total_visits':0,'visits_error':''}])
    sql, values=cursor.executemany.call_args.args
    assert values[0][:3]==(7,'MLM123',0)
    assert 'total_visits=COALESCE(VALUES(total_visits),total_visits)' in sql
