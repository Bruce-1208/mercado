import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from erp import mercadolibre_publication_preflight as contract
from erp.mercadolibre_follow_sell import follow_sell, MercadoLibreError


class Client:
    def __init__(self, schema=None, site="MLM"):
        self.schema = schema if schema is not None else [
            {"id": "BRAND", "tags": {"required": True}, "value_type": "string", "value_max_length": 30},
            {"id": "COLOR", "value_type": "list", "tags": {"allow_variations": True},
             "values": [{"id": "black", "name": "Black"}, {"id": "white", "name": "White"}]},
        ]
        self.site = site
        self.posts = []

    def request(self, method, path, **kwargs):
        if path == "/users/me":
            return {"id": 77, "site_id": self.site}
        if path.endswith("/attributes"):
            return copy.deepcopy(self.schema)
        if method == "POST" and path == "/items":
            self.posts.append(copy.deepcopy(kwargs["json_body"]))
            return {"id": "MLM555"}
        raise AssertionError((method, path))


def source():
    return {"id": "MLM123", "site_id": "MLM", "category_id": "MLM1", "title": "Sample",
            "price": 30, "currency_id": "MXN", "condition": "new",
            "attributes": [{"id": "BRAND", "value_name": "Actual brand"}],
            "pictures": [{"source": "https://example.com/photo.jpg"}]}


def call(client, value=None, **kwargs):
    return follow_sell(client, "MLM123", prepared_listing=(value or source(), {}), **kwargs)


def test_local_path_blocks_all_missing_fields_before_create_and_keeps_evidence():
    client = Client([{"id": key, "tags": {"required": True}} for key in ("BRAND", "MODEL", "GTIN")])
    item = source()
    item["attributes"] = []
    with pytest.raises(MercadoLibreError) as error:
        call(client, item, publish=True)
    assert not client.posts
    assert error.value.preflight_report["status"] == "needs_completion"
    assert {row["attribute_id"] for row in error.value.preflight_report["issues"]} == {"BRAND", "MODEL", "GTIN"}
    assert error.value.publication_draft["source"] == item


def test_unknown_optional_attribute_has_a_visible_mapping_report():
    item = source()
    item["attributes"].append({"id": "UNKNOWN", "value_name": "collected"})
    result = call(Client(), item)
    assert result["preflight"]["status"] == "ready"
    assert any(row["code"] == "unsupported_attribute" for row in result["preflight"]["issues"])
    assert result["draft"]["source"] == item
    assert any(row["target"] is None for row in result["preflight"]["mappings"])


def test_id_and_name_conflict_cannot_be_hidden_by_id_matching():
    item = source()
    item["attributes"].append({"id": "COLOR", "value_id": "black", "value_name": "White"})
    client = Client()
    with pytest.raises(MercadoLibreError) as error:
        call(client, item, publish=True)
    assert any(row["code"] == "id_name_conflict" for row in error.value.preflight_report["issues"])
    assert not client.posts


@pytest.mark.parametrize("attribute,definition,code", [
    ({"id": "WEIGHT", "value_name": "5 bananas"}, {"id": "WEIGHT", "value_type": "number_unit", "allowed_units": [{"id": "g"}]}, "invalid_unit"),
    ({"id": "COUNT", "value_name": "NaN"}, {"id": "COUNT", "value_type": "number"}, "invalid_number"),
    ({"id": "MODEL", "value_name": "123456"}, {"id": "MODEL", "value_type": "string", "value_max_length": 5}, "invalid_length"),
    ({"id": "COLOR", "value_name": "Black"}, {"id": "COLOR", "tags": {"read_only": True}}, "read_only"),
])
def test_format_constraints_block_create(attribute, definition, code):
    item = source()
    item["attributes"].append(attribute)
    client = Client([{"id": "BRAND"}, definition])
    with pytest.raises(MercadoLibreError) as error:
        call(client, item, publish=True)
    assert code in {row["code"] for row in error.value.preflight_report["issues"]}
    assert not client.posts


@pytest.mark.parametrize("dimension", ["UNKNOWN", "COLOR"])
def test_lost_or_unmapped_variation_dimensions_never_reach_platform(dimension):
    item = source()
    item["variations"] = [{"seller_sku": "sku-1", "attribute_combinations": [{"id": dimension, "value_name": "not-mappable"}]}]
    client = Client()
    with pytest.raises(MercadoLibreError) as error:
        call(client, item, publish=True)
    assert error.value.preflight_report["status"] == "mapping_conflict"
    assert any("SKU" in row["scope"] for row in error.value.preflight_report["issues"])
    assert not client.posts


def test_duplicate_sku_combinations_block_without_merging_stock():
    item = source()
    item["variations"] = [
        {"seller_sku": sku, "available_quantity": stock,
         "attribute_combinations": [{"id": "COLOR", "value_name": "Black"}]}
        for sku, stock in [("sku-1", 2), ("sku-2", 9)]
    ]
    client = Client()
    with pytest.raises(MercadoLibreError, match="不能合并库存"):
        call(client, item, publish=True)
    assert not client.posts
    assert [v["available_quantity"] for v in item["variations"]] == [2, 9]


def test_preview_and_submit_use_identical_payload_and_checkpoint_before_post():
    client = Client()
    preview = call(client)
    checkpoints = []
    def checkpoint(draft):
        assert not client.posts
        checkpoints.append(draft)
        if "submission_payload" in draft:
            draft["submission_payload"]["title"] = "callback cannot mutate submission"
    result = call(client, prepared_draft=preview["draft"], publish=True, on_prepared=checkpoint)
    assert client.posts == [preview["payload"]]
    assert result["submission_hash"] == contract.digest(client.posts[0])
    assert checkpoints[0]["revision"] == preview["draft"]["revision"]


@pytest.mark.parametrize("change", ["product", "quantity", "schema", "tamper", "expired"])
def test_draft_invalidates_on_all_material_changes(change, monkeypatch):
    client = Client()
    draft = call(client)["draft"]
    item = source()
    kwargs = {}
    if change == "product":
        item["attributes"][0]["value_name"] = "Changed brand"
    elif change == "quantity":
        kwargs["quantity"] = 2
    elif change == "schema":
        client.schema.append({"id": "MODEL", "tags": {"required": True}})
    elif change == "tamper":
        draft["payload"]["title"] = "Tampered"
    else:
        monkeypatch.setattr(contract.time, "time", lambda: draft["created_at"] + 901)
    with pytest.raises(MercadoLibreError, match="重新预检"):
        call(client, item, prepared_draft=draft, publish=True, **kwargs)
    assert not client.posts


def test_checkpoint_failure_stops_remote_publication():
    client = Client()
    def fail(_):
        raise RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable") as error:
        call(client, publish=True, on_prepared=fail)
    assert not client.posts
    assert error.value.publication_draft["revision"]


def test_concurrent_reports_do_not_mix_source_evidence():
    def run(index):
        item = source()
        item["attributes"][0]["value_name"] = f"Brand {index}"
        return call(Client(), item)["preflight"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(pool.map(run, range(8)))
    for index, report in enumerate(reports):
        assert {r["source"].get("value_name") for r in report["mappings"]} == {f"Brand {index}"}
    assert contract.ACTIVE_REPORT.get() is None


def test_exact_unit_conversion_preserves_original_evidence():
    client = Client([{"id": "BRAND"}, {"id": "WEIGHT", "value_type": "number_unit",
                     "allowed_units": [{"id": "g"}]}])
    item = source()
    item["attributes"].append({"id": "WEIGHT", "value_name": "1.25 kg"})
    preview = call(client, item)
    weight = next(a for a in preview["payload"]["attributes"] if a["id"] == "WEIGHT")
    assert weight["value_struct"] == {"number": 1250.0, "unit": "g"}
    assert item["attributes"][-1]["value_name"] == "1.25 kg"
    assert any(m["rule"] == "exact_unit_conversion" for m in preview["preflight"]["mappings"])


def test_explicit_correction_is_scoped_to_draft_and_keeps_original_snapshot():
    client = Client([{"id": "BRAND"}, {"id": "MODEL", "tags": {"required": True}}])
    item = source()
    corrections = [{"category_id": "MLM1", "attribute_id": "MODEL", "sku_index": None,
                    "value_name": "M-123", "evidence": "包装标签"}]
    preview = call(client, item, attribute_corrections=corrections)
    assert preview["preflight"]["status"] == "ready"
    assert preview["draft"]["source"] == item
    assert not any(a["id"] == "MODEL" for a in item["attributes"])
    assert preview["draft"]["context"]["attribute_corrections"] == corrections
    call(client, item, prepared_draft=preview["draft"], publish=True)
    assert any(a.get("value_name") == "M-123" for a in client.posts[0]["attributes"])


@pytest.mark.parametrize("change", ["evidence", "category_id", "value_name"])
def test_invalid_manual_corrections_never_bypass_checks(change):
    client = Client([{"id": "BRAND"}, {"id": "MODEL", "tags": {"required": True},
                     "value_max_length": 5}])
    correction = {"category_id": "MLM1", "attribute_id": "MODEL", "value_name": "M1", "evidence": "标签"}
    correction[change] = {"evidence": "", "category_id": "MLM999", "value_name": "TOO-LONG"}[change]
    with pytest.raises((ValueError, MercadoLibreError)):
        call(client, publish=True, attribute_corrections=[correction])
    assert not client.posts


def test_manual_correction_choices_come_from_failed_draft_schema():
    client = Client([{"id": "BRAND"}, {"id": "MODEL", "name": "型号", "tags": {"required": True}}])
    with pytest.raises(MercadoLibreError) as error:
        call(client)
    fields = contract.correction_fields(error.value.publication_draft)
    assert fields == [{"category_id": "MLM1", "attribute_id": "MODEL", "sku_index": None,
                       "scope": "商品", "name": "型号", "combination": False, "choices": []}]


def test_expired_retry_reuses_evidence_but_checks_new_rules(monkeypatch):
    client = Client([{"id": "BRAND"}, {"id": "MODEL", "tags": {"required": True}}])
    preview = call(client, attribute_corrections=[{"category_id":"MLM1", "attribute_id":"MODEL",
                    "value_name":"M-123", "evidence":"包装标签"}])
    monkeypatch.setattr(contract.time, "time", lambda: preview["draft"]["created_at"] + 901)
    # Evidence can be used to prepare a new revision, but the old submission is expired.
    retry = call(client, prior_draft=preview["draft"])
    assert retry["draft"]["revision"] != preview["draft"]["revision"]
    client.schema[1]["value_max_length"] = 2
    client._category_schema_cache = {}
    with pytest.raises(MercadoLibreError, match="长度"):
        call(client, prior_draft=preview["draft"], publish=True)
    assert not client.posts


def test_retry_cannot_apply_manual_facts_to_changed_source():
    client = Client()
    draft = call(client, attribute_corrections=[{"category_id":"MLM1", "attribute_id":"BRAND",
                  "value_name":"Confirmed brand", "evidence":"包装标签"}])["draft"]
    item = source()
    item["title"] = "Different product"
    with pytest.raises(ValueError, match="商品资料已变化"):
        call(client, item, prior_draft=draft, publish=True)
    assert not client.posts
