from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
import requests

from erp.mercadolibre_size_chart_store import SizeChartStore
from mercado_api.size_charts import MercadoSizeChartsClient, SizeChartWriteError, extract_fields


FULL_SPECS = {"input": {"groups": [{"id": "MAIN", "components": [
    {"component": "COMBO", "attributes": [{"id": "GENDER", "name": "Gender", "value_type": "string",
        "tags": ["grid_template_required", "grid_filter", "fixed", "required"], "values": [{"id": "1", "name": "Man"}]}]},
    {"component": "TEXT_INPUT", "attributes": [{"id": "MODEL", "tags": ["required"]},
                                                  {"id": "ITEM_CONDITION", "tags": ["required"]}]}]}]}}
GRID_SPECS = {"input": {"groups": [{"id": "SIZE_CHART", "section": "GRIDS", "components": [
    {"component": "GRID", "ui_config": {"max_allowed": 75}, "components": [
        {"component": "TEXT_OUTPUT", "attributes": [{"id": "BRAND", "name": "Brand", "value_type": "string",
            "tags": ["grid_filter", "required"], "values": [{"id": "known", "name": "Known Brand"}]}]},
        {"component": "TEXT_OUTPUT", "attributes": [{"id": "GENDER", "name": "Gender", "value_type": "string",
            "tags": ["grid_template_required", "grid_filter", "fixed", "required"], "values": [{"id": "1", "name": "Man"}]}]},
        {"component": "COMBO", "ui_config": {"max_allowed": 3}, "attributes": [{"id": "M_US_SIZE", "name": "US Size",
            "value_type": "list", "tags": ["required", "main_attribute_candidate", "unique"],
            "values": [{"id": "s7", "name": "7"}, {"id": "s8", "name": "8"}]}]},
        {"component": "NUMBER_UNIT_INPUT", "attributes": [{"id": "FOOT_LENGTH", "name": "Foot Length",
            "value_type": "number_unit", "units": [{"id": "cm", "name": "cm"}], "tags": ["required", "BODY_MEASURE"]}]},
        {"component": "NUMBER_UNIT_INPUT", "attributes": [{"id": "GARMENT_LENGTH_FROM", "name": "Garment Length",
            "value_type": "number_unit", "units": [{"id": "cm", "name": "cm"}], "tags": ["required", "CLOTHING_MEASURE"]}]}]}]}]}}


def draft():
    return {"name": "Local Template", "domain_id": "SNEAKERS", "names": {"CBT": "Size Chart 1", "MLM": "Mexico Size Chart", "MLB": "Brazil Size Chart"},
            "attributes": [{"id": "GENDER", "values": [{"id": "1", "name": "Man"}]},
                           {"id": "BRAND", "values": [{"name": "My Custom Brand"}]}],
            "main_attribute": [{"site_id": "CBT", "id": "M_US_SIZE"}],
            "rows": [{"attributes": [{"id": "M_US_SIZE", "values": [{"id": "s7", "name": "7"}]},
                                     {"id": "FOOT_LENGTH", "values": [{"name": "25 cm", "struct": {"number": 25, "unit": "cm"}}]}]}]}


class FakeResponse:
    def __init__(self, status=201, payload=None, bad_json=False):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.payload = payload or {"id": "chart-1", "rows": [{"id": "row-1"}]}
        self.bad_json = bad_json

    def json(self):
        if self.bad_json:
            raise ValueError("bad JSON")
        return deepcopy(self.payload)


class FakeSession:
    def __init__(self):
        self.calls = []
        self.error = None
        self.response = FakeResponse()

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        if self.error:
            raise self.error
        return self.response


class FakeClient:
    BASE_URL = "https://api.example.test"
    access_token = "not-a-real-token"
    timeout = 1

    def __init__(self, seller_id=101, site_id="CBT"):
        self.session = FakeSession()
        self.seller_id = seller_id
        self.site_id = site_id
        self.calls = []
        self.empty_grids = False
        self.chart = None

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        if path == "/users/me":
            return {"id": self.seller_id, "site_id": self.site_id}
        if path.startswith("/marketplace/users/"):
            return {"marketplaces": [{"site_id": "MLM", "user_id": 201}, {"site_id": "MLB", "user_id": 202},
                                     {"site_id": "MLM", "user_id": 203}]}
        if path.endswith("/configurations/active_domains"):
            return {"domains": [{"domain_id": self.site_id + "-SNEAKERS"}]}
        if path.endswith("/technical_specs"):
            return deepcopy({} if self.empty_grids else GRID_SPECS) if method == "POST" else deepcopy(FULL_SPECS)
        if path.startswith("/catalog/charts/") and self.chart:
            return deepcopy(self.chart)
        raise AssertionError((method, path))


@pytest.fixture
def service(tmp_path, monkeypatch):
    from bit import size_charts
    summaries = [
        {"id": 1, "organization_key": "org-a", "display_name": "Store A", "meli_user_id": 101, "enabled": True},
        {"id": 2, "organization_key": "org-b", "display_name": "Store B", "meli_user_id": 102, "enabled": True},
        {"id": 3, "organization_key": "org-a", "display_name": "Closed", "meli_user_id": 103, "enabled": False},
    ]
    clients = {row["id"]: FakeClient(row["meli_user_id"]) for row in summaries}
    monkeypatch.setattr(size_charts.bit_mysql, "list_mercado_store_tokens", lambda: {"rows": deepcopy(summaries)})
    monkeypatch.setattr(size_charts.bit_mysql, "get_mercado_store_token", lambda token_id: deepcopy(next(row for row in summaries if row["id"] == token_id)))
    monkeypatch.setattr(size_charts, "_client_and_token", lambda secret: (clients[secret["id"]], secret))
    store = SizeChartStore(tmp_path / "size-charts.sqlite3")
    def call(action, payload=None, allowed=(1, 3), org="org-a"):
        return size_charts.dispatch(action, payload, organization_key=org, allowed_token_ids=allowed, store=store)
    return call, store, clients, summaries


def create_preview(call, targets=None, template=None):
    saved = call("create", template or draft())["template"]
    preview = call("preview", {"template_id": saved["id"], "revision": saved["revision"],
                               "targets": targets or [{"token_id": 1, "site_id": "MLM"}, {"token_id": 1, "site_id": "MLB"}]})
    return saved, preview


def test_official_nested_schema_preserves_grid_units_candidates_and_row_limit():
    meta = extract_fields(GRID_SPECS)
    assert meta["max_rows"] == 75
    assert {row["id"] for row in meta["template_fields"]} == {"GENDER", "BRAND"}
    assert [row["id"] for row in meta["main_attributes"]] == ["M_US_SIZE"]
    assert next(row for row in meta["row_fields"] if row["id"] == "FOOT_LENGTH")["allowed_units"] == [{"id": "cm", "name": "cm"}]


def test_requirements_uses_full_domain_and_ignores_unrelated_listing_requirements(service):
    call, _, clients, _ = service
    first = call("requirements", {"token_id": 1, "domain_id": "CBT-SNEAKERS"})
    assert first["errors"] and not any(method == "POST" for method, _, _ in clients[1].calls)
    complete = call("requirements", {"token_id": 1, "domain_id": "SNEAKERS", "attributes": draft()["attributes"]})
    assert {row["id"] for row in complete["template_fields"]} == {"GENDER", "BRAND"}
    method, path, kwargs = clients[1].calls[-1]
    assert method == "POST" and path == "/domains/CBT-SNEAKERS/technical_specs"
    assert kwargs["params"] == {"section": "grids"}
    assert kwargs["json_body"] == {"attributes": [{"id": "GENDER", "value_id": "1"}]}


def test_output_mirror_cannot_erase_input_validation():
    specs = {"input": {"attributes": [{"id": "GENDER", "tags": ["grid_template_required"],
             "value_type": "list", "values": [{"id": "1", "name": "Man"}]}]},
             "output": {"attributes": [{"id": "GENDER", "value_type": "list", "tags": []}]}}
    field = extract_fields(specs)["template_fields"][0]
    assert field["required"] and "grid_template_required" in field["tags"]
    assert field["values"] == [{"id": "1", "name": "Man"}]


def test_store_only_preview_defaults_to_all_verified_sites(service):
    call, _, clients, _ = service
    saved = call("create", draft())["template"]
    preview = call("preview", {"template_id": saved["id"], "revision": 1, "token_ids": [1]})
    assert preview["results"][0]["sites"] == ["MLB", "MLM"]
    result = call("upload", {"preview_id": preview["preview_id"]})
    assert result["succeeded"] == 1
    assert clients[1].session.calls[0][2]["json"]["rows"][0]["sites"] == ["CBT", "MLB", "MLM"]


def test_legacy_site_selection_also_covers_all_sites(service):
    call, _, _, _ = service
    _, preview = create_preview(call, targets=[{"token_id": 1, "site_id": "MLM"}])
    assert preview["results"][0]["sites"] == ["MLB", "MLM"]


def test_batch_seeds_once_and_keeps_local_edits(service):
    from bit.size_chart_batch import batch_templates
    call, storage, _, _ = service
    first = batch_templates(storage, "org-a")
    assert len(first) == 21
    selected = first[0]
    edited = call("update", {"template_id": selected["id"], "revision": 1,
                           "template": {**selected, "name": "Edited reference"}})["template"]
    second = batch_templates(storage, "org-a")
    assert len(second) == 21 and len(storage.list_templates("org-a")) == 21
    assert next(t for t in second if t["id"] == edited["id"])["name"] == "Edited reference"


def test_reference_upgrade_does_not_replace_modified_waist_templates(service):
    import json
    from pathlib import Path
    from bit.size_chart_batch import batch_templates
    call, storage, _, _ = service
    library = json.loads((Path(__file__).parents[1] / "bit" / "size-chart-builtins.json").read_text())
    old = library["previous_templates"][0]
    original = call("create", old)["template"]
    custom = call("create", {**old, "name": "Custom brand measurements"})["template"]
    batch_templates(storage, "org-a")
    updated = storage.get_template("org-a", original["id"])
    assert updated["revision"] == 2 and updated["standard_template"]["size_system"] == "alpha"
    preserved = storage.get_template("org-a", custom["id"])
    assert preserved["revision"] == 1 and preserved["rows"] == old["rows"]


def test_reference_sleepwear_maps_body_chest_field_without_changing_measurements():
    from bit.size_charts import _prepare_reference_template
    source = {"standard_template": {"preset_id": "builtin_v1_women_pajamas", "size_system": "alpha"},
              "attributes": [], "rows": [{"attributes": [{"id": "CHEST_CIRCUMFERENCE_FROM", "values": [
                  {"name": "76 cm", "struct": {"number": 76, "unit": "cm"}}]}]}]}
    requirements = {"template_fields": [], "row_fields": [{"id": "BUST_CIRCUMFERENCE_FROM", "tags": ["BODY_MEASURE"], "required": True}]}
    prepared, notices, errors = _prepare_reference_template(source, requirements)
    assert not errors
    assert prepared["rows"][0]["attributes"][0]["id"] == "BUST_CIRCUMFERENCE_FROM"
    assert prepared["rows"][0]["attributes"][0]["values"] == source["rows"][0]["attributes"][0]["values"]
    assert source["rows"][0]["attributes"][0]["id"] == "CHEST_CIRCUMFERENCE_FROM"
    assert any("胸围" in notice for notice in notices)


def test_child_reference_omits_only_optional_neck_and_preserves_source():
    from bit.size_charts import _prepare_reference_template
    source = {"standard_template": {"preset_id": "builtin_v1_girls_tshirts", "size_system": "age"}, "attributes": [],
              "rows": [{"attributes": [{"id": name, "values": [{"name": f"{value} cm", "struct": {"number": value, "unit": "cm"}}]}
                                        for name, value in [("CHEST_CIRCUMFERENCE_FROM", 50), ("NECK_CIRCUMFERENCE_FROM", 24)]]}]}
    requirements = {"template_fields": [], "row_fields": [{"id": "CHEST_CIRCUMFERENCE_FROM", "tags": ["BODY_MEASURE"], "required": True},
                    {"id": "NECK_CIRCUMFERENCE_FROM", "tags": ["BODY_MEASURE"], "required": False}]}
    prepared, notices, errors = _prepare_reference_template(source, requirements)
    assert not errors and len(source["rows"][0]["attributes"]) == 2
    assert [row["id"] for row in prepared["rows"][0]["attributes"]] == ["CHEST_CIRCUMFERENCE_FROM"]
    assert any("30–70" in notice for notice in notices)


def test_batch_scope_and_disabled_store_rejected_before_start(service, monkeypatch, tmp_path):
    call, _, _, _ = service
    monkeypatch.setenv("BIT_SIZE_CHART_TASK_ROOT", str(tmp_path / "tasks"))
    for token_id in (2, 3):
        with pytest.raises(ValueError):
            call("batch-upload", {"token_ids": [token_id]})


def test_batch_worker_retains_failures_and_continues(service, monkeypatch):
    from bit import size_charts
    from bit.size_chart_batch import run
    _, storage, _, _ = service
    templates = [{"id": 1, "revision": 1, "name": "First"}, {"id": 2, "revision": 1, "name": "Second"}]
    def fake(action, payload, **kwargs):
        if action == "preview":
            if payload["template_id"] == 1:
                return {"results": [{"status": "invalid", "errors": ["规格不支持"], "message": "规格不支持"}]}
            return {"preview_id": "verified", "results": [{"status": "ready"}]}
        return {"results": [{"status": "succeeded", "chart_id": "official-chart", "sites": ["MLB", "MLM"]}]}
    monkeypatch.setattr(size_charts, "dispatch", fake)
    class Tasks:
        updates = []
        def update(self, task_id, **updates): self.updates.append(updates)
    class Stop:
        def is_set(self): return False
    tasks = Tasks()
    result = run({"chart_db_path": str(storage.path), "organization_key": "org-a", "token_ids": [1],
                  "templates": templates}, {"task_id": "batch"}, tasks, Stop())
    assert result["completed"] == 2 and result["succeeded"] == 1 and result["failed"] == 1
    assert result["status"] == "partial" and result["results"][0]["errors"] == ["规格不支持"]
    assert result["results"][1]["chart_id"] == "official-chart"


def test_multisite_upload_is_one_create_with_verified_parent_identity_and_wrapped_main(service):
    call, _, clients, _ = service
    _, preview = create_preview(call)
    assert preview["results"][0]["status"] == "ready", preview
    result = call("upload", {"preview_id": preview["preview_id"], "seller_id": 999, "site_id": "MLB"})
    assert result["succeeded"] == 1
    assert len(clients[1].session.calls) == 1
    method, url, request = clients[1].session.calls[0]
    body = request["json"]
    assert method == "POST" and url.endswith("/catalog/charts")
    assert body["site_id"] == "CBT" and "seller_id" not in body and body["domain_id"] == "SNEAKERS"
    assert preview["results"][0]["payload"]["seller_id"] == 101
    assert body["type"] == "SPECIFIC"
    assert body["main_attribute"] == {"attributes": [{"site_id": site, "id": "M_US_SIZE"} for site in ("CBT", "MLB", "MLM")]}
    assert body["rows"][0]["sites"] == ["CBT", "MLB", "MLM"]
    assert set(body["names"]) == {"CBT", "MLB", "MLM"}
    records = call("records")["records"]
    assert records[0]["chart_id"] == "chart-1" and records[0]["rows"][0]["id"] == "row-1"
    assert records[0]["template_snapshot"]["revision"] == 1


def test_upload_repeated_and_new_preview_do_not_duplicate_create(service):
    call, _, clients, _ = service
    template, preview = create_preview(call)
    call("upload", {"preview_id": preview["preview_id"]})
    call("upload", {"preview_id": preview["preview_id"]})
    duplicate = call("preview", {"template_id": template["id"], "revision": 1, "targets": [{"token_id": 1, "site_id": "MLM"}]})
    assert duplicate["results"][0]["status"] == "duplicate"
    assert len(clients[1].session.calls) == 1


def test_uncertain_connection_and_server_errors_are_never_replayed(service):
    call, _, clients, _ = service
    _, preview = create_preview(call)
    clients[1].session.error = requests.ConnectionError("secret data must not appear")
    result = call("upload", {"preview_id": preview["preview_id"]})
    assert result["unknown"] == 1 and "secret data" not in result["results"][0]["message"]
    call("upload", {"preview_id": preview["preview_id"]})
    assert len(clients[1].session.calls) == 1


@pytest.mark.parametrize("response", [FakeResponse(503, {"message": "unavailable"}), FakeResponse(bad_json=True), FakeResponse(201, {"rows": []})])
def test_adapter_treats_unconfirmed_responses_as_unknown_once(response):
    client = FakeClient()
    client.session.response = response
    with pytest.raises(SizeChartWriteError) as caught:
        MercadoSizeChartsClient(client).create_chart({"names": {"CBT": "Test"}})
    assert caught.value.unknown and len(client.session.calls) == 1


def test_explicit_remote_rejection_is_durable_and_same_preview_cannot_retry(service):
    call, _, clients, _ = service
    template, preview = create_preview(call)
    clients[1].session.response = FakeResponse(400, {"error": "invalid_chart", "cause": [{"code": "bad_name"}]})
    result = call("upload", {"preview_id": preview["preview_id"]})
    assert result["failed"] == 1 and result["results"][0]["response"]["cause"][0]["code"] == "bad_name"
    call("upload", {"preview_id": preview["preview_id"]})
    assert len(clients[1].session.calls) == 1
    new_preview = call("preview", {"template_id": template["id"], "revision": 1,
                                   "targets": [{"token_id": 1, "site_id": "MLM"}, {"token_id": 1, "site_id": "MLB"}]})
    clients[1].session.response = FakeResponse()
    retried = call("upload", {"preview_id": new_preview["preview_id"]})
    assert retried["succeeded"] == 1 and retried["results"][0]["attempt"] == 2
    assert retried["results"][0]["previous_attempts"][0]["status"] == "failed"


def test_org_token_and_member_scope_fail_before_official_reads(service):
    call, _, clients, _ = service
    saved = call("create", draft())["template"]
    assert call("list", org="org-b", allowed=None)["templates"] == []
    with pytest.raises(ValueError, match="当前组织"):
        call("copy", {"template_id": saved["id"]}, org="org-b", allowed=None)
    for targets in ([{"token_id": 2, "site_id": "MLM"}], [{"token_id": 1, "site_id": "MLM"}]):
        with pytest.raises(ValueError, match="授权范围"):
            call("preview", {"template_id": saved["id"], "revision": 1, "targets": targets}, allowed=[])
    assert not clients[1].calls and not clients[2].calls
    assert {row["id"] for row in call("options", allowed=None)["stores"]} == {1, 3}


def test_token_disabled_changed_identity_or_unavailable_site_fail_closed(service):
    call, _, clients, summaries = service
    saved, _ = create_preview(call)
    closed = call("preview", {"template_id": saved["id"], "revision": 1, "targets": [{"token_id": 3, "site_id": "MLM"}]})
    assert closed["results"][0]["status"] == "invalid"
    sites = call("preview", {"template_id": saved["id"], "revision": 1, "targets": [{"token_id": 1, "site_id": "MLC"}]})
    assert sites["results"][0]["status"] == "invalid"
    clients[1].seller_id = 999
    mismatch = call("preview", {"template_id": saved["id"], "revision": 1, "targets": [{"token_id": 1, "site_id": "MLM"}]})
    assert "身份" in mismatch["results"][0]["message"]
    assert not clients[1].session.calls


def test_revision_and_revoked_scope_invalidate_frozen_preview(service):
    call, _, clients, _ = service
    saved, preview = create_preview(call)
    with pytest.raises(ValueError, match="授权范围已变更"):
        call("upload", {"preview_id": preview["preview_id"]}, allowed=[])
    changed = call("update", {"template_id": saved["id"], "revision": 1, "template": {**draft(), "name": "Changed"}})["template"]
    assert changed["revision"] == 2
    with pytest.raises(ValueError, match="修改或删除"):
        call("upload", {"preview_id": preview["preview_id"]})
    with pytest.raises(ValueError, match="已被修改"):
        call("delete", {"template_id": saved["id"], "revision": 1})
    assert not clients[1].session.calls


def test_invalid_schema_missing_gender_numeric_struct_and_duplicate_main_cannot_upload(service):
    call, _, clients, _ = service
    bad = draft()
    bad["attributes"] = [bad["attributes"][1]]
    _, missing = create_preview(call, template=bad)
    assert missing["results"][0]["status"] == "invalid"
    bad = draft()
    bad["rows"][0]["attributes"][1]["values"] = [{"name": "25 cm"}]
    bad["rows"].append(deepcopy(bad["rows"][0]))
    bad["rows"][1]["attributes"][0]["values"][0]["name"] = "different label same ID"
    _, numeric = create_preview(call, template=bad)
    assert any("数值及单位结构" in error for error in numeric["results"][0]["errors"])
    assert any("重复" in error for error in numeric["results"][0]["errors"])
    clients[1].empty_grids = True
    _, empty = create_preview(call)
    assert empty["results"][0]["status"] == "invalid"
    assert not clients[1].session.calls


def test_mixed_measure_and_clothing_conditional_required_fields(service):
    call, _, _, _ = service
    normal, preview = create_preview(call)
    assert preview["results"][0]["status"] == "ready"
    mixed = draft()
    mixed["measure_type"] = "MIXED_MEASURE"
    _, missing = create_preview(call, template=mixed)
    assert any("Garment Length" in error for error in missing["results"][0]["errors"])
    mixed["rows"][0]["attributes"].append({"id": "GARMENT_LENGTH_FROM", "values": [{"name": "30 cm", "struct": {"number": 30, "unit": "cm"}}]})
    _, valid = create_preview(call, template=mixed)
    assert valid["results"][0]["status"] == "ready" and valid["results"][0]["payload"]["measure_type"] == "MIXED_MEASURE"


def test_noninteger_and_boolean_token_ids_are_rejected(service):
    call, _, _, _ = service
    for token_id in (1.5, True, "1.0", "-1"):
        with pytest.raises(ValueError, match="编号无效"):
            call("requirements", {"token_id": token_id})


def test_local_token_only_uses_verified_site(service):
    call, _, clients, _ = service
    clients[1].site_id = "MLM"
    saved, preview = create_preview(call, targets=[{"token_id": 1, "site_id": "MLM"}])
    assert preview["results"][0]["status"] == "ready"
    body = preview["results"][0]["payload"]
    assert body["site_id"] == "MLM" and set(body["names"]) == {"MLM"}
    assert body["main_attribute"] == {"attributes": [{"site_id": "MLM", "id": "M_US_SIZE"}]}


def test_atomic_upload_reservation_is_shared_across_threads_and_survives_restart(tmp_path):
    path = tmp_path / "charts.sqlite3"
    store = SizeChartStore(path)
    record = {"template_id": 1, "revision": 1, "token_id": 10, "payload_hash": "hash", "sites": ["MLM"],
              "preview_id": "preview-a", "template_snapshot": {"rows": [{"value": "original"}]}}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.claim_upload("org-a", record), range(12)))
    assert sum(claimed for _, claimed in results) == 1
    assert len({row["record_id"] for row, _ in results}) == 1
    restarted = SizeChartStore(path)
    assert not restarted.claim_upload("org-a", record)[1]
    assert restarted.list_records("org-b") == []
    assert restarted.list_records("org-a", [99]) == []
    assert restarted.list_records("org-a", [10])[0]["template_snapshot"]["rows"][0]["value"] == "original"


def test_unknown_upload_can_be_reconciled_only_with_matching_official_chart(service):
    call, _, clients, _ = service
    _, preview = create_preview(call)
    clients[1].session.error = requests.Timeout("unknown")
    record = call("upload", {"preview_id": preview["preview_id"]})["results"][0]
    chart = {**deepcopy(record["payload"]), "id": "chart-9"}
    chart["rows"][0]["id"] = "chart-9:1"
    chart["rows"][0]["attributes"][1]["values"][0]["struct"]["number"] = 25.0
    chart["rows"][0]["attributes"].append({"id": "AUTOMATIC_SIZE_EQUIVALENCE", "values": [{"name": "extra official field"}]})
    chart["attributes"][1]["values"][0]["id"] = "officially-resolved-brand"
    clients[1].chart = chart
    reconciled = call("reconcile", {"record_id": record["record_id"], "chart_id": "chart-9"})
    assert reconciled["record"]["status"] == "succeeded" and reconciled["record"]["chart_id"] == "chart-9"
    assert len(clients[1].session.calls) == 1


@pytest.mark.parametrize("change", ["seller", "site", "domain", "name", "main", "row", "measurement", "brand", "inactive"])
def test_reconcile_rejects_wrong_chart_and_preserves_uncertain_audit(service, change):
    call, _, clients, _ = service
    _, preview = create_preview(call)
    clients[1].session.error = requests.Timeout("unknown")
    record = call("upload", {"preview_id": preview["preview_id"]})["results"][0]
    chart = {**deepcopy(record["payload"]), "id": "chart-9"}
    chart["rows"][0]["id"] = "chart-9:1"
    if change == "seller": chart["seller_id"] = 999
    if change == "site": chart["site_id"] = "MLM"
    if change == "domain": chart["domain_id"] = "PANTS"
    if change == "name": chart["names"]["MLM"] = "Another Chart"
    if change == "main": chart["main_attribute"]["attributes"][0]["id"] = "OTHER_SIZE"
    if change == "row": chart["rows"].append(deepcopy(chart["rows"][0]))
    if change == "measurement": chart["rows"][0]["attributes"][1]["values"][0]["struct"]["number"] = 30
    if change == "brand": chart["attributes"][1]["values"][0]["name"] = "Another Brand"
    if change == "inactive": chart["chart_status"] = "INACTIVE"
    clients[1].chart = chart
    with pytest.raises(ValueError, match="不一致"):
        call("reconcile", {"record_id": record["record_id"], "chart_id": "chart-9"})
    assert call("records")["records"][0]["status"] == "unknown"
    assert len(clients[1].session.calls) == 1


def test_reconcile_accepts_official_local_main_attribute_and_missing_row_sites(service):
    call, _, clients, _ = service
    clients[1].site_id = "MLM"
    _, preview = create_preview(call, targets=[{"token_id": 1, "site_id": "MLM"}])
    clients[1].session.error = requests.Timeout("unknown")
    record = call("upload", {"preview_id": preview["preview_id"]})["results"][0]
    chart = {**deepcopy(record["payload"]), "id": "chart-local"}
    chart.pop("main_attribute")
    chart["main_attribute_id"] = "M_US_SIZE"
    chart["rows"][0].pop("sites")
    chart["rows"][0]["id"] = "chart-local:1"
    clients[1].chart = chart
    assert call("reconcile", {"record_id": record["record_id"], "chart_id": "chart-local"})["record"]["status"] == "succeeded"


def test_copy_maximum_name_and_plain_number_validation(service):
    from bit.size_charts import _validate_attributes
    call, _, _, _ = service
    saved = call("create", {**draft(), "name": "A" * 120})["template"]
    assert len(call("copy", {"template_id": saved["id"]})["template"]["name"]) == 120
    fields = [{"id": "NUMBER", "name": "Number", "value_type": "number", "min": 1, "max": 10}]
    for text in ("abc", "NaN", "Infinity", "0", "11"):
        assert _validate_attributes([{"id": "NUMBER", "values": [{"name": text}]}], fields, label="Row")
    assert not _validate_attributes([{"id": "NUMBER", "values": [{"name": "2.5"}]}], fields, label="Row")


def test_standard_template_provenance_survives_save_copy_and_stays_out_of_remote_payload(service):
    call, _, clients, _ = service
    source = {"preset_id": "adult-shoes", "size_system": "us-men"}
    saved, preview = create_preview(call, template={**draft(), "standard_template": source})
    assert saved["standard_template"] == source
    copied = call("copy", {"template_id": saved["id"]})["template"]
    assert copied["standard_template"] == source
    assert "standard_template" not in preview["results"][0]["payload"]
    call("upload", {"preview_id": preview["preview_id"]})
    assert "standard_template" not in clients[1].session.calls[0][2]["json"]


@pytest.mark.parametrize("source", [None, [], {}, {"preset_id": "adult-shoes"},
    {"preset_id": 1, "size_system": "us-men"}, {"preset_id": "adult shoes", "size_system": "us-men"}])
def test_invalid_standard_template_provenance_is_rejected(service, source):
    call, _, _, _ = service
    with pytest.raises(ValueError, match="标准模板来源"):
        call("create", {**draft(), "standard_template": source})


@pytest.fixture(scope="module")
def builtin_catalog():
    import json
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("Built-in catalog integration requires Node.js")
    result = subprocess.run([node, "-e", "process.stdout.write(JSON.stringify(require('./bit/static/size-chart-builtins.js').templates))"],
                            cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def builtin_requirements(template, prefix="official"):
    gender = next(attr for attr in template["attributes"] if attr["id"] == "GENDER")
    filter_id = template["rows"][0]["attributes"][1]["id"]
    return {
        "errors": [], "max_rows": 75,
        "template_fields": [
            {"id": "GENDER", "name": "Gender", "value_type": "list", "required": True, "values": gender["values"]},
            {"id": "BRAND", "name": "Brand", "value_type": "string", "required": True},
        ],
        "main_attributes": [{"id": "SIZE"}],
        "row_fields": [
            {"id": "SIZE", "name": "Size", "value_type": "string", "required": True},
            {"id": filter_id, "name": "International size", "value_type": "list", "required": True,
             "values": [{"id": f"{prefix}-{index}", "name": row["attributes"][1]["values"][0]["name"]}
                        for index, row in enumerate(template["rows"])]},
            *[{"id": field["id"], "name": field["id"], "value_type": "number_unit", "required": True,
               "allowed_units": ["cm"], "tags": ["body_measure"]}
              for field in template["rows"][0]["attributes"][2:]],
        ],
    }


def test_every_shipped_builtin_normalizes_resolves_and_validates_without_empty_cells(builtin_catalog):
    from bit.size_charts import normalize_template, _prepare_reference_template, _build_payload
    assert len(builtin_catalog) == 21
    for item in builtin_catalog:
        original = deepcopy(item["template"])
        template = normalize_template(original)
        requirements = builtin_requirements(template)
        prepared, notices, errors = _prepare_reference_template(template, requirements)
        assert not errors
        assert notices and "匹配" in notices[0]
        payload, errors = _build_payload(prepared, {"site_id": "CBT", "seller_id": 101}, ["MLM", "MLB"], requirements)
        assert not errors, (item["id"], errors)
        assert template == original
        assert len(payload["rows"]) == len(original["rows"])
        for before, after in zip(original["rows"], payload["rows"]):
            assert before["attributes"][2:] == after["attributes"][2:]
            assert after["attributes"][1]["values"][0]["id"].startswith("official-")


def test_builtin_missing_and_ambiguous_enums_and_extra_required_fields_block_upload(builtin_catalog):
    from bit.size_charts import _prepare_reference_template, _build_payload
    template = builtin_catalog[0]["template"]
    requirements = builtin_requirements(template)
    requirements["row_fields"][1]["values"] = [
        {"id": "one", "name": "XS"}, {"id": "two", "name": "XS"}, {"id": "three", "name": "ZZ"}]
    requirements["row_fields"].append({"id": "OTHER_REQUIRED", "name": "Other required", "value_type": "string", "required": True})
    prepared, _, _ = _prepare_reference_template(template, requirements)
    _, errors = _build_payload(prepared, {"site_id": "CBT", "seller_id": 101}, ["MLM"], requirements)
    assert any("官方属性值编号" in error for error in errors)
    assert any("Other required" in error for error in errors)
    assert "id" not in prepared["rows"][0]["attributes"][1]["values"][0]


def test_builtin_omissions_are_visible_and_no_measurementless_chart_can_pass(builtin_catalog):
    from bit.size_charts import _prepare_reference_template, _build_payload
    template = deepcopy(builtin_catalog[0]["template"])
    template["rows"][0]["attributes"].append({"id": "CUSTOM_UNKNOWN", "values": [{"name": "keep me"}]})
    requirements = builtin_requirements(builtin_catalog[0]["template"])
    requirements["row_fields"] = requirements["row_fields"][:2]
    prepared, notices, errors = _prepare_reference_template(template, requirements)
    assert errors and all("身体测量字段" in error for error in errors)
    assert any("CHEST_CIRCUMFERENCE_FROM" in notice for notice in notices)
    _, validation = _build_payload(prepared, {"site_id": "CBT", "seller_id": 101}, ["MLM"], requirements)
    assert any("CUSTOM_UNKNOWN" in error for error in validation)
    assert len(template["rows"][0]["attributes"]) == 13
    normal = deepcopy(template)
    normal.pop("standard_template")
    assert _prepare_reference_template(normal, requirements) == (normal, [], [])


def test_builtin_multistore_preview_preserves_reference_and_uploads_target_specific_enum_ids(service, builtin_catalog, monkeypatch):
    from bit import size_charts
    call, store, clients, summaries = service
    summaries.append({"id": 4, "organization_key": "org-a", "display_name": "Store Four", "meli_user_id": 104, "enabled": True})
    clients[4] = FakeClient(104)
    source = builtin_catalog[0]["template"]
    # Schemas vary by seller; no actual marketplace requests are made in this test.
    def requirements(api, identity, domain, attributes):
        assert domain == source["domain_id"]
        result = builtin_requirements(source, prefix=str(identity["seller_id"]))
        result["row_fields"] = [field for field in result["row_fields"] if field["id"] != "NECK_CIRCUMFERENCE_TO"]
        return result
    monkeypatch.setattr(size_charts, "_requirements", requirements)
    saved = call("create", source)["template"]
    preview = call("preview", {"template_id": saved["id"], "revision": 1,
        "targets": [{"token_id": 1, "site_id": "MLM"}, {"token_id": 4, "site_id": "MLB"}]}, allowed=(1, 4))
    assert [result["status"] for result in preview["results"]] == ["ready", "ready"]
    assert all(any("NECK_CIRCUMFERENCE_TO" in notice for notice in result["notices"]) for result in preview["results"])
    result = call("upload", {"preview_id": preview["preview_id"]}, allowed=(1, 4))
    assert result["succeeded"] == 2
    for token in (1, 4):
        sent = clients[token].session.calls[0][2]["json"]
        assert sent["rows"][0]["attributes"][1]["values"][0]["id"] == f"{clients[token].seller_id}-0"
        assert not any(field["id"] == "NECK_CIRCUMFERENCE_TO" for field in sent["rows"][0]["attributes"])
    assert store.get_template("org-a", saved["id"])["rows"] == source["rows"]
    assert preview["template_snapshot"]["standard_template"] == source["standard_template"]


def test_options_use_saved_scoped_sites_without_remote_identity_calls(service):
    call, _, clients, summaries = service
    summaries[0].update(site_id="CBT", site_settings=[{"site_id": "MLM"}, {"site_id": "MLB"}])
    result = call("options", allowed=[1])["stores"]
    assert [row["id"] for row in result] == [1]
    assert {row["site_id"] for row in result[0]["sites"]} == {"CBT", "MLM", "MLB"}
    assert not any(client.calls for client in clients.values())
    assert call("options", allowed=[])["stores"] == []
    summaries[0].pop("site_id")
    summaries[0].pop("site_settings")
    missing = call("options", allowed=[1])["stores"][0]
    assert missing["sites"] == [] and "更新店铺授权" in missing["message"]
