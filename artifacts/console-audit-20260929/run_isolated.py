"""Run regression tests without initializing production MySQL or external APIs."""
import os
from pathlib import Path
import tempfile
import pytest
from erp.ai_weight_price.service import Service

root = Path(tempfile.mkdtemp(prefix='zeshun-console-audit-'))
os.environ['AI_WEIGHT_PRICE_DATA_DIR'] = str(root)
os.environ['BIT_RUNTIME_ROLE'] = 'server'
os.environ.pop('BIT_EXECUTION_TARGET', None)
original = Service.__init__
def isolated_init(self, data_root, *args, **kwargs):
    if Path(data_root) == root and kwargs.get('storage_backend') == 'mysql':
        kwargs['storage_backend'] = 'sqlite'
    return original(self, data_root, *args, **kwargs)
Service.__init__ = isolated_init
if __name__ == '__main__':
    raise SystemExit(pytest.main([
        'tests', '-q', '--tb=short', '--continue-on-collection-errors',
        '--ignore=tests/test_console_ui_browser.py',
        '--ignore=tests/test_bit_interface_appeal.py',
        '--ignore=tests/test_zeshun_popup_browser.py',
        '--ignore=tests/test_store_link_video_browser.py',
        '--ignore=tests/test_pdd_orders_browser.py',
        '--ignore=tests/test_ai_weight_price_browser.py',
        '--junitxml=artifacts/console-audit-20260929/regression-final.xml',
    ]))
