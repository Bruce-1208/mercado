"""Explicit offline identities for legacy route tests; auth tests use real hooks."""
import pytest


@pytest.fixture
def isolated_legacy_console_user(monkeypatch):
    from flask import session
    from bit import bit_interface
    def current_user():
        user = session.get('workbench_user')
        if not user:
            return None
        return {'permissions':['*'], 'access_version':1, 'is_platform_admin':True,
                'organization_key':'default', **user}
    monkeypatch.setattr(bit_interface, 'get_current_workbench_user', current_user)


@pytest.fixture(autouse=True)
def isolated_store_link_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("MERCADO_STORE_LINK_AUDIT_PATH", str(tmp_path / "store-link-audit.sqlite3"))


@pytest.fixture(autouse=True)
def isolated_background_task_store(tmp_path, monkeypatch):
    """Test viewers and mocked jobs must never access the operator's tasks."""
    monkeypatch.setenv("BIT_BACKGROUND_TASK_ROOT", str(tmp_path / "background-tasks"))
    monkeypatch.setenv("MERCADO_STORE_LINK_REMOTE_UPDATE_PATH", str(tmp_path / "link-updates.sqlite3"))


@pytest.fixture(autouse=True)
def isolated_ai_usage_billing(tmp_path, monkeypatch):
    """Mocked model responses must never enter the operator's real ledger."""
    from erp import ai_usage_billing
    monkeypatch.setenv("AI_USAGE_BILLING_PATH", str(tmp_path / "ai-usage-billing.sqlite3"))
    for key in ("AI_BILLING_ACTOR_ID", "AI_BILLING_ACTOR_NAME", "AI_BILLING_ORGANIZATION_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(ai_usage_billing, "_start_outbox_worker", lambda: None)


@pytest.fixture
def subscribed_workbench_account(monkeypatch):
    """Existing permission/login scenarios exercise a paid account, without MySQL."""
    from bit import bit_db_api
    def billing(payload):
        assert payload['action'] == 'get'
        return {'user_id': payload['actor_id'], 'status': 'active'}
    monkeypatch.setattr(bit_db_api, 'account_billing', billing)
