"""Read-only verification against the live member's actual database permissions."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

from flask import Flask
from flask.sessions import SecureCookieSessionInterface
import requests
from bit import bit_mysql as db


connection = db.pymysql.connect(**{**db.config, "read_timeout": 10})
try:
    with connection.cursor() as cursor:
        cursor.execute("SELECT id,username,display_name,role_key FROM workbench_users WHERE display_name=%s AND is_active=1", ("徐家强",))
        user = cursor.fetchone()
        cursor.execute("SELECT t.id,s.salesperson FROM mercado_store_tokens t LEFT JOIN mercado_store_site_settings s ON s.token_id=t.id")
        rows = cursor.fetchall()
finally:
    connection.close()
assert user and user["role_key"] == "member"
allowed = {row["id"] for row in rows if str(row["salesperson"] or "").strip() in {user["display_name"], user["username"]}}
app = Flask("sync-visibility-verification")
app.secret_key = Path("bit/runtime_locks/workbench_secret.key").read_text().strip()
cookie = SecureCookieSessionInterface().get_signing_serializer(app).dumps({
    "workbench_user": {"id": user["id"], "access_version": 1},
})


def check(path):
    with requests.Session() as session:
        session.trust_env = False
        session.cookies.set("session", cookie, domain="127.0.0.1", path="/")
        response = session.get("http://127.0.0.1:5000" + path, timeout=30)
        payload = response.json()
    assert response.status_code == 200, (path, response.status_code, payload.get("message"))
    def verify(state):
        assert set(state.get("target_token_ids", [])) <= allowed
        assert set(state.get("active_token_ids", [])) <= allowed
        assert all(row["token_id"] in allowed for row in state.get("results", []))
        assert "李超宇" not in json.dumps(state, ensure_ascii=False)
        if state.get("rights_holder_sync"):
            verify(state["rights_holder_sync"])
    state = payload["data"]
    verify(state)
    return {"path": path, "http_status": response.status_code,
            "running": state.get("running"), "visible_results": len(state.get("results", [])),
            "no_foreign_store_data": True, "cache_control": response.headers.get("Cache-Control")}


paths = ["/api/store-links/sync/status", "/api/order-sync/status",
         "/api/prohibited-listings/sync/status", "/api/official-infractions/sync/status"]
with ThreadPoolExecutor(max_workers=4) as pool:
    checks = list(pool.map(check, paths))
report = {"member": user["display_name"], "role": user["role_key"],
          "authorized_store_count": len(allowed), "checks": checks}
Path(__file__).with_name("verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(report, ensure_ascii=False, indent=2))
