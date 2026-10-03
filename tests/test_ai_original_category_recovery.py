"""Category fallback, confirmed overrides and stage checkpoints, without network."""

import json
from copy import deepcopy

import pytest

from erp import ai_original_products as products


class DiscoveryClient:
    def __init__(self, predictions=None, schema=None):
        self.predictions = predictions or {}
        self.schema = schema if schema is not None else []
        self.calls = []

    def request(self, method, path, **kwargs):
        assert method == "GET"
        self.calls.append((path, kwargs))
        if path == "/marketplace/domain_discovery/search":
            return deepcopy(self.predictions.get(kwargs["params"]["q"], []))
        assert path.startswith("/categories/CBT") and path.endswith("/attributes")
        return deepcopy(self.schema)


@pytest.fixture(autouse=True)
def reject_real_network(monkeypatch):
    def reject(*args, **kwargs):
        pytest.fail("Category recovery tests must not use the network")
    monkeypatch.setattr("requests.sessions.Session.request", reject)


@pytest.mark.parametrize("title,refined,category_id", [
    ("Halloween LED string lights", "string lights", "CBT101"),
    ("Children's Venom Costume with Gloves", "children costume", "CBT102"),
])
def test_first_empty_recommendation_can_recover_with_generic_entity(title, refined, category_id):
    client = DiscoveryClient({refined: [{"category_id": category_id, "category_name": refined}]})
    responses = iter([
        {"category_id": "", "search_query": refined},
        {"category_id": category_id},
        {"attributes": []},
    ])
    prompts = []

    def chat(messages, **kwargs):
        prompts.append(messages[0]["content"])
        return json.dumps(next(responses))

    result = products.complete_marketplace_category(
        {"title": title, "category_id": "CBT_OLD"}, {"product_type_en": title}, client, chat=chat,
    )

    assert result["category_id"] == category_id
    assert result["category_prediction_query"] == refined
    assert result["category_search_history"] == [title, refined]
    assert "不能改变商品类型" in prompts[0]
    assert "searched_queries" in prompts[1]
    assert [call[1]["params"]["q"] for call in client.calls if "domain_discovery" in call[0]] == [title, refined]


def test_exhausted_search_is_bounded_and_never_keeps_an_old_category():
    client = DiscoveryClient()
    model_calls = []

    def chat(*args, **kwargs):
        model_calls.append(1)
        return json.dumps({"category_id": "CBT_OLD", "search_query": f"light fixture {len(model_calls)}"})

    with pytest.raises(products.OriginalPreparationError, match="请人工选择分类") as error:
        products.complete_marketplace_category(
            {"category_id": "CBT_OLD"}, {"product_type_en": "Halloween lamp"}, client, chat=chat,
        )

    assert error.value.step == "category"
    assert "category_id" not in error.value.state
    assert error.value.state["category_search_history"] == [
        "Halloween lamp", "light fixture 1", "light fixture 2", "light fixture 3", "light fixture 4",
    ]
    assert len(client.calls) == 5
    assert len(model_calls) == 5


@pytest.mark.parametrize("refined", ["  STRING    Lights  ", "灯串", "12345"])
def test_repeated_or_non_english_queries_are_not_sent(refined):
    client = DiscoveryClient()
    calls = []

    def chat(*args, **kwargs):
        calls.append(1)
        return json.dumps({"category_id": "", "search_query": refined})

    with pytest.raises(products.OriginalPreparationError) as error:
        products.complete_marketplace_category({}, {"product_type_en": "string lights"}, client, chat=chat)

    assert error.value.state["category_search_history"] == ["string lights"]
    assert len(client.calls) == 1
    assert len(calls) == 5


def test_confirmed_override_skips_discovery_and_reads_current_schema():
    client = DiscoveryClient(schema=[{"id": "COLOR"}, {"id": "INTERNAL", "tags": {"read_only": True}}])
    result = products.complete_marketplace_category(
        {"category_id": "CBT_OLD"}, {}, client,
        category_override={"category_id": "CBT105", "category_name": "Costumes"},
        chat=lambda *a, **k: json.dumps({"attributes": [
            {"id": "COLOR", "value_name": "Black"},
            {"id": "OLD_ATTRIBUTE", "value_name": "old"},
            {"id": "INTERNAL", "value_name": "private"},
        ]}),
    )

    assert client.calls == [("/categories/CBT105/attributes", {})]
    assert result["category_id"] == "CBT105"
    assert result["category_prediction_query"] == ""
    assert result["category_search_history"] == []
    assert result["attributes"] == [{"id": "COLOR", "name": "COLOR", "value_name": "Black"}]


def test_invalid_override_is_rejected_before_any_api_call():
    client = DiscoveryClient()
    with pytest.raises(products.OriginalPreparationError, match="分类编号无效"):
        products.complete_marketplace_category({}, {}, client, category_override={"category_id": "MLM105"})
    assert client.calls == []


def test_category_checkpoint_survives_a_later_variation_failure(monkeypatch):
    schema = [{"id": "COLOR"}]
    client = DiscoveryClient(schema=schema)
    checkpoints = []

    def fail_variations(*args, **kwargs):
        raise ValueError("规格无法映射")

    monkeypatch.setattr(products, "normalize_marketplace_variations", fail_variations)
    with pytest.raises(products.OriginalPreparationError, match="规格无法映射") as error:
        products.complete_marketplace_category(
            {}, {}, client, category_override={"category_id": "CBT105"},
            chat=lambda *a, **k: '{"attributes": [{"id": "COLOR", "value_name": "Black"}]}',
            on_category_ready=checkpoints.append,
        )

    assert error.value.step == "variations"
    assert len(checkpoints) == 2
    assert checkpoints[-1] == error.value.state
    assert checkpoints[0]["schema_fingerprint"] == products._preparation_fingerprint(schema)
    assert checkpoints[0]["attributes_ready"] is False
    assert checkpoints[-1]["attributes"][0]["value_name"] == "Black"


def test_variation_checkpoint_is_written_before_required_completion(monkeypatch):
    checkpoints = []
    variations = [{"id": "sku-a", "price": 15, "attribute_combinations": [{"id": "COLOR", "value_name": "Black"}]}]
    monkeypatch.setattr(products, "normalize_marketplace_variations", lambda *a, **k: deepcopy(variations))

    def fail_required(*args, **kwargs):
        assert checkpoints == [{"variations": variations}]
        raise ValueError("missing required")

    monkeypatch.setattr(products, "complete_required_attributes", fail_required)
    with pytest.raises(products.OriginalPreparationError, match="missing required") as error:
        products.complete_marketplace_category(
            {}, {}, DiscoveryClient(schema=[{"id": "COLOR"}]),
            category_override={"category_id": "CBT105"},
            chat=lambda *a, **k: '{"attributes": []}', on_variations_ready=checkpoints.append,
        )
    assert error.value.step == "attributes"
    assert error.value.state["variations"] == variations
    assert error.value.state["variations_ready"] is True


def test_matching_cached_category_reuses_attrs_and_variations_after_reading_schema(monkeypatch):
    schema = [{"id": "COLOR"}]
    cached = {
        "category_id": "CBT105", "schema_fingerprint": products._preparation_fingerprint(schema),
        "category_prediction_query": "string lights", "category_search_history": ["holiday lights", "string lights"],
        "attributes": [{"id": "COLOR", "value_name": "Black"}, {"id": "UNWRITABLE", "value_name": "x"}],
        "variations": [{"id": "a", "price": 15, "available_quantity": 4}], "variations_ready": True,
    }
    before = deepcopy(cached)
    client = DiscoveryClient(schema=schema)
    monkeypatch.setattr(products, "normalize_marketplace_variations", lambda *a, **k: pytest.fail("already mapped"))
    result = products.complete_marketplace_category(
        {}, {}, client, category_override={"category_id": "CBT105"}, cached_result=cached,
        chat=lambda *a, **k: pytest.fail("Completed cached stages must not call the model"),
    )

    assert client.calls == [("/categories/CBT105/attributes", {})]
    assert result["attributes"] == [{"id": "COLOR", "name": "COLOR", "value_name": "Black"}]
    assert result["variations"] == cached["variations"]
    assert result["variations"] is not cached["variations"]
    assert result["category_search_history"] == cached["category_search_history"]
    assert cached == before


@pytest.mark.parametrize("changed", ["schema", "category"])
def test_changed_schema_or_category_invalidates_cached_mapping(monkeypatch, changed):
    cached = {
        "category_id": "CBT_OTHER" if changed == "category" else "CBT105",
        "schema_fingerprint": products._preparation_fingerprint([] if changed == "schema" else [{"id": "COLOR"}]),
        "attributes": [{"id": "COLOR", "value_name": "Stale"}],
        "variations": [{"id": "stale"}], "variations_ready": True,
    }
    calls = []
    monkeypatch.setattr(products, "normalize_marketplace_variations", lambda *a, **k: calls.append("variations") or [])

    def chat(*args, **kwargs):
        calls.append("attributes")
        return '{"attributes": [{"id": "COLOR", "value_name": "Fresh"}]}'

    result = products.complete_marketplace_category(
        {}, {}, DiscoveryClient(schema=[{"id": "COLOR"}]),
        category_override={"category_id": "CBT105"}, cached_result=cached, chat=chat,
    )
    assert calls == ["attributes", "variations"]
    assert result["attributes"][0]["value_name"] == "Fresh"
    assert result["variations"] == []


def test_model_account_error_is_not_changed_into_a_category_failure():
    account_error = products.OriginalModelAccountError("insufficient model balance")

    def chat(*args, **kwargs):
        raise account_error

    with pytest.raises(products.OriginalModelAccountError) as error:
        products.complete_marketplace_category({}, {"product_type_en": "string lights"}, DiscoveryClient(), chat=chat)
    assert error.value is account_error


def test_retry_on_the_same_client_reads_live_schema_again(monkeypatch):
    client = DiscoveryClient(schema=[{"id": "COLOR"}])
    callbacks = []
    mappings = []
    monkeypatch.setattr(products, "normalize_marketplace_variations", lambda *a, **k: mappings.append(1) or [])
    first = products.complete_marketplace_category(
        {}, {}, client, category_override={"category_id": "CBT105"},
        chat=lambda *a, **k: '{"attributes": [{"id": "COLOR", "value_name": "Black"}]}',
        on_category_ready=callbacks.append,
    )
    cached = {**first, "variations_ready": True}
    client.schema = [{"id": "MODEL"}]
    result = products.complete_marketplace_category(
        {}, {}, client, category_override={"category_id": "CBT105"}, cached_result=cached,
        chat=lambda *a, **k: '{"attributes": [{"id": "MODEL", "value_name": "Leather version"}]}',
    )

    assert client.calls == [("/categories/CBT105/attributes", {})] * 2
    assert len(mappings) == 2
    assert result["schema_fingerprint"] != callbacks[0]["schema_fingerprint"]
    assert result["attributes"] == [{"id": "MODEL", "name": "MODEL", "value_name": "Leather version"}]


def test_malformed_category_id_from_model_does_not_escape_the_bounded_fallback():
    with pytest.raises(products.OriginalPreparationError) as error:
        products.complete_marketplace_category(
            {}, {"product_type_en": "costume"}, DiscoveryClient(),
            chat=lambda *a, **k: '{"category_id": [], "search_query": "costume"}',
        )
    assert error.value.step == "category"
    assert error.value.state["category_search_history"] == ["costume"]


def test_explicit_manual_attrs_override_model_values_under_current_category_rules():
    client = DiscoveryClient(schema=[
        {"id": "COLOR", "value_type": "list", "tags": {"required": True}, "values": [
            {"id": "black", "name": "Black"}, {"id": "red", "name": "Red"},
        ]},
        {"id": "INTERNAL", "tags": {"read_only": True}},
    ])
    manual = [
        {"id": "COLOR", "value_id": "red"},
        {"id": "OLD_ATTRIBUTE", "value_name": "Old category only"},
        {"id": "INTERNAL", "value_name": "Read only"},
    ]
    result = products.complete_marketplace_category(
        {}, {}, client, category_override={"category_id": "CBT105"}, attribute_overrides=manual,
        chat=lambda *a, **k: '{"attributes": [{"id": "COLOR", "value_name": "Black"}]}',
    )

    assert result["attributes"] == [{"id": "COLOR", "name": "COLOR", "value_id": "red", "value_name": "Red"}]
    assert manual[0] == {"id": "COLOR", "value_id": "red"}


def test_manual_attr_overrides_cannot_bypass_required_enum_validation():
    client = DiscoveryClient(schema=[
        {"id": "COLOR", "value_type": "list", "tags": {"required": True},
         "values": [{"id": "black", "name": "Black"}]},
    ])
    with pytest.raises(products.OriginalPreparationError, match="必填属性 COLOR"):
        products.complete_marketplace_category(
            {}, {}, client, category_override={"category_id": "CBT105"},
            attribute_overrides=[{"id": "COLOR", "value_id": "unknown", "value_name": "Unknown"}],
            chat=lambda *a, **k: '{"attributes": [{"id": "COLOR", "value_name": "Black"}]}',
        )


def test_explicitly_deleted_optional_attr_is_not_resurrected_by_cached_or_generated_values():
    schema = [{"id": "COLOR"}, {"id": "MODEL"}]
    attrs = [{"id": "COLOR", "value_name": "Black"}, {"id": "MODEL", "value_name": "Costume"}]
    for cached in (None, {"category_id": "CBT105", "schema_fingerprint": products._preparation_fingerprint(schema),
                          "attributes": attrs, "variations": [], "variations_ready": True}):
        result = products.complete_marketplace_category(
            {}, {}, DiscoveryClient(schema=schema), category_override={"category_id": "CBT105"},
            cached_result=cached, attribute_overrides=attrs[1:], attribute_removals=["COLOR"],
            chat=lambda *a, **k: json.dumps({"attributes": attrs}),
        )
        assert [item["id"] for item in result["attributes"]] == ["MODEL"]
