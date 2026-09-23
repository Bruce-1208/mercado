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
