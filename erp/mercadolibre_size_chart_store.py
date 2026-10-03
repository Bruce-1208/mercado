"""Organization-scoped size-chart drafts and immutable upload audit records."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PATH = Path(__file__).resolve().parents[1] / ".data" / "size-charts.sqlite3"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_organization(value: Any) -> str:
    key = str(value or "wuhan-zeshun").strip().lower()
    return "wuhan-zeshun" if key == "default" else key


class SizeChartStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("BIT_SIZE_CHART_DB_PATH") or DEFAULT_PATH).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS size_chart_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    organization_key TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS size_chart_template_scope
                    ON size_chart_templates(organization_key, id);
                CREATE TABLE IF NOT EXISTS size_chart_previews (
                    preview_id TEXT PRIMARY KEY,
                    organization_key TEXT NOT NULL,
                    template_id INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS size_chart_uploads (
                    record_id TEXT PRIMARY KEY,
                    organization_key TEXT NOT NULL,
                    template_id INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    token_id INTEGER NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(organization_key, token_id, payload_hash)
                );
                CREATE INDEX IF NOT EXISTS size_chart_upload_scope
                    ON size_chart_uploads(organization_key, template_id, revision, token_id);
            """)

    def _connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @staticmethod
    def _template(row) -> dict | None:
        if row is None:
            return None
        return {**json.loads(row["payload_json"]), "id": row["id"], "revision": row["revision"],
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def list_templates(self, organization_key: str) -> list[dict]:
        with self._connect() as db:
            return [self._template(row) for row in db.execute(
                "SELECT * FROM size_chart_templates WHERE organization_key=? ORDER BY updated_at DESC,id DESC",
                (normalize_organization(organization_key),)).fetchall()]

    def get_template(self, organization_key: str, template_id: int) -> dict | None:
        with self._connect() as db:
            return self._template(db.execute(
                "SELECT * FROM size_chart_templates WHERE organization_key=? AND id=?",
                (normalize_organization(organization_key), int(template_id))).fetchone())

    def create_template(self, organization_key: str, payload: dict) -> dict:
        now = now_iso()
        with self._connect() as db:
            row_id = db.execute(
                "INSERT INTO size_chart_templates(organization_key,payload_json,created_at,updated_at) VALUES(?,?,?,?)",
                (normalize_organization(organization_key), json_text(payload), now, now)).lastrowid
        return self.get_template(organization_key, row_id)

    def update_template(self, organization_key: str, template_id: int, revision: int, payload: dict) -> dict:
        with self._connect() as db:
            changed = db.execute(
                "UPDATE size_chart_templates SET payload_json=?,revision=revision+1,updated_at=? "
                "WHERE organization_key=? AND id=? AND revision=?",
                (json_text(payload), now_iso(), normalize_organization(organization_key), int(template_id), int(revision))).rowcount
            if not changed:
                raise ValueError("模板已被修改或删除，请刷新后重试")
        return self.get_template(organization_key, template_id)

    def delete_template(self, organization_key: str, template_id: int, revision: int) -> None:
        with self._connect() as db:
            changed = db.execute(
                "DELETE FROM size_chart_templates WHERE organization_key=? AND id=? AND revision=?",
                (normalize_organization(organization_key), int(template_id), int(revision))).rowcount
            if not changed:
                raise ValueError("模板已被修改或删除，请刷新后重试")

    def save_preview(self, organization_key: str, snapshot: dict, expires_at: str) -> dict:
        preview_id = uuid.uuid4().hex
        result = {**snapshot, "preview_id": preview_id, "expires_at": expires_at}
        with self._connect() as db:
            db.execute("INSERT INTO size_chart_previews VALUES(?,?,?,?,?,?,?)", (
                preview_id, normalize_organization(organization_key), int(snapshot["template_id"]),
                int(snapshot["revision"]), json_text(result), expires_at, now_iso()))
        return result

    def get_preview(self, organization_key: str, preview_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT snapshot_json FROM size_chart_previews WHERE organization_key=? AND preview_id=?",
                             (normalize_organization(organization_key), str(preview_id))).fetchone()
            return json.loads(row[0]) if row else None

    @staticmethod
    def _record(row) -> dict | None:
        return {**json.loads(row["record_json"]), "record_id": row["record_id"], "status": row["status"],
                "created_at": row["created_at"], "updated_at": row["updated_at"]} if row else None

    def blocking_record(self, organization_key: str, template_id: int, revision: int, token_id: int,
                        payload_hash: str = "") -> dict | None:
        with self._connect() as db:
            row = db.execute("""SELECT * FROM size_chart_uploads WHERE organization_key=? AND token_id=?
                AND status IN ('running','succeeded','unknown')
                AND ((template_id=? AND revision=?) OR payload_hash=?) ORDER BY created_at DESC LIMIT 1""",
                (normalize_organization(organization_key), int(token_id), int(template_id), int(revision), payload_hash)).fetchone()
            return self._record(row)

    def claim_upload(self, organization_key: str, record: dict) -> tuple[dict, bool]:
        """Atomically reserve a create; every process observes the same reservation."""
        org = normalize_organization(organization_key)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("""SELECT * FROM size_chart_uploads WHERE organization_key=? AND token_id=?
                AND ((template_id=? AND revision=? AND status IN ('running','succeeded','unknown')) OR payload_hash=?)
                ORDER BY created_at DESC LIMIT 1""", (org, record["token_id"], record["template_id"],
                                                       record["revision"], record["payload_hash"])).fetchone()
            # Rejected remote writes may be retried using a new preview. Ambiguous
            # and in-flight writes are never replayed, including after a restart.
            prior = self._record(existing)
            if existing and (existing["status"] != "failed" or prior.get("preview_id") == record.get("preview_id")):
                return self._record(existing), False
            now = now_iso()
            record_id = existing["record_id"] if existing else uuid.uuid4().hex
            prepared = {**record, "record_id": record_id, "status": "running",
                        "message": "正在提交，重复提交已锁定", "attempt": (prior or {}).get("attempt", 0) + 1,
                        "previous_attempts": [*((prior or {}).get("previous_attempts") or []),
                                              *([{key: prior.get(key) for key in ("preview_id", "status", "message", "response", "updated_at")}]
                                                if prior else [])]}
            if existing:
                db.execute("UPDATE size_chart_uploads SET status='running',record_json=?,updated_at=? WHERE record_id=?",
                           (json_text(prepared), now, record_id))
            else:
                db.execute("INSERT INTO size_chart_uploads VALUES(?,?,?,?,?,?,?,?,?,?)", (
                    record_id, org, record["template_id"], record["revision"], record["token_id"],
                    record["payload_hash"], "running", json_text(prepared), now, now))
            return {**prepared, "created_at": now, "updated_at": now}, True

    def finish_upload(self, organization_key: str, record_id: str, updates: dict) -> dict:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM size_chart_uploads WHERE organization_key=? AND record_id=?",
                             (normalize_organization(organization_key), str(record_id))).fetchone()
            if not row:
                raise ValueError("上传记录不存在")
            record = {**self._record(row), **updates}
            db.execute("UPDATE size_chart_uploads SET status=?,record_json=?,updated_at=? WHERE record_id=?",
                       (record["status"], json_text(record), now_iso(), record_id))
            return record

    def list_records(self, organization_key: str, allowed_token_ids=None, template_id=None) -> list[dict]:
        clauses, params = ["organization_key=?"], [normalize_organization(organization_key)]
        if allowed_token_ids is not None:
            ids = sorted(set(int(value) for value in allowed_token_ids))
            if not ids:
                return []
            clauses.append("token_id IN (" + ",".join("?" for _ in ids) + ")")
            params.extend(ids)
        if template_id:
            clauses.append("template_id=?")
            params.append(int(template_id))
        with self._connect() as db:
            return [self._record(row) for row in db.execute(
                "SELECT * FROM size_chart_uploads WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC LIMIT 500",
                params).fetchall()]

    def get_record(self, organization_key: str, record_id: str) -> dict | None:
        with self._connect() as db:
            return self._record(db.execute(
                "SELECT * FROM size_chart_uploads WHERE organization_key=? AND record_id=?",
                (normalize_organization(organization_key), str(record_id))).fetchone())
