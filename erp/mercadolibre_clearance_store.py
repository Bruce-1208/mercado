"""Central, durable clearance observations and merchant-confirmed evidence.

Companion storage follows the promotion workbench's SQLite deployment model.
It stores neither credentials nor orders. A store snapshot is replaced only
after a complete read; failed reads invalidate conclusions without losing facts.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from erp.mercadolibre_clearance_rules import evaluate_clearance, evaluate_workflow

LOCAL = ZoneInfo("Asia/Shanghai")
DEFAULT_PATH = Path(__file__).resolve().parents[1] / ".data" / "clearance-alerts.sqlite3"
FACT_FIELDS = {
    "first_inbound_at", "inbound_history_confirmed", "restock_after_stockout",
    "last_restock_at", "visits_30d", "visits_as_of", "notice_at",
    "promotion_start_at", "return_order_at",
}
DATE_FIELDS = FACT_FIELDS - {"inbound_history_confirmed", "restock_after_stockout", "visits_30d"}


def utc_now():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def validate_facts(payload, now=None):
    if not isinstance(payload, dict) or set(payload) - FACT_FIELDS - {"sku_key"}:
        raise ValueError("核实信息包含不支持的字段")
    now = now or utc_now()
    result = {key: value for key, value in payload.items() if key in FACT_FIELDS}
    for field in DATE_FIELDS & result.keys():
        value = result[field]
        if value in (None, ""):
            result[field] = None
            continue
        try:
            parsed = date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("日期须为 YYYY-MM-DD 格式") from exc
        if field != "promotion_start_at" and parsed > now.astimezone(LOCAL).date():
            raise ValueError("入仓、补货、访问量及已收到的平台通知日期不能晚于今天")
        result[field] = parsed.isoformat()
    for field in ("inbound_history_confirmed", "restock_after_stockout"):
        if field in result and type(result[field]) is not bool and not (
            field == "restock_after_stockout" and result[field] is None
        ):
            raise ValueError("入仓确认须为布尔值；缺货后补货须为是、否或待核实")
    if "visits_30d" in result and result["visits_30d"] is not None:
        value = result["visits_30d"]
        if type(value) is not int or not 0 <= value <= 2147483647:
            raise ValueError("过去30天访问量须为非负整数")
    return result


class ClearanceStore:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("BIT_CLEARANCE_DB_PATH") or DEFAULT_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS clearance_facts (
                    link_id INTEGER NOT NULL, sku_key TEXT NOT NULL,
                    payload TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(link_id, sku_key)
                );
                CREATE TABLE IF NOT EXISTS clearance_snapshots (
                    link_id INTEGER NOT NULL, sku_key TEXT NOT NULL,
                    token_id INTEGER NOT NULL, site_id TEXT NOT NULL,
                    item_id TEXT NOT NULL, searchable TEXT NOT NULL,
                    alert_status TEXT NOT NULL, valid_until TEXT NOT NULL,
                    payload TEXT NOT NULL, checked_at TEXT NOT NULL,
                    PRIMARY KEY(link_id, sku_key)
                );
                CREATE INDEX IF NOT EXISTS idx_clearance_scope
                    ON clearance_snapshots(token_id,site_id,alert_status);
                CREATE TABLE IF NOT EXISTS clearance_runs (
                    token_id INTEGER NOT NULL, site_id TEXT NOT NULL, checked_at TEXT NOT NULL,
                    status TEXT NOT NULL, last_error TEXT NOT NULL DEFAULT ''
                    , PRIMARY KEY(token_id,site_id)
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def facts(self, link_id, sku_key):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM clearance_facts WHERE link_id=? AND sku_key=?",
                             (int(link_id), str(sku_key))).fetchone()
        return json.loads(row[0]) if row else {}

    def _evaluate(self, row, facts, now):
        row = dict(row)
        facts = dict(facts)
        observed = row.get("observed_restock_at")
        if observed and str(facts.get("restock_verified_at") or "") < observed:
            facts["restock_after_stockout"] = None
            facts["last_restock_at"] = None
        row["facts"] = facts
        inputs = {**row, **facts}
        # Only a merchant-confirmed *current* 30-day SKU observation can rule
        # out low exposure. Remote item visits are displayed separately.
        inputs["visits_complete"] = (
            facts.get("visits_30d") is not None
            and facts.get("visits_as_of") == now.astimezone(LOCAL).date().isoformat()
        )
        checked_at = row.get("checked_at")
        try:
            observed_at = datetime.fromisoformat(checked_at)
            age = now - observed_at
            fresh = (timedelta(0) <= age < timedelta(hours=26)
                     and observed_at.astimezone(LOCAL).date() == now.astimezone(LOCAL).date())
        except (TypeError, ValueError):
            fresh = False
        if not fresh or row.get("source_failed") or row.get("inventory_complete") is False:
            inputs["sales_complete"] = False
            inputs["available_quantity"] = None
        row["alert"] = evaluate_clearance(inputs, now=now)
        if row.get("source_errors"):
            row["alert"]["details"].extend(row["source_errors"])
        row["workflow"] = evaluate_workflow(facts, row["alert"]["status"], now=now)
        row["visits_30d"] = facts.get("visits_30d")
        row["visits_daily_avg"] = (
            round(facts["visits_30d"] / 30, 2) if facts.get("visits_30d") is not None else None
        )
        return row

    def _write_snapshot(self, db, row, facts, now):
        evaluated = self._evaluate(row, facts, now)
        # A new Beijing day changes the rolling observation and can expire a
        # warehouse exemption. Until the daily scan, downgrade old conclusions.
        midnight = datetime.combine(now.astimezone(LOCAL).date() + timedelta(days=1),
                                    datetime.min.time(), tzinfo=LOCAL)
        try:
            observed_at = datetime.fromisoformat(row["checked_at"])
            midnight = min(midnight, datetime.combine(observed_at.astimezone(LOCAL).date() + timedelta(days=1),
                           datetime.min.time(), tzinfo=LOCAL))
        except (TypeError, ValueError, KeyError):
            midnight = now
        expiry = min(midnight, now + timedelta(hours=26))
        searchable = " ".join(str(evaluated.get(key) or "") for key in
                              ("item_id", "seller_sku", "title", "store_name", "variation_id"))
        db.execute("""INSERT INTO clearance_snapshots
            (link_id,sku_key,token_id,site_id,item_id,searchable,alert_status,valid_until,payload,checked_at)
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(link_id,sku_key) DO UPDATE SET
            token_id=excluded.token_id,site_id=excluded.site_id,item_id=excluded.item_id,
            searchable=excluded.searchable,alert_status=excluded.alert_status,
            valid_until=excluded.valid_until,payload=excluded.payload,checked_at=excluded.checked_at""",
            (evaluated["link_id"], evaluated["sku_key"], evaluated["token_id"], evaluated["site_id"],
             evaluated["item_id"], searchable, evaluated["alert"]["status"], stamp(expiry),
             encode(evaluated), evaluated["checked_at"]))

    def replace_store(self, token_id, rows, now=None, site_ids=None):
        now = now or utc_now()
        rows = list(rows)  # Exhaust the source before altering the last snapshot.
        selected_sites = sorted(set(site_ids)) if site_ids is not None else None
        extra = " AND site_id IN (" + ",".join("?" for _ in selected_sites) + ")" if selected_sites else ""
        if selected_sites == []:
            return
        with self.connect() as db:
            previous = {(r["link_id"], r["sku_key"]): json.loads(r["payload"])
                        for r in db.execute("SELECT link_id,sku_key,payload FROM clearance_snapshots WHERE token_id=?",
                                            (int(token_id),))}
            facts = {(r["link_id"], r["sku_key"]): json.loads(r["payload"])
                     for r in db.execute("SELECT f.* FROM clearance_facts f JOIN clearance_snapshots s "
                                         "ON s.link_id=f.link_id AND s.sku_key=f.sku_key WHERE s.token_id=?",
                                         (int(token_id),))}
            db.execute("DELETE FROM clearance_snapshots WHERE token_id=?" + extra, [int(token_id), *(selected_sites or [])])
            for source in rows:
                row = dict(source)
                if int(row["token_id"]) != int(token_id):
                    raise ValueError("检测快照店铺不匹配")
                if selected_sites is not None and row["site_id"] not in selected_sites:
                    raise ValueError("检测快照站点不匹配")
                key = (row["link_id"], row["sku_key"])
                old = previous.get(key, {})
                row["observed_restock_at"] = old.get("observed_restock_at")
                if old.get("available_quantity") == 0 and (row.get("available_quantity") or 0) > 0:
                    row["observed_restock_at"] = stamp(now)
                row.setdefault("checked_at", stamp(now))
                evidence = facts.get(key)
                if evidence is None:
                    stored = db.execute("SELECT payload FROM clearance_facts WHERE link_id=? AND sku_key=?", key).fetchone()
                    evidence = json.loads(stored[0]) if stored else {}
                self._write_snapshot(db, row, evidence, now)
            for site_id in selected_sites if selected_sites is not None else {row["site_id"] for row in rows}:
                incomplete = any(row["site_id"] == site_id and row.get("source_errors") for row in rows)
                db.execute("INSERT INTO clearance_runs VALUES (?,?,?,?,?) ON CONFLICT(token_id,site_id) DO UPDATE SET "
                           "checked_at=excluded.checked_at,status=excluded.status,last_error=excluded.last_error",
                           (int(token_id), site_id, stamp(now), "failed" if incomplete else "succeeded",
                            "部分 SKU 数据读取失败或无法核实，请重新检测" if incomplete else ""))

    def mark_failed(self, token_id, now=None, site_ids=None):
        now = now or utc_now()
        selected_sites = sorted(set(site_ids)) if site_ids is not None else None
        extra = " AND site_id IN (" + ",".join("?" for _ in selected_sites) + ")" if selected_sites else ""
        if selected_sites == []:
            return
        with self.connect() as db:
            rows = db.execute("SELECT link_id,sku_key,payload FROM clearance_snapshots WHERE token_id=? "
                              "AND alert_status != 'not_applicable'" + extra, [int(token_id), *(selected_sites or [])]).fetchall()
            for stored in rows:
                payload = json.loads(stored["payload"])
                payload["source_failed"] = True
                db.execute("UPDATE clearance_snapshots SET valid_until=?,alert_status='pending_data',payload=? "
                           "WHERE link_id=? AND sku_key=?", (stamp(now), encode(payload), stored["link_id"], stored["sku_key"]))
            for site_id in selected_sites if selected_sites is not None else {json.loads(row["payload"])["site_id"] for row in rows}:
                db.execute("INSERT INTO clearance_runs VALUES (?,?,?,?,?) ON CONFLICT(token_id,site_id) DO UPDATE SET "
                           "checked_at=excluded.checked_at,status=excluded.status,last_error=excluded.last_error",
                           (int(token_id), site_id, stamp(now), "failed", "店铺数据读取失败，请重新检测"))

    @staticmethod
    def _scope_sql(scopes):
        if not scopes:
            return "0", []
        # token/site permissions are evaluated from the current authorization,
        # never from a display name or the historical snapshot's group.
        clauses, values = [], []
        for token_id, site_id in sorted(scopes):
            clauses.append("(?,?)")
            values.extend([int(token_id), site_id])
        return "(token_id,site_id) IN (VALUES " + ",".join(clauses) + ")", values

    def save_facts(self, link_id, sku_key, payload, scopes, now=None):
        now = now or utc_now()
        changes = validate_facts(payload, now)
        scope_sql, scope_values = self._scope_sql(scopes)
        with self.connect() as db:
            row = db.execute("SELECT payload FROM clearance_snapshots WHERE link_id=? AND sku_key=? AND " + scope_sql,
                             [int(link_id), str(sku_key), *scope_values]).fetchone()
            if row is None:
                raise KeyError("SKU 不存在或不在当前半托管店铺权限内，请先检测")
            snapshot = json.loads(row[0])
            stored = db.execute("SELECT payload FROM clearance_facts WHERE link_id=? AND sku_key=?",
                                (int(link_id), str(sku_key))).fetchone()
            facts = {**(json.loads(stored[0]) if stored else {}), **changes}
            if facts.get("inbound_history_confirmed") and not facts.get("first_inbound_at"):
                raise ValueError("确认入仓记录时必须填写首次入仓日期")
            if facts.get("restock_after_stockout") is True and not facts.get("last_restock_at"):
                raise ValueError("缺货后补货时必须填写新批次入仓日期")
            if facts.get("visits_30d") is not None and not facts.get("visits_as_of"):
                raise ValueError("填写访问量时必须填写统计截至日期")
            if facts.get("first_inbound_at") and facts.get("last_restock_at") and facts["last_restock_at"] < facts["first_inbound_at"]:
                raise ValueError("补货入仓日期不能早于首次入仓日期")
            facts["updated_at"] = stamp(now)
            if "restock_after_stockout" in changes:
                facts["restock_verified_at"] = stamp(now)
            db.execute("INSERT INTO clearance_facts VALUES (?,?,?,?) ON CONFLICT(link_id,sku_key) "
                       "DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                       (int(link_id), str(sku_key), encode(facts), stamp(now)))
            self._write_snapshot(db, snapshot, facts, now)
        return self._evaluate(snapshot, facts, now)

    def list_alerts(self, scopes, *, search="", alert_status="", page=1, page_size=100, now=None):
        now = now or utc_now()
        if alert_status not in {"", "risk", "pending_data", "exempt", "normal"}:
            raise ValueError("告警状态无效")
        page, page_size = max(1, int(page)), max(1, min(200, int(page_size)))
        scope_sql, values = self._scope_sql(scopes)
        conditions = [scope_sql, "alert_status != 'not_applicable'"]
        if search:
            conditions.append("instr(lower(searchable),lower(?))>0")
            values.append(str(search)[:200])
        where = " AND ".join(conditions)
        effective = "CASE WHEN valid_until<=? THEN 'pending_data' ELSE alert_status END"
        with self.connect() as db:
            grouped = db.execute(f"SELECT {effective} AS state,COUNT(*) AS n FROM clearance_snapshots WHERE {where} GROUP BY state",
                                 [stamp(now), *values]).fetchall()
            counts = {state: 0 for state in ("risk", "pending_data", "exempt", "normal")}
            counts.update({row["state"]: row["n"] for row in grouped})
            filtered = where
            params = [stamp(now), *values]
            if alert_status:
                filtered += f" AND {effective}=?"
                params.extend([stamp(now), alert_status])
            total = counts.get(alert_status, 0) if alert_status else sum(counts.values())
            rows = db.execute(f"SELECT *,{effective} AS effective_status FROM clearance_snapshots WHERE {filtered} "
                              "ORDER BY CASE effective_status WHEN 'risk' THEN 0 WHEN 'pending_data' THEN 1 "
                              "WHEN 'exempt' THEN 2 ELSE 3 END,token_id,site_id,item_id,sku_key LIMIT ? OFFSET ?",
                              [*params, page_size, (page - 1) * page_size]).fetchall()
            runs_scope_sql, runs_scope_values = self._scope_sql(scopes)
            runs = db.execute("SELECT * FROM clearance_runs WHERE " + runs_scope_sql, runs_scope_values).fetchall()
        result_rows = []
        for stored in rows:
            row = json.loads(stored["payload"])
            row = self._evaluate(row, row.get("facts", {}), now)
            if stored["effective_status"] == "pending_data" and row["alert"]["status"] != "pending_data":
                row["alert"].update(status="pending_data", status_label="待核实", summary="检测数据已过期或读取失败，请重新检测")
                row["alert"]["missing_fields"].append("fresh_snapshot")
                row["workflow"] = evaluate_workflow(row.get("facts", {}), "pending_data", now=now)
            result_rows.append(row)
        stores = {int(key[0]): {"token_id": int(key[0]), "store_name": scope["store_name"]}
                  for key, scope in scopes.items()}
        return {"rows": result_rows, "total": total, "page": page, "page_size": page_size,
                "counts": counts, "stores": list(stores.values()),
                "checked_at": max((r["checked_at"] for r in runs), default=""),
                "last_error": "；".join(r["last_error"] for r in runs if r["last_error"]),
                "scope_count": len(scopes)}

    def last_success_date(self, token_id, site_ids=None):
        with self.connect() as db:
            rows = db.execute("SELECT site_id,checked_at,status FROM clearance_runs WHERE token_id=?", (int(token_id),)).fetchall()
        required = set(site_ids) if site_ids is not None else {row["site_id"] for row in rows}
        successful = {row["site_id"]: datetime.fromisoformat(row["checked_at"]).astimezone(LOCAL).date().isoformat()
                      for row in rows if row["status"] == "succeeded"}
        if required and required <= successful.keys():
            return min(successful[site] for site in required)
        return ""
