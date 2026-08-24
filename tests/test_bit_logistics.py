import json

from bit import bit_logistics


def _order(**changes):
    row = {
        "order_id": "20001",
        "purchase_tracking": "SF123456",
        "logistics_company": "shunfeng",
        "tracking_cache_json": None,
        "tracking_checked_at": None,
    }
    row.update(changes)
    return row


def test_tracking_without_api_credentials_returns_official_query_link(monkeypatch):
    monkeypatch.delenv("KUAIDI100_CUSTOMER", raising=False)
    monkeypatch.delenv("KUAIDI100_KEY", raising=False)
    monkeypatch.setattr(
        bit_logistics.bit_mysql,
        "get_mercado_order_procurement",
        lambda _order_id: _order(),
    )

    result = bit_logistics.query_order_tracking("20001")

    assert result["configured"] is False
    assert result["events"] == []
    assert "com=shunfeng" in result["external_url"]
    assert "nu=SF123456" in result["external_url"]


def test_tracking_api_normalizes_and_caches_events(monkeypatch):
    monkeypatch.setenv("KUAIDI100_CUSTOMER", "customer")
    monkeypatch.setenv("KUAIDI100_KEY", "key")
    monkeypatch.setattr(
        bit_logistics.bit_mysql,
        "get_mercado_order_procurement",
        lambda _order_id: _order(),
    )
    cached = []
    monkeypatch.setattr(
        bit_logistics.bit_mysql,
        "update_mercado_tracking_cache",
        lambda order_id, payload: cached.append((order_id, payload)),
    )

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "status": "200",
                "state": "3",
                "message": "ok",
                "data": [
                    {
                        "ftime": "2026-08-24 10:00:00",
                        "context": "快件已揽收",
                        "location": "武汉",
                    }
                ],
            }

    posted = {}

    def fake_post(url, data, timeout):
        posted.update({"url": url, "data": data, "timeout": timeout})
        return Response()

    monkeypatch.setattr(bit_logistics.requests, "post", fake_post)

    result = bit_logistics.query_order_tracking("20001")

    assert result["configured"] is True
    assert result["events"][0] == {
        "time": "2026-08-24 10:00:00",
        "description": "快件已揽收",
        "location": "武汉",
        "status": "",
    }
    assert cached[0][0] == "20001"
    assert json.loads(posted["data"]["param"])["num"] == "SF123456"
    assert len(posted["data"]["sign"]) == 32
