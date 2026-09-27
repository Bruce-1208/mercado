from urllib.parse import parse_qs, urlparse

import pytest

from bit.mercado_pppi_snapshot import collect_tab, normalize_page
from bit import mercado_pppi_snapshot as snapshot
from erp import mercadolibre_infraction_store as store


def page_state(offset=0, total=3):
    return {
        "title": "Intellectual property infringements - Mexico",
        "selectedTab": "detections", "detectionsTotal": total,
        "infractions": {
            "paging": {"offset": offset, "limit": 2, "total": total},
            "results": [
                {"case_id": i + 1, "element_id": f"MLM{i + 1}",
                 "date_created": "7/14/26", "related_filter_name": "FAKES",
                 "item": {"title": "Same product", "picture": ""}}
                for i in range(offset, min(total, offset + 2))
            ],
        },
    }


class Page:
    def __init__(self, states):
        self.states = states
        self.offsets = []

    def goto(self, url, **kwargs):
        self.offsets.append(int(parse_qs(urlparse(url).query)["offset"][0]))

    def wait_for_function(self, *args, **kwargs):
        pass

    def evaluate(self, expression):
        return self.states[len(self.offsets) - 1]


def test_collects_every_page_and_keeps_distinct_listing_ids():
    page = Page([page_state(), page_state(2)])
    rows = collect_tab(page, "MLM", "detections")
    assert page.offsets == [0, 2]
    assert [row["item_id"] for row in rows] == ["MLM1", "MLM2", "MLM3"]
    assert rows[0]["occurred_at"] == "2026-07-14 00:00:00"


@pytest.mark.parametrize("failure", ["site", "tab", "short", "total", "item", "offset"])
def test_invalid_pages_cannot_be_saved_as_zero(failure):
    state = page_state()
    if failure == "site": state["title"] = "Intellectual property infringements - Brazil"
    if failure == "tab": state["selectedTab"] = "denounces"
    if failure == "short": state["infractions"]["results"] = []
    if failure == "total": state["detectionsTotal"] = 0
    if failure == "item": state["infractions"]["results"][0]["element_id"] = "MLB1"
    if failure == "offset": state["infractions"]["paging"]["offset"] = 2
    with pytest.raises(ValueError):
        normalize_page(state, "MLM", "detections", 0)


def test_empty_snapshot_requires_explicit_zero_and_correct_site():
    assert normalize_page(page_state(total=0), "MLM", "detections", 0)[0] == []
    with pytest.raises(ValueError):
        normalize_page(None, "MLM", "detections", 0)


def test_repeated_and_changing_pages_abort_snapshot():
    repeated = page_state(2)
    repeated["infractions"]["results"][0]["case_id"] = 1
    for second in [repeated, page_state(2, total=4)]:
        with pytest.raises(ValueError):
            collect_tab(Page([page_state(), second]), "MLM", "detections")


def test_second_tab_failure_preserves_entire_previous_site(monkeypatch):
    saved = []
    def collect(page, site, tab):
        if tab == "denounces":
            raise ValueError("login expired")
        return normalize_page(page_state(), "MLM", "detections", 0)[0]
    monkeypatch.setattr(snapshot, "collect_tab", collect)
    monkeypatch.setattr(snapshot, "replace_pppi_snapshot", lambda *args: saved.append(args))
    with pytest.raises(ValueError, match="login expired"):
        snapshot.sync_site_page(None, {"id": 2}, "MLM")
    assert saved == []


def test_failed_snapshot_rolls_back_visibility_flags(monkeypatch):
    class Connection:
        committed = False
        rolled_back = False
        closed = False
        statements = []
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, sql, params):
            self.statements.append(sql)
            if "ON DUPLICATE KEY UPDATE `pppi_visible`" in sql:
                raise RuntimeError("database interrupted")
        def commit(self): self.committed = True
        def rollback(self): self.rolled_back = True
        def close(self): self.closed = True

    db = Connection()
    monkeypatch.setattr(store, "ensure_infraction_tables", lambda cursor: None)
    rows = normalize_page(page_state(), "MLM", "detections", 0)[0]
    with pytest.raises(RuntimeError, match="database interrupted"):
        store.replace_pppi_snapshot({"id": 2}, "MLM", rows, connection_factory=lambda: db)
    assert db.rolled_back and db.closed and not db.committed
    assert any("SET `pppi_visible` = 0" in sql for sql in db.statements)
    assert all("SET `is_current`" not in sql for sql in db.statements)
