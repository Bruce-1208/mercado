"""Check the fast-detail/translation contract without platform or database calls."""
from unittest.mock import patch

import pytest

from bit import bit_interface as workbench

pytestmark = pytest.mark.usefixtures("isolated_legacy_console_user")


def client():
    workbench.app.config.update(TESTING=True, SECRET_KEY="order-detail-test")
    result = workbench.app.test_client()
    with result.session_transaction() as session:
        session["workbench_user"] = {"id": 1, "username": "test"}
    return result


@pytest.mark.parametrize("query,translate,refresh", [
    ("", False, False), ("&translate=1", True, False),
    ("&translate=0&refresh=1", False, True),
])
def test_public_detail_forwards_stages_and_keeps_authorization(query, translate, refresh):
    with patch.object(workbench, "_authorized_token_ids_for_user", return_value={7}), \
            patch.object(workbench.bit_db_api, "get_order_product_detail", return_value={"item": {"id": "MLM123"}}) as read:
        response = client().get("/api/orders/product-detail?product_id=MLM123&order_id=100&order_id=101" + query)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    read.assert_called_once_with(["100", "101"], "MLM123", {7},
                                 include_translation=translate, force_refresh=refresh, include_description=True)


def test_detail_requires_login():
    workbench.app.config.update(TESTING=True, SECRET_KEY="order-detail-test")
    with patch.object(workbench.bit_db_api, "get_order_product_detail") as read:
        response = workbench.app.test_client().get("/api/orders/product-detail?product_id=MLM123&order_id=100")
    assert response.status_code == 401
    read.assert_not_called()


@pytest.mark.parametrize("flag,expected", [(True, True), (False, False), ("false", False)])
def test_internal_detail_uses_boolean_flags(flag, expected):
    with patch.object(workbench, "reject_db_api_client_mode", return_value=None), \
            patch("bit.order_product_details.get_order_product_detail", return_value={"item": {"id": "MLM123"}}) as read:
        response = client().post("/api/db/orders/product-detail", json={
            "order_ids": ["100"], "product_id": "MLM123", "allowed_token_ids": [7],
            "include_translation": flag, "force_refresh": flag,
        })
    assert response.status_code == 200
    read.assert_called_once_with(["100"], "MLM123", [7],
                                 include_translation=expected, force_refresh=expected, include_description=True)


def test_internal_detail_failure_stays_json():
    with patch.object(workbench, "reject_db_api_client_mode", return_value=None), \
            patch("bit.order_product_details.get_order_product_detail", side_effect=RuntimeError("queue busy")):
        response = client().post("/api/db/orders/product-detail", json={
            "order_ids": ["100"], "product_id": "MLM123", "allowed_token_ids": [7],
        })
    assert response.status_code == 502
    assert response.get_json()["status"] == "error"


def test_display_exchange_rate_requires_login():
    with patch('bit.order_display_rates.latest_order_display_rate') as read:
        response = workbench.app.test_client().get('/api/orders/display-exchange-rate')
    assert response.status_code == 401
    read.assert_not_called()


def test_display_exchange_rate_returns_dated_snapshot():
    snapshot = {'cny_per_usd':7.12, 'rate_date':'2026-10-08', 'source':'欧洲央行参考汇率'}
    with patch('bit.order_display_rates.latest_order_display_rate', return_value=snapshot):
        response = client().get('/api/orders/display-exchange-rate')
    assert response.status_code == 200
    assert response.json['data'] == snapshot
    assert response.headers['Cache-Control'] == 'no-store'


def test_display_exchange_rate_failure_is_explicit():
    with patch('bit.order_display_rates.latest_order_display_rate', side_effect=ValueError('missing')):
        response = client().get('/api/orders/display-exchange-rate')
    assert response.status_code == 502
    assert response.json['status'] == 'error'


def test_fast_detail_stage_is_forwarded():
    with patch.object(workbench, '_authorized_token_ids_for_user', return_value={7}), patch.object(workbench.bit_db_api, 'get_order_product_detail', return_value={}) as read:
        response = client().get('/api/orders/product-detail?product_id=MLM123&order_id=100&description=0')
    assert response.status_code == 200
    assert read.call_args.kwargs['include_description'] is False
