"""Run membership regressions without starting workers or opening the live AI store."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['BIT_BACKGROUND_SERVICES_DISABLED'] = '1'
os.environ['BIT_RUNTIME_ROLE'] = 'server'

from erp.ai_weight_price.service import Service
import pytest


def main():
    original = Service.__init__
    with tempfile.TemporaryDirectory(prefix='membership-offline-') as directory:
        def offline_service(self, root, *args, **kwargs):
            kwargs.update(storage_backend='sqlite', migrate_legacy_state=False)
            return original(self, Path(directory), *args, **kwargs)
        suites = [
            'tests/test_membership_access.py', 'tests/test_bit_access_control.py',
            'tests/test_enterprise_salesperson_quota.py',
            'tests/test_enterprise_permission_isolation.py',
            'tests/test_registration_agent_scope.py', 'tests/test_catalog_visibility.py',
        ]
        with patch.object(Service, '__init__', offline_service):
            return pytest.main(sys.argv[1:] or [*(str(ROOT / name) for name in suites), '-q'])


if __name__ == '__main__':
    raise SystemExit(main())
