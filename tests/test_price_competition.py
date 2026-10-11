from copy import deepcopy
from decimal import Decimal
import pytest
from bit import price_competition as service
from erp.price_competition_store import PriceCompetitionStore
from mercado_api.client import MercadoAPIError
from mercado_api.price_competition import PriceCompetitionClient


def observation(kind="ordinary", target=80, **changes):
    signal = {"item_id": "MLM100", "currency_id": "MXN", "status": "with_benchmark_high",
              "current_price": {"amount": 100}, "suggested_price": {"amount": target}, "applicable_suggestion": True}
    if kind == "catalog":
        signal = {"item_id": "MLM100", "currency_id": "MXN", "status": "competing", "current_price": 100,
                  "price_to_win": target, "boosts": [{"id": "fulfillment", "status": "opportunity"}], "reason": []}
    signal.update(changes)
    return {"kind": kind, "item_id": "MLM100", "current_price": 100, "currency_id": "MXN", "item_status": "active", "signal": signal}


def costs(**changes):
    return service.validate_costs({"currency_id": "MXN", "fixed_cost": 50,
        "variable_cost_percent": 10, "minimum_profit": 10, **changes})


class Client:
    def __init__(self, flag=False, error=None):
        self.flag, self.error, self.calls = flag, error, []
    def get_marketplace_item(self, item_id):
        return {"id": item_id, "catalog_listing": self.flag}
    def request(self, method, path, **options):
        self.calls.append((method, path, options))
        if self.error:
            raise self.error
        return {"currency_id": "MXN"}


def test_separate_global_selling_endpoints_and_unknown_type():
    client = Client()
    adapter = PriceCompetitionClient(client, caller_id="parent", client_id="application")
    assert adapter.inspect("MLM100")["kind"] == "ordinary"
    assert client.calls == [("GET", "/marketplace/benchmarks/items/MLM100/details", {})]
    client.flag = True
    assert adapter.inspect("MLM100")["kind"] == "catalog"
    assert client.calls[-1] == ("GET", "/marketplace/items/MLM100/price_to_win", {"params": {"caller.id": "parent", "client.id": "application"}})
    client.flag = None
    count = len(client.calls)
    assert adapter.inspect("MLM100")["kind"] == "unknown"
    assert len(client.calls) == count
    assert all(call[0] == "GET" for call in client.calls)


def test_missing_reference_is_distinct_from_failed_reads():
    assert PriceCompetitionClient(Client(error=MercadoAPIError("GET endpoint 失败 (404): not found"))).reference("MLM100") is None
    for message in ("GET endpoint 失败 (403): forbidden", "网络请求多次失败"):
        with pytest.raises(MercadoAPIError):
            PriceCompetitionClient(Client(error=MercadoAPIError(message))).reference("MLM100")


@pytest.mark.parametrize("item_id", ["CBT100", "MLM100/details", "", "MLMU123", "../tokens"])
def test_non_marketplace_ids_are_rejected(item_id):
    with pytest.raises(ValueError):
        PriceCompetitionClient(Client()).inspect(item_id)


def test_separate_columns_and_profit_model():
    normal = service.suggestion(observation(), costs())
    catalog = service.suggestion(observation("catalog"), costs())
    assert normal["official_reference_price"] == 80 and normal["competition_target_price"] is None
    assert catalog["official_reference_price"] is None and catalog["competition_target_price"] == 80
    assert catalog["boosts"][0]["status"] == "opportunity"
    for result in (normal, catalog):
        assert result["minimum_acceptable_price"] == 66.67
        assert result["estimated_profit"] == 22 and result["ready"]


@pytest.mark.parametrize("snapshot,model,reason", [
    (observation(target=60), costs(), "低于最低可接受价"), (observation(), None, "核实"),
    (observation(), costs(currency_id="USD"), "币种"),
    (observation(applicable_suggestion=False), costs(), "不适用"),
    ({**observation(), "signal": None}, costs(), "缺失"), (observation(target=101), costs(), "不高于"),
    (observation("catalog", target=None), costs(), "未提供"),
    (observation("catalog", status="winning"), costs(), "已获胜"),
    (observation("catalog", status="sharing_first_place"), costs(), "共享首位"),
    (observation("catalog", status="listed"), costs(), "尚未参与"),
    (observation("catalog", status="unexpected"), costs(), "未知"),
    (observation(currency_id="USD"), costs(), "币种不一致"),
    (observation(current_price={"amount": 99}), costs(), "不同步"),
    ({**observation(), "item_status": "paused"}, costs(), "不在售"),
])
def test_blocks_unsafe_or_inapplicable_suggestions(snapshot, model, reason):
    result = service.suggestion(snapshot, model)
    assert not result["ready"] and reason in result["reason"]


def test_rounding_does_not_cross_profit_floor_or_catalog_target():
    result = service.suggestion(observation("catalog", target=Decimal("66.669")), costs())
    assert result["suggested_price"] == 66.66 and not result["ready"]
    assert service.suggestion(observation(target=66.67), costs())["ready"]
    tiny = service.suggestion(observation("catalog", target="0.005"), costs(fixed_cost=0, minimum_profit=0))
    assert not tiny["ready"] and "精度不足" in tiny["reason"]


@pytest.mark.parametrize("changes", [{"fixed_cost": ""}, {"fixed_cost": True}, {"minimum_profit": -1},
    {"fixed_cost": "NaN"}, {"variable_cost_percent": 100}, {"variable_cost_percent": "Infinity"}])
def test_invalid_cost_inputs(changes):
    with pytest.raises(ValueError):
        costs(**changes)


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    store = PriceCompetitionStore(tmp_path / "pricing.sqlite3")
    row = {"id": 1, "token_id": 11, "item_id": "MLM100", "is_current": True, "status": "active", "price": 100, "currency_id": "MXN"}
    live = observation()
    def link(link_id, allowed):
        if allowed is not None and row["token_id"] not in allowed:
            raise PermissionError("无权访问")
        return row
    monkeypatch.setattr(service, "_link", link)
    monkeypatch.setattr(service, "_inspect", lambda _: deepcopy(live))
    def dispatch(action, payload, actor="7", scope={11}):
        return service.dispatch(action, payload, actor=actor, allowed_token_ids=scope, store=store)
    dispatch("costs", {"link_id": 1, **costs()})
    return store, row, live, dispatch


def test_durable_submit_once_confirmation_does_not_change_platform(workflow):
    store, _, _, dispatch = workflow
    preview = dispatch("preview", {"link_id": 1})
    payload = {"preview_id": preview["preview_id"], "confirmed": True}
    result = dispatch("confirm", payload)
    assert result["status"] == "confirmed" and result["platform_price_changed"] is False
    assert result["row"]["approval"]["matches_current"]
    assert PriceCompetitionStore(store.path).latest_approval(11, "MLM100")["actor"] == "7"
    with pytest.raises(ValueError, match="已确认"):
        dispatch("confirm", payload)


@pytest.mark.parametrize("change", ["price", "boost", "costs"])
def test_confirmation_rechecks_official_prices_conditions_and_costs(workflow, change):
    store, _, live, dispatch = workflow
    if change == "boost":
        live.update(observation("catalog"))
    preview = dispatch("preview", {"link_id": 1})
    if change == "price":
        live["current_price"] = 101
    elif change == "boost":
        live["signal"]["boosts"][0]["status"] = "boosted"
    else:
        dispatch("costs", {"link_id": 1, **costs(fixed_cost=51)})
    with pytest.raises(ValueError, match="已变化"):
        dispatch("confirm", {"preview_id": preview["preview_id"], "confirmed": True})
    assert store.latest_approval(11, "MLM100") is None


def test_owner_scope_expiration_and_explicit_confirmation(workflow):
    store, _, _, dispatch = workflow
    preview = dispatch("preview", {"link_id": 1})
    payload = {"preview_id": preview["preview_id"], "confirmed": True}
    with pytest.raises(PermissionError):
        dispatch("confirm", payload, actor="8")
    with pytest.raises(PermissionError):
        dispatch("confirm", payload, scope=set())
    with pytest.raises(ValueError, match="核对"):
        dispatch("confirm", {**payload, "confirmed": "true"})
    with store.connection() as db:
        db.execute("UPDATE previews SET expires=0")
    with pytest.raises(ValueError, match="过期"):
        dispatch("confirm", payload)


def test_failure_invalidates_old_suggestion(workflow, monkeypatch):
    _, _, _, dispatch = workflow
    assert dispatch("refresh", {"link_id": 1})["pricing"]["ready"]
    def fail(_):
        raise RuntimeError("secret access token")
    monkeypatch.setattr(service, "_inspect", fail)
    row = dispatch("refresh", {"link_id": 1})
    assert not row["pricing"]["ready"] and row["pricing"]["official_reference_price"] is None
    assert "secret" not in row["error"]


def test_cost_change_marks_old_confirmation_as_history(workflow):
    _, _, _, dispatch = workflow
    preview = dispatch("preview", {"link_id": 1})
    dispatch("confirm", {"preview_id": preview["preview_id"], "confirmed": True})
    assert not dispatch("costs", {"link_id": 1, **costs(fixed_cost=51)})["approval"]["matches_current"]
