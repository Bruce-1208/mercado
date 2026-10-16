"""Run enterprise regressions offline: python3 scripts/test_enterprise_isolation.py.

MySQL and HTTP are blocked before importing the workbench. Its unrelated startup
AI store uses disposable SQLite; tests supply their own relational fixtures.
Additional pytest paths/options can be passed on the command line.
"""
from pathlib import Path
import os
import sys
import tempfile


def main():
    os.environ['BIT_RUNTIME_ROLE'] = 'server'
    os.environ['BIT_DURABLE_TASK_WORKER'] = '1'
    os.environ['BIT_BACKGROUND_SERVICES_DISABLED'] = '1'
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import pymysql
    import requests

    def blocked(*args, **kwargs):
        raise RuntimeError('Offline tests: live database/network access disabled')

    pymysql.connect = blocked
    requests.sessions.Session.request = blocked
    from erp.ai_weight_price import service
    original_store = service.Store
    with tempfile.TemporaryDirectory(prefix='enterprise-offline-') as root:
        service.Store = lambda _root, backend=None: original_store(root, backend='sqlite')
        try:
            from bit import bit_interface  # noqa: F401
        finally:
            service.Store = original_store
        import pytest
        return pytest.main(sys.argv[1:] or ['tests/test_enterprise_permission_isolation.py', '-q'])


if __name__ == '__main__':
    raise SystemExit(main())
