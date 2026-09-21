"""Durable local markers for actions performed on store links.

The listing table is refreshed from Mercado Libre and therefore cannot use the
remote listing snapshot to remember local workflow actions.  Keep the small
amount of workflow metadata in a separate SQLite database keyed by the stable
``(token_id, site_id, item_id)`` identity instead of the MySQL row id.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


DEFAULT_PATH = Path(__file__).resolve().parents[1] / ".data" / "store_link_markers.sqlite3"


def _path() -> Path:
    return Path(os.environ.get("BIT_STORE_LINK_MARKER_DB_PATH") or DEFAULT_PATH).expanduser().resolve()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _connect() -> sqlite3.Connection:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS store_link_markers (
            token_id INTEGER NOT NULL,
            site_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            advertising_enabled INTEGER NOT NULL DEFAULT 0,
            ad_campaign_id TEXT NOT NULL DEFAULT '',
            video_uploaded INTEGER NOT NULL DEFAULT 0,
            video_clip_uuid TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (token_id, site_id, item_id)
        )
        """
    )


@contextmanager
def _database():
    connection = _connect()
    try:
        _ensure_schema(connection)
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _identity(row: Mapping[str, Any]) -> tuple[int, str, str] | None:
    try:
        token_id = int(row.get("token_id") or 0)
    except (TypeError, ValueError):
        token_id = 0
    site_id = str(row.get("site_id") or "").strip().upper()
    item_id = str(row.get("item_id") or "").strip().upper()
    return (token_id, site_id, item_id) if token_id > 0 and site_id and item_id else None


def _mark(identity: tuple[int, str, str], **changes: Any) -> None:
    token_id, site_id, item_id = identity
    allowed = {
        "advertising_enabled": int(bool(changes.get("advertising_enabled"))),
        "ad_campaign_id": str(changes.get("ad_campaign_id") or ""),
        "video_uploaded": int(bool(changes.get("video_uploaded"))),
        "video_clip_uuid": str(changes.get("video_clip_uuid") or ""),
    }
    with _database() as connection:
        connection.execute(
            """
            INSERT INTO store_link_markers (
                token_id, site_id, item_id, advertising_enabled, ad_campaign_id,
                video_uploaded, video_clip_uuid, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(token_id, site_id, item_id) DO UPDATE SET
                advertising_enabled = CASE
                    WHEN excluded.advertising_enabled = 1 THEN 1
                    ELSE store_link_markers.advertising_enabled
                END,
                ad_campaign_id = CASE
                    WHEN excluded.ad_campaign_id <> '' THEN excluded.ad_campaign_id
                    ELSE store_link_markers.ad_campaign_id
                END,
                video_uploaded = CASE
                    WHEN excluded.video_uploaded = 1 THEN 1
                    ELSE store_link_markers.video_uploaded
                END,
                video_clip_uuid = CASE
                    WHEN excluded.video_clip_uuid <> '' THEN excluded.video_clip_uuid
                    ELSE store_link_markers.video_clip_uuid
                END,
                updated_at = excluded.updated_at
            """,
            (
                token_id,
                site_id,
                item_id,
                allowed["advertising_enabled"],
                allowed["ad_campaign_id"],
                allowed["video_uploaded"],
                allowed["video_clip_uuid"],
                _now(),
            ),
        )


def mark_advertising_enabled(row: Mapping[str, Any], campaign_id: Any = "") -> None:
    identity = _identity(row)
    if identity:
        _mark(identity, advertising_enabled=True, ad_campaign_id=campaign_id)


def mark_video_uploaded(row: Mapping[str, Any], clip_uuid: Any = "") -> None:
    identity = _identity(row)
    if identity:
        _mark(identity, video_uploaded=True, video_clip_uuid=clip_uuid)


def mark_advertising_status(
    *, token_id: Any, site_id: Any, item_ids: Iterable[Any], enabled: bool, campaign_id: Any = ""
) -> None:
    """Update markers after an ad-analysis start/pause operation."""
    for item_id in item_ids or ():
        row = {"token_id": token_id, "site_id": site_id, "item_id": item_id}
        identity = _identity(row)
        if not identity:
            continue
        if enabled:
            _mark(identity, advertising_enabled=True, ad_campaign_id=campaign_id)
        else:
            token, site, item = identity
            with _database() as connection:
                connection.execute(
                    """
                    INSERT INTO store_link_markers (
                        token_id, site_id, item_id, advertising_enabled, updated_at
                    ) VALUES (?, ?, ?, 0, ?)
                    ON CONFLICT(token_id, site_id, item_id) DO UPDATE SET
                        advertising_enabled = 0, updated_at = excluded.updated_at
                    """,
                    (token, site, item, _now()),
                )


def markers_for_rows(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[int, str, str], dict[str, Any]]:
    identities = [identity for row in rows if (identity := _identity(row))]
    if not identities:
        return {}
    unique = list(dict.fromkeys(identities))
    placeholders = ", ".join("(?, ?, ?)" for _ in unique)
    params = [value for identity in unique for value in identity]
    with _database() as connection:
        rows = connection.execute(
            f"""
            SELECT token_id, site_id, item_id, advertising_enabled,
                   ad_campaign_id, video_uploaded, video_clip_uuid, updated_at
            FROM store_link_markers
            WHERE (token_id, site_id, item_id) IN ({placeholders})
            """,
            params,
        ).fetchall()
    return {
        (int(row["token_id"]), str(row["site_id"]), str(row["item_id"])): {
            "advertising_enabled": bool(row["advertising_enabled"]),
            "ad_campaign_id": str(row["ad_campaign_id"] or ""),
            "video_uploaded": bool(row["video_uploaded"]),
            "video_clip_uuid": str(row["video_clip_uuid"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }
        for row in rows
    }
