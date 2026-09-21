import json
from pathlib import Path
from types import SimpleNamespace

from bit import bit_interface
from local_agent import LocalAgent
from scripts.workbench_backup import create_backup, verify_backup


def test_agent_credential_cannot_reach_workbench_account_api(monkeypatch):
    monkeypatch.setattr(bit_interface.app, "testing", False)
    monkeypatch.setattr(bit_interface, "USE_DB_API", False)
    monkeypatch.delenv("BIT_DB_API_TOKEN", raising=False)
    monkeypatch.setattr(
        bit_interface,
        "_verify_local_agent_credential",
        lambda _token: {"agent_id": "agent-a", "user_id": 7},
    )
    response = bit_interface.app.test_client().get(
        "/api/db/workbench/users",
        headers={"X-Internal-Token": "agent:test"},
        environ_overrides={"REMOTE_ADDR": "198.51.100.10"},
    )
    assert response.status_code == 403


def test_customer_filter_is_independent_from_employee_display_name(monkeypatch):
    user = {
        "id": 7,
        "username": "operator",
        "display_name": "同名员工",
        "organization_key": "acme-shop",
        "permissions": ["store_links.view"],
        "access_version": 1,
        "is_platform_admin": False,
        "own_store_only": False,
    }
    filtered = bit_interface._filter_mercado_tokens_for_user(
        {
            "rows": [
                {"id": 1, "organization_key": "acme-shop", "display_name": "A"},
                {"id": 2, "organization_key": "other-shop", "display_name": "同名员工"},
            ],
            "total": 2,
        },
        user,
    )
    assert [row["id"] for row in filtered["rows"]] == [1]


def test_local_agent_can_roll_back_to_previous_release(tmp_path):
    release_root = tmp_path / "releases"
    for version in ("new-version", "known-good"):
        path = release_root / version
        path.mkdir(parents=True)
        (path / "local_agent_worker.py").write_text("# worker", encoding="utf-8")
    (tmp_path / "current-release.json").write_text(
        json.dumps({"version": "new-version"}), encoding="utf-8"
    )
    config = SimpleNamespace(
        data_dir=tmp_path,
        agent_token="",
        server_url="https://example.test",
        agent_id="agent-a",
        name="测试电脑",
    )
    agent = LocalAgent(config)
    assert agent.rollback_release() == "known-good"
    current = json.loads((tmp_path / "current-release.json").read_text(encoding="utf-8"))
    assert current["version"] == "known-good"
    assert current["previous_version"] == "new-version"


def test_backup_manifest_is_verifiable_without_mysql(tmp_path):
    backup = create_backup(tmp_path, skip_mysql=True)
    manifest = verify_backup(backup)
    assert set(manifest["files"]).issubset(
        {"mysql.sql", "local-agent-hub.sqlite3"}
    )
    assert (backup / "manifest.json").is_file()
