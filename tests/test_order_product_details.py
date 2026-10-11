import json
from unittest.mock import MagicMock, call, patch

import pytest
import requests

from bit import bit_db_api, order_product_details as details
from mercado_api.client import MercadoAPIError, MercadoLibreClient
from erp.mercadolibre_translation import ListingTranslationError


@pytest.fixture(autouse=True)
def clean_detail_cache():
    with details._DETAIL_CACHE_LOCK:
        details._DETAIL_CACHE.clear()
    yield
    with details._DETAIL_CACHE_LOCK:
        details._DETAIL_CACHE.clear()


@pytest.fixture
def order_context():
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchall.return_value = [{
        'order_id': '10', 'token_id': 1, 'product_id': 'MLB123',
        'raw_json': '{}', 'access_token': 'old-token', 'refresh_token': 'refresh-token',
    }]
    connection = MagicMock()
    connection.cursor.return_value = cursor
    with patch.object(details.bit_mysql.pymysql, 'connect', return_value=connection):
        yield cursor, connection


@pytest.fixture
def listing_client():
    client = MagicMock()
    client.request.side_effect = lambda method, path, **kwargs: (
        {'plain_text': 'Algodão'} if path.endswith('/description')
        else {'id': path.rsplit('/', 1)[-1], 'title': 'Camisa'}
    )
    with patch.object(details, 'MercadoLibreClient', return_value=client) as factory:
        yield client, factory


def test_first_read_skips_translation_and_bounds_platform_requests(order_context, listing_client):
    client, factory = listing_client
    with patch.object(details, 'translate_product_detail') as translate:
        result = details.get_order_product_detail(['10'], 'mlb123', {1})
    assert result['item']['title'] == 'Camisa'
    assert result['translations_zh'] == {}
    assert result['translation_error'] == ''
    translate.assert_not_called()
    factory.assert_called_once_with('old-token', timeout=(3, 8), admission_timeout=2)
    assert client.request.call_args_list == [
        call('GET', '/marketplace/items/MLB123', max_attempts=1,
             params={'include_attributes': 'all', 'include_internal_attributes': 'true'}),
        call('GET', '/items/MLB123/description', max_attempts=1),
    ]
    client.session.close.assert_called_once()
    order_context[1].close.assert_called_once()


def test_translation_reuses_platform_detail_but_rechecks_order_access(order_context, listing_client):
    first = details.get_order_product_detail(['10'], 'MLB123', {1})
    with patch.object(details, 'translate_product_detail', return_value={'Camisa': '衬衫'}) as translate:
        translated = details.get_order_product_detail(['10'], 'MLB123', {1}, include_translation=True)
    assert translated['translations_zh'] == {'Camisa': '衬衫'}
    translate.assert_called_once_with(first['item'], first['description'])
    assert listing_client[0].request.call_count == 2
    assert order_context[0].execute.call_count == 2
    # Returned dictionaries cannot change what another request reads from cache.
    translated['item']['title'] = 'changed by caller'
    assert details.get_order_product_detail(['10'], 'MLB123', {1})['item']['title'] == 'Camisa'


def test_cached_product_is_not_returned_after_order_scope_is_removed(order_context, listing_client):
    details.get_order_product_detail(['10'], 'MLB123', {1})
    with pytest.raises(ValueError, match='无权查看'):
        details.get_order_product_detail(['10'], 'MLB123', set(), include_translation=True)
    assert listing_client[0].request.call_count == 2
    assert order_context[0].execute.call_count == 2


def test_cache_is_separate_for_each_store(order_context, listing_client):
    details.get_order_product_detail(['10'], 'MLB123', {1})
    order_context[0].fetchall.return_value[0]['token_id'] = 2
    details.get_order_product_detail(['10'], 'MLB123', {2})
    assert listing_client[0].request.call_count == 4


def test_refresh_reads_platform_and_translation_uses_the_new_snapshot(order_context, listing_client):
    details.get_order_product_detail(['10'], 'MLB123', {1})
    listing_client[0].request.side_effect = [
        {'id': 'MLB123', 'title': 'Updated'}, {'plain_text': 'Updated description'},
    ]
    details.get_order_product_detail(['10'], 'MLB123', {1}, force_refresh=True)
    with patch.object(details, 'translate_product_detail', return_value={}) as translate:
        details.get_order_product_detail(['10'], 'MLB123', {1}, include_translation=True)
    assert translate.call_args.args[0]['title'] == 'Updated'
    assert listing_client[0].request.call_count == 4


def test_cache_expires_and_refetches(order_context, listing_client):
    with patch.object(details.time, 'monotonic', return_value=100):
        details.get_order_product_detail(['10'], 'MLB123', {1})
    with patch.object(details.time, 'monotonic', return_value=401):
        details.get_order_product_detail(['10'], 'MLB123', {1})
    assert listing_client[0].request.call_count == 4


def test_global_item_uses_marketplace_endpoint_first(order_context, listing_client):
    order_context[0].fetchall.return_value[0]['product_id'] = 'CBT123'
    details.get_order_product_detail(['10'], 'CBT123', {1})
    assert listing_client[0].request.call_args_list[0].args[1] == '/marketplace/items/CBT123'


def test_localized_item_uses_global_selling_token_scope_instead_of_id_prefix(order_context, listing_client):
    product_id = 'MLA2122263425'
    order_context[0].fetchall.return_value[0]['product_id'] = product_id

    def read(method, path, **kwargs):
        if path == f'/items/{product_id}':
            raise MercadoAPIError(f'GET {path} 失败 (403): access_denied')
        if path == f'/marketplace/items/{product_id}':
            return {'id': product_id, 'title': 'Producto'}
        if path == f'/items/{product_id}/description':
            return {'plain_text': 'Descripción'}
        raise AssertionError(f'unexpected path: {path}')

    listing_client[0].request.side_effect = read
    result = details.get_order_product_detail(['10'], product_id, {1})
    assert result['item']['id'] == product_id
    assert result['description']['plain_text'] == 'Descripción'
    assert [entry.args[1] for entry in listing_client[0].request.call_args_list] == [
        f'/marketplace/items/{product_id}', f'/items/{product_id}/description',
    ]


@pytest.mark.parametrize('status', [404, 405])
def test_unsupported_primary_endpoint_falls_back(order_context, listing_client, status):
    listing_client[0].request.side_effect = [
        MercadoAPIError(f'GET /marketplace/items/MLB123 失败 ({status}): not found'),
        {'id': 'MLB123', 'title': 'Camisa'}, {},
    ]
    assert details.get_order_product_detail(['10'], 'MLB123', {1})['item']['id'] == 'MLB123'
    paths = [entry.args[1] for entry in listing_client[0].request.call_args_list]
    assert paths == ['/marketplace/items/MLB123', '/items/MLB123', '/items/MLB123/description']


@pytest.mark.parametrize('message', [
    'GET /items/MLB123 网络请求多次失败：timed out',
    'GET /items/MLB123 失败 (429): too many requests',
    'GET /items/MLB123 失败 (500): unavailable',
    'GET /items/MLB123 失败 (403): forbidden',
])
def test_transient_or_permission_failure_does_not_repeat_through_fallback(order_context, listing_client, message):
    listing_client[0].request.side_effect = MercadoAPIError(message)
    with pytest.raises(MercadoAPIError):
        details.get_order_product_detail(['10'], 'MLB123', {1})
    assert listing_client[0].request.call_count == 1
    listing_client[0].session.close.assert_called_once()


@pytest.mark.parametrize('status, warning_count', [(404, 0), (500, 1)])
def test_missing_description_does_not_block_product(order_context, listing_client, status, warning_count):
    listing_client[0].request.side_effect = [
        {'id': 'MLB123', 'title': 'Camisa'},
        MercadoAPIError(f'GET /items/MLB123/description 失败 ({status}): unavailable'),
    ]
    result = details.get_order_product_detail(['10'], 'MLB123', {1})
    assert result['item']['title'] == 'Camisa'
    assert result['description'] == {}
    assert len(result['warnings']) == warning_count


def test_invalid_description_json_does_not_block_product(order_context, listing_client):
    listing_client[0].request.side_effect = [
        {'id': 'MLB123', 'title': 'Camisa'}, ValueError('invalid JSON'),
    ]
    result = details.get_order_product_detail(['10'], 'MLB123', {1})
    assert result['item']['title'] == 'Camisa'
    assert result['description'] == {}
    assert result['warnings'] == ['商品描述暂时读取失败，请重试']


def test_central_token_refresh_uses_the_interactive_timeout():
    with patch.object(details.mercado_tokens, 'refresh_and_save') as refresh, \
            patch.object(details.bit_mysql, 'get_mercado_store_token', return_value={'access_token': 'new'}):
        assert details._refresh_store_token(1)['access_token'] == 'new'
    assert refresh.call_args.kwargs['timeout'] == 8
    assert refresh.call_args.kwargs['lock_timeout'] == 2
    assert refresh.call_args.kwargs['record_error'] is details.bit_mysql.record_mercado_store_token_error


def test_real_api_401_refreshes_the_central_store_token(order_context):
    def response(status, body):
        value = requests.Response()
        value.status_code = status
        value._content = json.dumps(body).encode()
        return value

    expired = MagicMock()
    expired.request.return_value = response(401, {'error': 'invalid_token'})
    renewed = MagicMock()
    renewed.request.side_effect = [
        response(200, {'id': 'MLB123', 'title': 'Camisa'}),
        response(200, {'plain_text': 'Algodão'}),
    ]

    def build_client(access_token, *, timeout, admission_timeout):
        assert admission_timeout == 2
        return MercadoLibreClient(access_token, timeout=timeout,
                                  session=renewed if access_token == 'new-token' else expired,
                                  admission_timeout=admission_timeout)

    with patch.object(details, 'MercadoLibreClient', side_effect=build_client), \
            patch.object(details, '_refresh_store_token', return_value={'access_token': 'new-token'}) as refresh:
        result = details.get_order_product_detail(['10'], 'MLB123', {1})
    assert result['item']['title'] == 'Camisa'
    refresh.assert_called_once_with(1)
    assert expired.request.call_count == 1
    assert renewed.request.call_count == 2
    assert renewed.request.call_args.kwargs['headers']['Authorization'] == 'Bearer new-token'
    assert renewed.request.call_args.kwargs['timeout'] == (3, 8)
    expired.close.assert_called_once()
    renewed.close.assert_called_once()


def test_translation_failure_keeps_the_raw_detail_and_does_not_poison_cache(order_context, listing_client):
    with patch.object(details, 'translate_product_detail', side_effect=ListingTranslationError('模型未安装')):
        result = details.get_order_product_detail(['10'], 'MLB123', {1}, include_translation=True)
    assert result['item']['title'] == 'Camisa'
    assert result['translations_zh'] == {}
    assert '模型未安装' in result['translation_error']
    assert result['translation_error'] in result['warnings']
    raw = details.get_order_product_detail(['10'], 'MLB123', {1})
    assert raw['warnings'] == []
    assert raw['translation_error'] == ''


def test_null_order_items_can_use_the_synced_product_id(order_context, listing_client):
    order_context[0].fetchall.return_value[0]['raw_json'] = '{"order_items": null}'
    assert details.get_order_product_detail(['10'], 'MLB123', {1})['item']['id'] == 'MLB123'


def test_product_in_raw_order_items_is_accessible(order_context, listing_client):
    order_context[0].fetchall.return_value[0].update(
        product_id='MLB456', raw_json='{"order_items": [{"item": {"id": "MLB123"}}]}',
    )
    assert details.get_order_product_detail(['10'], 'MLB123', {1})['item']['id'] == 'MLB123'


def test_api_client_forwards_stage_and_refresh_with_interactive_timeouts():
    with patch.object(bit_db_api, 'DB_MODE', 'api'), patch.object(bit_db_api, '_request') as request:
        bit_db_api.get_order_product_detail(['10'], 'MLB123', {1}, force_refresh=True)
        assert request.call_args.kwargs['timeout'] == (3, 35)
        assert request.call_args.kwargs['json'] == {
            'order_ids': ['10'], 'product_id': 'MLB123', 'allowed_token_ids': [1],
            'include_translation': False, 'force_refresh': True, 'include_description': True,
        }
        bit_db_api.get_order_product_detail(['10'], 'MLB123', {1}, include_translation=True)
        assert request.call_args.kwargs['timeout'] == (3, 45)
        assert request.call_args.kwargs['json']['include_translation'] is True
        assert request.call_args.kwargs['max_attempts'] == 1


def test_fast_stage_returns_item_without_waiting_for_description(order_context, listing_client):
    first = details.get_order_product_detail(['10'], 'MLB123', {1}, include_description=False)
    assert first['description_pending'] is True
    assert first['item']['title'] == 'Camisa'
    assert listing_client[0].request.call_count == 1
    with patch.object(details, 'translate_product_detail', return_value={'Camisa': '衬衫'}):
        complete = details.get_order_product_detail(['10'], 'MLB123', {1}, include_translation=True)
    assert complete['description_pending'] is False
    assert complete['description']['plain_text'] == 'Algodão'
    assert listing_client[0].request.call_count == 2
    assert listing_client[0].request.call_args.args[1].endswith('/description')


def test_reopen_reuses_translation_and_refresh_invalidates_it(order_context, listing_client):
    with patch.object(details, 'translate_product_detail', return_value={'Camisa': '衬衫'}) as translate:
        details.get_order_product_detail(['10'], 'MLB123', {1}, include_translation=True)
        reopened = details.get_order_product_detail(['10'], 'MLB123', {1}, include_description=False)
        assert reopened['translations_ready'] is True
        assert reopened['translations_zh']['Camisa'] == '衬衫'
        details.get_order_product_detail(['10'], 'MLB123', {1}, include_translation=True)
        translate.assert_called_once()
        refreshed = details.get_order_product_detail(['10'], 'MLB123', {1}, include_description=False, force_refresh=True)
        assert not refreshed.get('translations_ready')
        assert refreshed['description_pending'] is True
    assert order_context[0].execute.call_count == 4
