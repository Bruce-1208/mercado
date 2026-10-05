from unittest.mock import patch
import bit.bit_interface as app
from bit import bit_db_api


def client():
    app.app.config.update(TESTING=True, SECRET_KEY='test-secret')
    result = app.app.test_client()
    with result.session_transaction() as session:
        session['workbench_user'] = {'id': 1, 'username': 'tester', 'display_name': '测试'}
    return result


def test_list_negative_store_is_scoped_without_leaking_other_stores():
    with patch.object(app, '_authorized_order_store_ids', return_value={-2, 7}), patch.object(app, 'db_list_orders', return_value={'rows': [], 'total': 0}) as query:
        response = client().get('/api/orders?store_id=-2&store_id=-3&origin=yandex')
    assert response.status_code == 200
    assert query.call_args.kwargs['store_ids'] == [-2]
    assert query.call_args.kwargs['origin'] == 'yandex'


def test_unauthorized_yandex_sync_does_not_start():
    with patch.object(app, '_can_access_yandex_orders', return_value=False), patch.object(bit_db_api, 'yandex_order_sync') as sync:
        response = client().post('/api/order-sync/yandex')
    assert response.status_code == 403
    sync.assert_not_called()


def test_yandex_sync_returns_durable_progress():
    with patch.object(app, '_can_access_yandex_orders', return_value=True), patch.object(bit_db_api, 'yandex_order_sync', return_value={'started': True, 'state': {'running': True}}) as sync:
        response = client().post('/api/order-sync/yandex')
    assert response.status_code == 202
    sync.assert_called_once_with('start')


def test_yandex_cannot_be_sent_to_mercado_label_api():
    with patch.object(bit_db_api, 'download_order_labels') as contexts:
        response = client().post('/api/orders/print', json={'order_ids': ['yandex:33:123']})
    assert response.status_code == 400
    contexts.assert_not_called()


def test_other_organizations_cannot_see_yandex_logs():
    with patch.object(app, '_can_access_yandex_orders', return_value=False), patch.object(bit_db_api, 'list_order_operation_logs') as logs:
        response = client().get('/api/orders/yandex:33:123/logs')
    assert response.status_code == 403
    logs.assert_not_called()
