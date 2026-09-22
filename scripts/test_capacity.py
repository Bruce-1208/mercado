"""Run capacity regressions without production DB access or automatic jobs.

Usage: python scripts/test_capacity.py [pytest arguments]
Only the import-time AI store is replaced by a temporary SQLite store. Tests
retain their own authentication, SQL mocks and assertions.
"""

def main():
    import os, sys, tempfile
    from pathlib import Path
    from unittest.mock import patch
    os.environ['BIT_BACKGROUND_SERVICES_DISABLED'] = '1'
    os.environ['BIT_SERVICE_MODE'] = 'combined'
    os.environ['MYSQL_HOST'] = '127.0.0.1'
    os.environ['MYSQL_PORT'] = '1'
    temporary = tempfile.TemporaryDirectory(prefix='capacity-tests-')
    os.environ['BIT_LOCAL_AGENT_HUB_PATH'] = str(Path(temporary.name) / 'hub.sqlite3')
    os.environ['BIT_RUNTIME_LOCK_DIR'] = str(Path(temporary.name) / 'locks')
    ROOT = Path(__file__).resolve().parents[1]
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from erp.ai_weight_price.service import Service
    original = Service.__init__
    def isolated(self, root, *args, **kwargs):
        kwargs['storage_backend'] = 'sqlite'
        kwargs['migrate_legacy_state'] = False
        return original(self, str(Path(temporary.name) / 'ai'), *args, **kwargs)
    with patch.object(Service, '__init__', isolated):
        from bit import bit_interface
    bit_interface.app.testing = True
    import pytest
    arguments = sys.argv[1:] or ['-q',
        'tests/test_capacity_optimizations.py', 'tests/test_local_agent_hub.py',
        'tests/test_local_agent_runtime.py', 'tests/test_local_agent_rate_limit.py',
        'tests/test_local_agent_interface.py', 'tests/test_local_agent_frontend.py',
        'tests/test_bit_store_links.py', 'tests/test_bit_prohibited_listings.py',
        'tests/test_mercado_infraction_dashboard.py', 'tests/test_bit_order_management.py',
        'tests/test_workbench_runtime_interface.py', 'tests/test_bit_login_remember.py',
        'tests/test_bit_access_control.py', 'tests/test_commercial_hardening.py',
        'tests/test_local_executor_auth.py', 'tests/test_db_pool.py',
        'tests/test_erp_mercadolibre_collection_store.py', 'tests/test_appeal_polling.py',
        'tests/test_appeal_polling_frontend.py', 'mercado_api/tests']
    return pytest.main(arguments)


if __name__ == '__main__':
    raise SystemExit(main())
