"""Persist Mercado Libre source-page and ZYing extension snapshots in MySQL."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from erp.mercadolibre_follow_sell import extract_item_id


DEFAULT_TABLE = "erp_mercadolibre_source_items"
TABLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
PACKAGE_ATTRIBUTE_IDS = {
    "package_length_cm": "PACKAGE_LENGTH",
    "package_width_cm": "PACKAGE_WIDTH",
    "package_height_cm": "PACKAGE_HEIGHT",
    "weight_g": "PACKAGE_WEIGHT",
}


class SourceStoreError(RuntimeError):
    """A source snapshot could not be normalized or persisted."""


def _validate_table_name(table_name: str) -> str:
    name = str(table_name or "").strip()
    if not TABLE_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"非法数据表名: {table_name!r}")
    return name


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).strip().replace(",", "."))
    except Exception as exc:
        raise SourceStoreError(f"无法识别数值: {value!r}") from exc


def normalize_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one browser/API snapshot into database columns."""
    source = dict(snapshot.get("source") or {})
    source_url = str(snapshot.get("source_url") or source.get("permalink") or "").strip()
    item_hint = str(snapshot.get("item_id") or source.get("id") or source_url)
    try:
        item_id = extract_item_id(item_hint)
    except ValueError as exc:
        raise SourceStoreError(str(exc)) from exc

    description_value = snapshot.get("description")
    if isinstance(description_value, Mapping):
        description = dict(description_value)
    elif description_value in (None, ""):
        description = {}
    else:
        description = {"plain_text": str(description_value)}

    plugin_snapshot = snapshot.get("plugin_snapshot") or {}
    page_snapshot = snapshot.get("page_snapshot") or {}
    scraped_at = snapshot.get("scraped_at") or datetime.now().replace(microsecond=0)
    if isinstance(scraped_at, datetime):
        scraped_at = scraped_at.strftime("%Y-%m-%d %H:%M:%S")

    pictures = list(source.get("pictures") or snapshot.get("pictures") or [])
    main_image_url = str(snapshot.get("main_image_url") or "").strip()
    if not pictures and main_image_url:
        pictures = [{"source": main_image_url}]
    normalized = {
        "item_id": item_id,
        "source_url": source_url or f"https://articulo.mercadolibre.com.mx/{item_id}",
        "final_url": str(snapshot.get("final_url") or source.get("permalink") or "").strip(),
        "site_id": str(item_id[:3] or source.get("site_id") or snapshot.get("site_id") or "MLM"),
        "category_id": str(source.get("category_id") or snapshot.get("category_id") or ""),
        "title": str(source.get("title") or snapshot.get("title") or "").strip(),
        "subtitle": str(source.get("subtitle") or snapshot.get("subtitle") or "").strip(),
        "price": _decimal_or_none(source.get("price", snapshot.get("price"))),
        "currency_id": str(source.get("currency_id") or snapshot.get("currency_id") or "MXN"),
        "condition_id": str(source.get("condition") or snapshot.get("condition") or "new"),
        "available_quantity": source.get("available_quantity", snapshot.get("available_quantity")),
        "description_text": str(
            description.get("plain_text") or description.get("text") or ""
        ).strip(),
        "pictures": pictures,
        "attributes": list(source.get("attributes") or snapshot.get("attributes") or []),
        "variations": list(source.get("variations") or snapshot.get("variations") or []),
        "sale_terms": list(source.get("sale_terms") or snapshot.get("sale_terms") or []),
        "weight_g": _decimal_or_none(snapshot.get("weight_g")),
        "package_length_cm": _decimal_or_none(snapshot.get("package_length_cm")),
        "package_width_cm": _decimal_or_none(snapshot.get("package_width_cm")),
        "package_height_cm": _decimal_or_none(snapshot.get("package_height_cm")),
        "source": source,
        "description": description,
        "page_snapshot": page_snapshot,
        "plugin_snapshot": plugin_snapshot,
        "scrape_status": str(snapshot.get("scrape_status") or "ok").strip()[:32],
        "error_message": str(snapshot.get("error_message") or "").strip(),
        "scraped_at": scraped_at,
    }
    return normalized


def ensure_source_table(cursor: Any, table_name: str = DEFAULT_TABLE) -> None:
    """Create the source snapshot table when it does not exist."""
    table = _validate_table_name(table_name)
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{table}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `item_id` VARCHAR(32) NOT NULL,
            `source_url` VARCHAR(1000) NOT NULL,
            `final_url` VARCHAR(1000) NULL,
            `site_id` VARCHAR(16) NULL,
            `category_id` VARCHAR(64) NULL,
            `title` VARCHAR(255) NULL,
            `subtitle` VARCHAR(255) NULL,
            `price` DECIMAL(20,4) NULL,
            `currency_id` VARCHAR(16) NULL,
            `condition_id` VARCHAR(32) NULL,
            `available_quantity` INT NULL,
            `description_text` LONGTEXT NULL,
            `pictures_json` LONGTEXT NULL,
            `attributes_json` LONGTEXT NULL,
            `variations_json` LONGTEXT NULL,
            `sale_terms_json` LONGTEXT NULL,
            `weight_g` DECIMAL(20,4) NULL,
            `package_length_cm` DECIMAL(20,4) NULL,
            `package_width_cm` DECIMAL(20,4) NULL,
            `package_height_cm` DECIMAL(20,4) NULL,
            `source_json` LONGTEXT NULL,
            `description_json` LONGTEXT NULL,
            `page_snapshot_json` LONGTEXT NULL,
            `plugin_snapshot_json` LONGTEXT NULL,
            `scrape_status` VARCHAR(32) NOT NULL,
            `error_message` TEXT NULL,
            `scraped_at` DATETIME NOT NULL,
            `target_user_id` BIGINT NULL,
            `published_global_item_id` VARCHAR(32) NULL,
            `published_site_item_id` VARCHAR(32) NULL,
            `parent_user_product_id` VARCHAR(32) NULL,
            `publish_status` VARCHAR(32) NULL,
            `publish_result_json` LONGTEXT NULL,
            `published_at` DATETIME NULL,
            `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_erp_meli_source_item` (`item_id`),
            KEY `idx_erp_meli_source_status` (`scrape_status`, `scraped_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(f"SHOW COLUMNS FROM `{table}`")
    existing = {
        str(row.get("Field") if isinstance(row, Mapping) else row[0])
        for row in (cursor.fetchall() or [])
    }
    publish_columns = {
        "target_user_id": "BIGINT NULL",
        "published_global_item_id": "VARCHAR(32) NULL",
        "published_site_item_id": "VARCHAR(32) NULL",
        "parent_user_product_id": "VARCHAR(32) NULL",
        "publish_status": "VARCHAR(32) NULL",
        "publish_result_json": "LONGTEXT NULL",
        "published_at": "DATETIME NULL",
    }
    for column, definition in publish_columns.items():
        if column not in existing:
            cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")


def _default_connection_factory() -> Any:
    import pymysql
    from bit.bit_mysql import config as mysql_config

    return pymysql.connect(**mysql_config)


def upsert_source_snapshot(
    snapshot: Mapping[str, Any],
    *,
    table_name: str = DEFAULT_TABLE,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Insert or update a source snapshot and return its normalized values."""
    table = _validate_table_name(table_name)
    record = normalize_snapshot(snapshot)
    connection = (connection_factory or _default_connection_factory)()
    values = (
        record["item_id"],
        record["source_url"],
        record["final_url"],
        record["site_id"],
        record["category_id"],
        record["title"],
        record["subtitle"],
        record["price"],
        record["currency_id"],
        record["condition_id"],
        record["available_quantity"],
        record["description_text"],
        _json_dumps(record["pictures"]),
        _json_dumps(record["attributes"]),
        _json_dumps(record["variations"]),
        _json_dumps(record["sale_terms"]),
        record["weight_g"],
        record["package_length_cm"],
        record["package_width_cm"],
        record["package_height_cm"],
        _json_dumps(record["source"]),
        _json_dumps(record["description"]),
        _json_dumps(record["page_snapshot"]),
        _json_dumps(record["plugin_snapshot"]),
        record["scrape_status"],
        record["error_message"],
        record["scraped_at"],
    )
    try:
        with connection.cursor() as cursor:
            ensure_source_table(cursor, table)
            cursor.execute(
                f"""
                INSERT INTO `{table}` (
                    `item_id`, `source_url`, `final_url`, `site_id`, `category_id`,
                    `title`, `subtitle`, `price`, `currency_id`, `condition_id`,
                    `available_quantity`, `description_text`, `pictures_json`,
                    `attributes_json`, `variations_json`, `sale_terms_json`,
                    `weight_g`, `package_length_cm`, `package_width_cm`,
                    `package_height_cm`, `source_json`, `description_json`,
                    `page_snapshot_json`, `plugin_snapshot_json`, `scrape_status`,
                    `error_message`, `scraped_at`
                ) VALUES ({", ".join(["%s"] * 27)})
                ON DUPLICATE KEY UPDATE
                    `source_url` = VALUES(`source_url`),
                    `final_url` = VALUES(`final_url`),
                    `site_id` = VALUES(`site_id`),
                    `category_id` = VALUES(`category_id`),
                    `title` = VALUES(`title`),
                    `subtitle` = VALUES(`subtitle`),
                    `price` = VALUES(`price`),
                    `currency_id` = VALUES(`currency_id`),
                    `condition_id` = VALUES(`condition_id`),
                    `available_quantity` = VALUES(`available_quantity`),
                    `description_text` = VALUES(`description_text`),
                    `pictures_json` = VALUES(`pictures_json`),
                    `attributes_json` = VALUES(`attributes_json`),
                    `variations_json` = VALUES(`variations_json`),
                    `sale_terms_json` = VALUES(`sale_terms_json`),
                    `weight_g` = VALUES(`weight_g`),
                    `package_length_cm` = VALUES(`package_length_cm`),
                    `package_width_cm` = VALUES(`package_width_cm`),
                    `package_height_cm` = VALUES(`package_height_cm`),
                    `source_json` = VALUES(`source_json`),
                    `description_json` = VALUES(`description_json`),
                    `page_snapshot_json` = VALUES(`page_snapshot_json`),
                    `plugin_snapshot_json` = VALUES(`plugin_snapshot_json`),
                    `scrape_status` = VALUES(`scrape_status`),
                    `error_message` = VALUES(`error_message`),
                    `scraped_at` = VALUES(`scraped_at`)
                """,
                values,
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return record


def _package_attributes(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for column, attribute_id in PACKAGE_ATTRIBUTE_IDS.items():
        value = row.get(column)
        if value in (None, ""):
            continue
        unit = "g" if column == "weight_g" else "cm"
        number = format(Decimal(str(value)), "f")
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        result.append({"id": attribute_id, "value_name": f"{number} {unit}"})
    return result


def _merge_package_attributes(
    attributes: list[dict[str, Any]], row: Mapping[str, Any]
) -> list[dict[str, Any]]:
    package_ids = set(PACKAGE_ATTRIBUTE_IDS.values())
    merged = [item for item in attributes if str(item.get("id")) not in package_ids]
    merged.extend(_package_attributes(row))
    return merged


def load_source_snapshot(
    item_or_url: str,
    *,
    table_name: str = DEFAULT_TABLE,
    connection_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Load the most recent database snapshot for one Mercado Libre item."""
    table = _validate_table_name(table_name)
    item_id = extract_item_id(item_or_url)
    connection = (connection_factory or _default_connection_factory)()
    try:
        with connection.cursor() as cursor:
            ensure_source_table(cursor, table)
            cursor.execute(f"SELECT * FROM `{table}` WHERE `item_id` = %s", (item_id,))
            row = cursor.fetchone()
        connection.commit()
    finally:
        connection.close()
    if not row:
        raise SourceStoreError(f"数据库中没有源商品 {item_id}")
    return dict(row)


def load_listing_for_publish(
    item_or_url: str,
    *,
    table_name: str = DEFAULT_TABLE,
    connection_factory: Callable[[], Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reconstruct the API-like item and description used by payload builders."""
    row = load_source_snapshot(
        item_or_url, table_name=table_name, connection_factory=connection_factory
    )
    source = dict(_json_loads(row.get("source_json"), {}))
    source.update(
        {
            "id": row["item_id"],
            "site_id": row.get("site_id") or "MLM",
            "title": row.get("title") or source.get("title"),
            "category_id": row.get("category_id") or source.get("category_id"),
            "price": row.get("price") if row.get("price") is not None else source.get("price"),
            "currency_id": row.get("currency_id") or source.get("currency_id") or "MXN",
            "condition": row.get("condition_id") or source.get("condition") or "new",
            "available_quantity": row.get("available_quantity"),
            "pictures": _json_loads(row.get("pictures_json"), source.get("pictures") or []),
            "variations": _json_loads(row.get("variations_json"), source.get("variations") or []),
            "sale_terms": _json_loads(row.get("sale_terms_json"), source.get("sale_terms") or []),
        }
    )
    attributes = _json_loads(row.get("attributes_json"), source.get("attributes") or [])
    source["attributes"] = _merge_package_attributes(list(attributes), row)
    description = dict(_json_loads(row.get("description_json"), {}))
    if not description and row.get("description_text"):
        description = {"plain_text": row["description_text"]}
    return source, description


def record_publish_result(
    item_or_url: str,
    result: Mapping[str, Any],
    *,
    target_user_id: int | str | None = None,
    table_name: str = DEFAULT_TABLE,
    connection_factory: Callable[[], Any] | None = None,
) -> None:
    """Attach a successful Global Selling publication to its source record."""
    table = _validate_table_name(table_name)
    item_id = extract_item_id(item_or_url)
    site_items = result.get("site_items") or []
    site_item_id = ""
    if isinstance(site_items, list):
        for site_item in site_items:
            if isinstance(site_item, Mapping) and site_item.get("item_id"):
                site_item_id = str(site_item["item_id"])
                if str(site_item.get("site_id") or "") == "MLM":
                    break
    connection = (connection_factory or _default_connection_factory)()
    try:
        with connection.cursor() as cursor:
            ensure_source_table(cursor, table)
            affected = cursor.execute(
                f"""
                UPDATE `{table}` SET
                    `target_user_id` = %s,
                    `published_global_item_id` = %s,
                    `published_site_item_id` = %s,
                    `parent_user_product_id` = %s,
                    `publish_status` = %s,
                    `publish_result_json` = %s,
                    `published_at` = %s
                WHERE `item_id` = %s
                """,
                (
                    target_user_id or result.get("seller_id"),
                    str(result.get("item_id") or ""),
                    site_item_id,
                    str(result.get("parent_user_product_id") or ""),
                    "published",
                    _json_dumps(result),
                    datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
                    item_id,
                ),
            )
            if not affected:
                raise SourceStoreError(f"数据库中没有可关联的源商品 {item_id}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="保存或读取 Mercado Libre 源商品抓取快照")
    parser.add_argument("--input-json", type=Path, help="待保存的 UTF-8 JSON 文件")
    parser.add_argument("--show", help="读取商品编号或链接并输出（不包含数据库配置）")
    parser.add_argument("--table", default=DEFAULT_TABLE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.input_json:
            payload = json.loads(args.input_json.read_text(encoding="utf-8"))
            record = upsert_source_snapshot(payload, table_name=args.table)
            print(f"已保存源商品 {record['item_id']}，状态: {record['scrape_status']}")
            return 0
        if args.show:
            source, description = load_listing_for_publish(args.show, table_name=args.table)
            print(json.dumps({"source": source, "description": description}, ensure_ascii=False, indent=2, default=str))
            return 0
        raise SourceStoreError("请使用 --input-json 保存，或使用 --show 读取")
    except (OSError, ValueError, SourceStoreError, json.JSONDecodeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
