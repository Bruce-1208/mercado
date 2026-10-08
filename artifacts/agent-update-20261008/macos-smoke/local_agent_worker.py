import sys, json, tkinter, ssl, requests, pandas, openpyxl, cryptography
from pathlib import Path
from playwright.sync_api import sync_playwright
assert sys.platform == "darwin"
assert getattr(sys, "frozen", False)
ssl.create_default_context(cafile=requests.certs.where())
root = tkinter.Tk(); root.withdraw(); root.destroy()
Path('/Users/a11/mercado/artifacts/agent-update-20261008/macos-smoke/result.json').write_text(json.dumps({"ok":True,"platform":"darwin"}))
