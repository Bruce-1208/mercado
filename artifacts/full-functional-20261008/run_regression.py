"""Run existing regressions with an isolated server bootstrap, without MySQL.

Only the console's import-time weight-price store is redirected to temporary
SQLite. Tests retain their own mocks and real storage implementations afterward.
"""
import os
from pathlib import Path
import sys
import tempfile
import socket
import signal
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
# Isolated tests may start loopback HTTP fixtures, but cannot reach production
# databases, running workbench services, merchants or model/payment providers.
original_connect = socket.socket.connect
def isolated_connect(self, address):
    if (not isinstance(address, tuple) or len(address) < 2
            or address[0] not in {"127.0.0.1", "localhost", "::1"}
            or int(address[1]) < 10000 or int(address[1]) in {33060, 37600, 54345}):
        raise PermissionError("Functional regression blocks business-service networking")
    return original_connect(self, address)
socket.socket.connect = isolated_connect

import pytest
completed = set()

class BoundedRegression:
    def pytest_collection_modifyitems(self, config, items):
        selected, deselected = [], []
        for item in items:
            target = deselected if (Path(str(item.path)).stem, item.name) in completed else selected
            target.append(item)
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        (ROOT / "artifacts/full-functional-20261008/current-test.txt").write_text(item.nodeid)
        previous = signal.getsignal(signal.SIGALRM)
        def timeout(signum, frame):
            pytest.fail("Isolated functional test exceeded 45 seconds: " + item.nodeid,
                        pytrace=False)
        signal.signal(signal.SIGALRM, timeout)
        signal.setitimer(signal.ITIMER_REAL, 45)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

def main():
    scratch = Path(tempfile.mkdtemp(prefix="mercado-functional-regression-"))
    os.environ.update({
        "BIT_RUNTIME_ROLE": "server",
        "BIT_BACKGROUND_SERVICES_DISABLED": "1",
        "AI_WEIGHT_PRICE_DATA_DIR": str(scratch / "weight-price"),
        "AI_VIDEO_STORAGE_PATH": str(scratch / "videos"),
        "BIT_BACKGROUND_TASK_ROOT": str(scratch / "background"),
        "BIT_BROWSER_EXTENSION_APPEAL_ROOT": str(scratch / "extension-appeals"),
        "BIT_LOCAL_AGENT_HUB_PATH": str(scratch / "agents.json"),
        "BIT_WIPO_SYNC_PATH": str(scratch / "wipo.sqlite3"),
        "BIT_CLEARANCE_DB_PATH": str(scratch / "clearance.sqlite3"),
        "BIT_PROMOTION_DB_PATH": str(scratch / "promotions.sqlite3"),
        "BIT_SIZE_CHART_DB_PATH": str(scratch / "size-charts.sqlite3"),
        "BIT_STORE_LINK_MARKER_DB_PATH": str(scratch / "markers.sqlite3"),
        "BIT_AD_PROFIT_PATH": str(scratch / "ad-profit.sqlite3"),
        "BIT_DAILY_TASK_HISTORY_PATH": str(scratch / "daily-history.json"),
        "BIT_DAILY_TASK_LOG_PATH": str(scratch / "daily-logs"),
        "BIT_API_REPUTATION_STATE_PATH": str(scratch / "reputation.json"),
        "BIT_AD_ANALYSIS_STATE_PATH": str(scratch / "ads.json"),
        "BIT_COLLECTION_RATE_LIMIT_STATE_PATH": str(scratch / "rate-limit.json"),
        "MERCADO_INFRINGEMENT_TASK_PATH": str(scratch / "infringement.sqlite3"),
        "MERCADO_STORE_LINK_REMOTE_UPDATE_PATH": str(scratch / "remote-update.sqlite3"),
        "MERCADO_STORE_LINK_AUDIT_PATH": str(scratch / "operations.sqlite3"),
        "AI_USAGE_BILLING_PATH": str(scratch / "billing.sqlite3"),
        "WORKBENCH_SECRET_KEY": "isolated-functional-regression-secret",
        "CONSOLE_TEST_BROWSER": "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    })
    
    from erp.ai_weight_price.service import Service
    original = Service.__init__
    def isolated_init(self, root, *args, **kwargs):
        kwargs["storage_backend"] = "sqlite"
        kwargs["migrate_legacy_state"] = False
        original(self, root, *args, **kwargs)
    Service.__init__ = isolated_init
    try:
        from bit import bit_interface
    finally:
        Service.__init__ = original
    
    global completed
    arguments = sys.argv[1:] or ["-q", "tests"]
    completed = set()
    while "--resume-report" in arguments:
        index = arguments.index("--resume-report")
        report_path = arguments[index + 1]
        del arguments[index:index + 2]
        completed.update((case.get("classname", "").split(".")[-1], case.get("name"))
                         for case in ET.parse(report_path).iter("testcase")
                         if case.get("name") and case.find("error") is None)
    # Keep browser assertions bounded with Playwright's normal timeout path,
    # so failures leave its event loop usable for the next scenario.
    from playwright.sync_api import Browser, BrowserContext
    for owner in (Browser, BrowserContext):
        native_new_page = owner.new_page
        def bounded_page(self, *args, _native=native_new_page, **kwargs):
            page = _native(self, *args, **kwargs)
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(10000)
            return page
        owner.new_page = bounded_page
    
    print("Isolated console bootstrap; temporary storage:", scratch, flush=True)
    return pytest.main(arguments, plugins=[BoundedRegression()])

if __name__ == "__main__":
    raise SystemExit(main())
