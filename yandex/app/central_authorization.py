from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any


class AuthorizationStoreError(RuntimeError):
    """Raised when the shared authorization database cannot be used."""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class CentralAuthorizationStore:
    """Store Yandex credentials in the project's shared MySQL database.

    Tokens are only returned by ``get_store(..., include_secret=True)`` for
    server-side API calls.  Public route responses always use the redacted
    representation produced by ``_store_row``.
    """

    @staticmethod
    def _connection():
        try:
            import pymysql
            from bit.bit_mysql import config as shared_config
        except Exception as exc:  # pragma: no cover - deployment dependency error
            raise AuthorizationStoreError(
                "中央授权数据库驱动不可用，请重新运行 yandex/run.ps1 安装依赖"
            ) from exc

        config = dict(shared_config)
        overrides = {
            "host": os.getenv("YANDEX_MYSQL_HOST", "").strip(),
            "user": os.getenv("YANDEX_MYSQL_USER", "").strip(),
            "password": os.getenv("YANDEX_MYSQL_PASSWORD", ""),
            "database": os.getenv("YANDEX_MYSQL_DATABASE", "").strip(),
        }
        for key, value in overrides.items():
            if value:
                config[key] = value
        port = os.getenv("YANDEX_MYSQL_PORT", "").strip()
        if port:
            try:
                config["port"] = int(port)
            except ValueError as exc:
                raise AuthorizationStoreError("YANDEX_MYSQL_PORT 必须是整数") from exc
        config["charset"] = "utf8mb4"
        config["cursorclass"] = pymysql.cursors.DictCursor
        config.setdefault("connect_timeout", 10)
        config.setdefault("read_timeout", 20)
        config.setdefault("write_timeout", 20)
        try:
            return pymysql.connect(**config)
        except Exception as exc:
            raise AuthorizationStoreError(f"无法连接中央授权数据库：{exc}") from exc

    @staticmethod
    def _ensure_tables(cursor) -> None:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS `yandex_store_authorizations` (
                `id` BIGINT NOT NULL AUTO_INCREMENT,
                `alias` VARCHAR(80) NOT NULL,
                `access_token` LONGTEXT NOT NULL,
                `token_fingerprint` CHAR(64) NOT NULL,
                `business_id` BIGINT NOT NULL,
                `business_name` VARCHAR(255) NOT NULL DEFAULT '',
                `campaign_id` BIGINT NOT NULL,
                `store_name` VARCHAR(255) NOT NULL DEFAULT '',
                `placement_type` VARCHAR(32) NOT NULL DEFAULT '',
                `api_availability` VARCHAR(32) NOT NULL DEFAULT '',
                `auth_scopes_json` LONGTEXT NOT NULL,
                `created_at` DATETIME NOT NULL,
                `updated_at` DATETIME NOT NULL,
                PRIMARY KEY (`id`),
                UNIQUE KEY `uniq_yandex_token_fingerprint` (`token_fingerprint`),
                UNIQUE KEY `uniq_yandex_campaign_id` (`campaign_id`),
                KEY `idx_yandex_business_id` (`business_id`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS `yandex_zeshun_authorizations` (
                `id` BIGINT NOT NULL AUTO_INCREMENT,
                `alias` VARCHAR(80) NOT NULL,
                `tg_code` VARCHAR(120) NOT NULL,
                `authorization_url` VARCHAR(4000) NOT NULL DEFAULT '',
                `store_id` BIGINT NULL,
                `token_updated_at` DATETIME NULL,
                `created_at` DATETIME NOT NULL,
                `updated_at` DATETIME NOT NULL,
                PRIMARY KEY (`id`),
                UNIQUE KEY `uniq_yandex_zeshun_tg_code` (`tg_code`),
                KEY `idx_yandex_zeshun_store_id` (`store_id`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )

    @staticmethod
    def _store_row(row: dict[str, Any] | None, *, include_secret: bool = False):
        if not row:
            return None
        result = dict(row)
        try:
            result["auth_scopes"] = json.loads(result.pop("auth_scopes_json") or "[]")
        except (TypeError, ValueError):
            result["auth_scopes"] = []
        result.pop("token_fingerprint", None)
        if not include_secret:
            result.pop("access_token", None)
        for key in ("created_at", "updated_at"):
            if result.get(key) is not None:
                result[key] = str(result[key])
        return result

    @staticmethod
    def _zeshun_row(row: dict[str, Any] | None):
        if not row:
            return None
        result = dict(row)
        result["authorized"] = bool(result.get("store_id") and result.get("token_updated_at"))
        for key in ("token_updated_at", "created_at", "updated_at"):
            if result.get(key) is not None:
                result[key] = str(result[key])
        return result

    def initialize(self) -> None:
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                self._ensure_tables(cursor)
            connection.commit()
        except Exception as exc:
            connection.rollback()
            if isinstance(exc, AuthorizationStoreError):
                raise
            raise AuthorizationStoreError(f"初始化中央授权数据库失败：{exc}") from exc
        finally:
            connection.close()

    def save_store(
        self,
        *,
        alias: str,
        token: str,
        token_fingerprint: str,
        store: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        now = _now()
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                self._ensure_tables(cursor)
                cursor.execute(
                    """
                    SELECT `id` FROM `yandex_store_authorizations`
                    WHERE `token_fingerprint` = %s OR `campaign_id` = %s
                    """,
                    (token_fingerprint, int(store["campaign_id"])),
                )
                ids = {int(row["id"]) for row in (cursor.fetchall() or [])}
                if len(ids) > 1:
                    raise ValueError("该 token 与店铺已分别关联其他授权记录，请先核对数据库")
                values = (
                    alias,
                    token,
                    token_fingerprint,
                    int(store["business_id"]),
                    str(store.get("business_name") or ""),
                    int(store["campaign_id"]),
                    str(store.get("store_name") or ""),
                    str(store.get("placement_type") or ""),
                    str(store.get("api_availability") or ""),
                    json.dumps(store.get("auth_scopes") or [], ensure_ascii=False),
                    now,
                )
                if ids:
                    store_id = ids.pop()
                    cursor.execute(
                        """
                        UPDATE `yandex_store_authorizations` SET
                            `alias`=%s, `access_token`=%s, `token_fingerprint`=%s,
                            `business_id`=%s, `business_name`=%s, `campaign_id`=%s,
                            `store_name`=%s, `placement_type`=%s,
                            `api_availability`=%s, `auth_scopes_json`=%s,
                            `updated_at`=%s WHERE `id`=%s
                        """,
                        values + (store_id,),
                    )
                    created = False
                else:
                    cursor.execute(
                        """
                        INSERT INTO `yandex_store_authorizations` (
                            `alias`, `access_token`, `token_fingerprint`, `business_id`,
                            `business_name`, `campaign_id`, `store_name`, `placement_type`,
                            `api_availability`, `auth_scopes_json`, `created_at`, `updated_at`
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        values[:-1] + (now, now),
                    )
                    store_id = int(cursor.lastrowid)
                    created = True
                cursor.execute(
                    "SELECT * FROM `yandex_store_authorizations` WHERE `id`=%s",
                    (store_id,),
                )
                row = cursor.fetchone()
            connection.commit()
            return self._store_row(row), created
        except (ValueError, KeyError):
            connection.rollback()
            raise
        except Exception as exc:
            connection.rollback()
            raise AuthorizationStoreError(f"保存 Yandex 授权失败：{exc}") from exc
        finally:
            connection.close()

    def list_stores(self) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                self._ensure_tables(cursor)
                cursor.execute(
                    "SELECT * FROM `yandex_store_authorizations` ORDER BY `alias`, `id`"
                )
                return [self._store_row(row) for row in (cursor.fetchall() or [])]
        except Exception as exc:
            raise AuthorizationStoreError(f"读取 Yandex 授权失败：{exc}") from exc
        finally:
            connection.close()

    def get_store(self, store_id: int, *, include_secret: bool = False):
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                self._ensure_tables(cursor)
                cursor.execute(
                    "SELECT * FROM `yandex_store_authorizations` WHERE `id`=%s LIMIT 1",
                    (int(store_id),),
                )
                return self._store_row(cursor.fetchone(), include_secret=include_secret)
        except Exception as exc:
            raise AuthorizationStoreError(f"读取 Yandex 授权失败：{exc}") from exc
        finally:
            connection.close()

    def update_store_alias(self, store_id: int, alias: str):
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                self._ensure_tables(cursor)
                cursor.execute(
                    "UPDATE `yandex_store_authorizations` SET `alias`=%s, `updated_at`=%s WHERE `id`=%s",
                    (alias, _now(), int(store_id)),
                )
                cursor.execute(
                    "UPDATE `yandex_zeshun_authorizations` SET `alias`=%s, `updated_at`=%s WHERE `store_id`=%s",
                    (alias, _now(), int(store_id)),
                )
                cursor.execute(
                    "SELECT * FROM `yandex_store_authorizations` WHERE `id`=%s",
                    (int(store_id),),
                )
                row = cursor.fetchone()
            connection.commit()
            return self._store_row(row)
        except Exception as exc:
            connection.rollback()
            raise AuthorizationStoreError(f"更新 Yandex 店铺名称失败：{exc}") from exc
        finally:
            connection.close()

    def update_store_connection(self, store_id: int, store: dict[str, Any]):
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                self._ensure_tables(cursor)
                cursor.execute(
                    """
                    UPDATE `yandex_store_authorizations` SET
                        `business_id`=%s, `business_name`=%s, `campaign_id`=%s,
                        `store_name`=%s, `placement_type`=%s,
                        `api_availability`=%s, `auth_scopes_json`=%s, `updated_at`=%s
                    WHERE `id`=%s
                    """,
                    (
                        int(store["business_id"]), str(store.get("business_name") or ""),
                        int(store["campaign_id"]), str(store.get("store_name") or ""),
                        str(store.get("placement_type") or ""),
                        str(store.get("api_availability") or ""),
                        json.dumps(store.get("auth_scopes") or [], ensure_ascii=False),
                        _now(), int(store_id),
                    ),
                )
                cursor.execute(
                    "SELECT * FROM `yandex_store_authorizations` WHERE `id`=%s",
                    (int(store_id),),
                )
                row = cursor.fetchone()
            connection.commit()
            return self._store_row(row)
        except Exception as exc:
            connection.rollback()
            raise AuthorizationStoreError(f"刷新 Yandex 店铺授权失败：{exc}") from exc
        finally:
            connection.close()

    def delete_store(self, store_id: int) -> bool:
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                self._ensure_tables(cursor)
                cursor.execute(
                    """
                    UPDATE `yandex_zeshun_authorizations`
                    SET `store_id`=NULL, `token_updated_at`=NULL, `updated_at`=%s
                    WHERE `store_id`=%s
                    """,
                    (_now(), int(store_id)),
                )
                cursor.execute(
                    "DELETE FROM `yandex_store_authorizations` WHERE `id`=%s",
                    (int(store_id),),
                )
                affected = cursor.rowcount
            connection.commit()
            return affected > 0
        except Exception as exc:
            connection.rollback()
            raise AuthorizationStoreError(f"删除 Yandex 授权失败：{exc}") from exc
        finally:
            connection.close()

    def create_zeshun_authorization(
        self, *, alias: str, tg_code: str, authorization_url: str
    ) -> dict[str, Any]:
        connection = self._connection()
        try:
            now = _now()
            with connection.cursor() as cursor:
                self._ensure_tables(cursor)
                cursor.execute(
                    """
                    INSERT INTO `yandex_zeshun_authorizations`
                        (`alias`,`tg_code`,`authorization_url`,`created_at`,`updated_at`)
                    VALUES (%s,%s,%s,%s,%s)
                    """,
                    (alias, tg_code, authorization_url, now, now),
                )
                record_id = int(cursor.lastrowid)
                cursor.execute(
                    "SELECT * FROM `yandex_zeshun_authorizations` WHERE `id`=%s",
                    (record_id,),
                )
                row = cursor.fetchone()
            connection.commit()
            return self._zeshun_row(row)
        except Exception as exc:
            connection.rollback()
            if "Duplicate" in str(exc) or "duplicate" in str(exc):
                raise ValueError("该 TG 码已经存在") from exc
            raise AuthorizationStoreError(f"保存 TG 授权记录失败：{exc}") from exc
        finally:
            connection.close()

    def list_zeshun_authorizations(self) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                self._ensure_tables(cursor)
                cursor.execute(
                    """
                    SELECT authorization.*, stores.`store_name`, stores.`business_name`,
                           stores.`campaign_id`, stores.`api_availability`
                    FROM `yandex_zeshun_authorizations` AS authorization
                    LEFT JOIN `yandex_store_authorizations` AS stores
                      ON stores.`id`=authorization.`store_id`
                    ORDER BY authorization.`alias`, authorization.`id`
                    """
                )
                return [self._zeshun_row(row) for row in (cursor.fetchall() or [])]
        except Exception as exc:
            raise AuthorizationStoreError(f"读取 TG 授权记录失败：{exc}") from exc
        finally:
            connection.close()

    def get_zeshun_authorization(self, authorization_id: int):
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                self._ensure_tables(cursor)
                cursor.execute(
                    "SELECT * FROM `yandex_zeshun_authorizations` WHERE `id`=%s LIMIT 1",
                    (int(authorization_id),),
                )
                return self._zeshun_row(cursor.fetchone())
        except Exception as exc:
            raise AuthorizationStoreError(f"读取 TG 授权记录失败：{exc}") from exc
        finally:
            connection.close()

    def update_zeshun_authorization(
        self, authorization_id: int, *, alias: str, authorization_url: str
    ):
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                self._ensure_tables(cursor)
                cursor.execute(
                    """
                    UPDATE `yandex_zeshun_authorizations`
                    SET `alias`=%s, `authorization_url`=%s, `updated_at`=%s WHERE `id`=%s
                    """,
                    (alias, authorization_url, _now(), int(authorization_id)),
                )
                cursor.execute(
                    "SELECT `store_id` FROM `yandex_zeshun_authorizations` WHERE `id`=%s",
                    (int(authorization_id),),
                )
                linked = cursor.fetchone()
                if linked and linked.get("store_id"):
                    cursor.execute(
                        "UPDATE `yandex_store_authorizations` SET `alias`=%s, `updated_at`=%s WHERE `id`=%s",
                        (alias, _now(), int(linked["store_id"])),
                    )
                cursor.execute(
                    "SELECT * FROM `yandex_zeshun_authorizations` WHERE `id`=%s",
                    (int(authorization_id),),
                )
                row = cursor.fetchone()
            connection.commit()
            return self._zeshun_row(row)
        except Exception as exc:
            connection.rollback()
            raise AuthorizationStoreError(f"更新 TG 授权记录失败：{exc}") from exc
        finally:
            connection.close()

    def complete_zeshun_authorization(self, authorization_id: int, *, store_id: int):
        connection = self._connection()
        try:
            now = _now()
            with connection.cursor() as cursor:
                self._ensure_tables(cursor)
                cursor.execute(
                    """
                    UPDATE `yandex_zeshun_authorizations`
                    SET `store_id`=%s, `token_updated_at`=%s, `updated_at`=%s WHERE `id`=%s
                    """,
                    (int(store_id), now, now, int(authorization_id)),
                )
                if not cursor.rowcount:
                    return None
                cursor.execute(
                    "SELECT * FROM `yandex_zeshun_authorizations` WHERE `id`=%s",
                    (int(authorization_id),),
                )
                row = cursor.fetchone()
            connection.commit()
            return self._zeshun_row(row)
        except Exception as exc:
            connection.rollback()
            raise AuthorizationStoreError(f"完成 TG 授权失败：{exc}") from exc
        finally:
            connection.close()

    def delete_zeshun_authorization(self, authorization_id: int) -> bool:
        connection = self._connection()
        try:
            with connection.cursor() as cursor:
                self._ensure_tables(cursor)
                cursor.execute(
                    "DELETE FROM `yandex_zeshun_authorizations` WHERE `id`=%s",
                    (int(authorization_id),),
                )
                affected = cursor.rowcount
            connection.commit()
            return affected > 0
        except Exception as exc:
            connection.rollback()
            raise AuthorizationStoreError(f"删除 TG 授权记录失败：{exc}") from exc
        finally:
            connection.close()

    def import_zeshun_authorization(
        self,
        *,
        alias: str,
        tg_code: str,
        authorization_url: str,
        store_id: int | None,
        authorized: bool,
    ) -> None:
        connection = self._connection()
        try:
            now = _now()
            with connection.cursor() as cursor:
                self._ensure_tables(cursor)
                cursor.execute(
                    """
                    INSERT INTO `yandex_zeshun_authorizations`
                        (`alias`,`tg_code`,`authorization_url`,`store_id`,`token_updated_at`,`created_at`,`updated_at`)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        `alias`=VALUES(`alias`), `authorization_url`=VALUES(`authorization_url`),
                        `store_id`=VALUES(`store_id`),
                        `token_updated_at`=VALUES(`token_updated_at`), `updated_at`=VALUES(`updated_at`)
                    """,
                    (
                        alias, tg_code, authorization_url, store_id,
                        now if authorized and store_id else None, now, now,
                    ),
                )
            connection.commit()
        except Exception as exc:
            connection.rollback()
            raise AuthorizationStoreError(f"迁移 TG 授权记录失败：{exc}") from exc
        finally:
            connection.close()


authorization_store = CentralAuthorizationStore()


def migrate_legacy_authorizations(local_database) -> dict[str, int]:
    """Move old DPAPI/SQLite credentials to MySQL, then erase local copies.

    The local rows are retained if any import fails, so startup can be retried
    without losing authorization data.
    """
    from yandex.app.secret_store import secret_fingerprint, unprotect_secret

    local_stores = local_database.list_legacy_stores_with_secrets()
    local_zeshun = local_database.list_zeshun_authorizations()
    if not local_stores and not local_zeshun:
        return {"stores": 0, "zeshun": 0}

    store_id_map: dict[int, int] = {}
    for row in local_stores:
        token = unprotect_secret(bytes(row["encrypted_token"]))
        stored, _ = authorization_store.save_store(
            alias=str(row.get("alias") or "Yandex 店铺"),
            token=token,
            token_fingerprint=secret_fingerprint(token),
            store={
                "business_id": row["business_id"],
                "business_name": row.get("business_name") or "",
                "campaign_id": row["campaign_id"],
                "store_name": row.get("store_name") or "",
                "placement_type": row.get("placement_type") or "",
                "api_availability": row.get("api_availability") or "",
                "auth_scopes": row.get("auth_scopes") or [],
            },
        )
        store_id_map[int(row["id"])] = int(stored["id"])

    for row in local_zeshun:
        old_store_id = int(row["store_id"]) if row.get("store_id") else None
        new_store_id = store_id_map.get(old_store_id) if old_store_id else None
        authorization_store.import_zeshun_authorization(
            alias=str(row.get("alias") or "Yandex 店铺"),
            tg_code=str(row.get("tg_code") or ""),
            authorization_url=str(row.get("authorization_url") or ""),
            store_id=new_store_id,
            authorized=bool(row.get("authorized")),
        )

    local_database.clear_legacy_authorizations()
    return {"stores": len(local_stores), "zeshun": len(local_zeshun)}
