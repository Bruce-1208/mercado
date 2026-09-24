"""Uploaded order measurements and explicit Mercado Libre listing updates."""
from __future__ import annotations

import io
import re
import threading
import uuid
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

from bit import bit_mysql
from bit.bit_store_link_sync import _client_and_token
from mercado_api.client import MercadoLibreClient


MAX_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_ORDER_ROWS = 3000
_lock = threading.RLock()
_tasks: dict[str, dict[str, Any]] = {}

HEADERS = {
    "time": ("时间", "下单时间"),
    "order_number": ("编号", "订单号", "销售编号"),
    "salesperson": ("业务员",),
    "source": ("来源",),
    "company_store": ("公司店铺", "店铺"),
    "product_id": ("产品id", "产品ID", "产品号"),
    "category": ("产品分类", "产品类别"),
    "title": ("标题", "产品标题"),
    "image_url": ("图片", "主图"),
    "shipment_id": ("运单号",),
    "tracking_number": ("追踪号", "物流单号"),
    "carrier": ("运输商", "物流商"),
    "region": ("地区", "站点"),
}

PUBLIC_COLUMNS = (
    ("time", "时间"),
    ("image_url", "图片"),
    ("order_number", "编号"),
    ("salesperson", "业务员"),
    ("source", "来源"),
    ("company_store", "公司店铺"),
    ("product_id", "产品id"),
    ("category", "产品分类"),
    ("title", "标题"),
    ("shipment_id", "运单号"),
    ("tracking_number", "追踪号"),
    ("carrier", "运输商"),
    ("region", "地区"),
    ("declared_weight_g", "当前标记重量（克）"),
    ("declared_dimensions_cm", "当前标记尺寸（厘米）"),
    ("declared_freight", "当前运费"),
    ("actual_weight_g", "实际重量（克）"),
    ("actual_dimensions_cm", "实际尺寸（厘米）"),
    ("actual_freight", "实际运费"),
    ("marketplace_item_id", "美客多产品编号"),
    ("query_status", "查询状态"),
    ("query_error", "说明"),
)
EXPORT_COLUMNS = PUBLIC_COLUMNS + (
    ("current_net_proceeds_usd", "本次净收益（USD）"),
    ("execution_status", "执行状态"),
    ("execution_error", "执行说明"),
)

_COUNTRY_TO_SITE = {
    "墨西哥": "MLM", "巴西": "MLB", "智利": "MLC", "哥伦比亚": "MCO",
    "阿根廷": "MLA", "乌拉圭": "MLU", "秘鲁": "MPE", "厄瓜多尔": "MEC",
}
_PACKAGE_ATTRIBUTES = {
    "PACKAGE_WEIGHT": ("weight", "g"),
    "PACKAGE_LENGTH": ("length", "cm"),
    "PACKAGE_WIDTH": ("width", "cm"),
    "PACKAGE_HEIGHT": ("height", "cm"),
}


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return str(value).strip()


def read_order_file(file_bytes: bytes, filename: str) -> list[dict[str, Any]]:
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError("文件不能超过 16 MB")
    if not str(filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise ValueError("请上传 .xlsx 或 .xlsm 订单文件")
    book = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    try:
        rows = book.active.values
        header = next(rows, None)
        if not header:
            raise ValueError("订单文件没有表头")
        header_map = {str(value or "").strip().casefold(): index for index, value in enumerate(header)}
        columns: dict[str, int] = {}
        missing = []
        for field, aliases in HEADERS.items():
            index = next((header_map.get(name.casefold()) for name in aliases if name.casefold() in header_map), None)
            if index is None and field in {"order_number", "company_store"}:
                missing.append(aliases[0])
            if index is not None:
                columns[field] = index
        if missing:
            raise ValueError("订单文件缺少必需列：" + "、".join(missing))
        records = []
        for row_number, row in enumerate(rows, start=2):
            record = {field: _cell_text(row[index] if index < len(row) else None)
                      for field, index in columns.items()}
            record["order_number"] = re.sub(r"\.0$", "", record.get("order_number", ""))
            if not record["order_number"]:
                continue
            if len(records) >= MAX_ORDER_ROWS:
                raise ValueError(f"每次最多处理 {MAX_ORDER_ROWS} 条订单")
            record.update({
                "source_row": row_number,
                "declared_weight_g": "",
                "declared_dimensions_cm": "",
                "declared_freight": "",
                "actual_weight_g": "",
                "actual_dimensions_cm": "",
                "actual_freight": "",
                "marketplace_item_id": "",
                "query_status": "等待查询",
                "query_error": "",
                "_token_id": None,
                "_global_item_id": "",
                "_net_proceeds_usd": None,
                "_package_status": "",
                "_net_status": "",
            })
            records.append(record)
        if not records:
            raise ValueError("文件中没有带订单编号的有效订单行")
        return records
    finally:
        book.close()


def _store_map(store_rows: list[Mapping[str, Any]], allowed_token_ids: set[int] | None):
    by_alias: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for store in store_rows:
        token_id = int(store.get("id") or 0)
        if not token_id or not store.get("enabled", True):
            continue
        if allowed_token_ids is not None and token_id not in allowed_token_ids:
            continue
        for alias in (store.get("display_name"), store.get("nickname")):
            key = str(alias or "").strip().casefold()
            if key and store not in by_alias[key]:
                by_alias[key].append(store)
    return by_alias


def _resolve_store(record: Mapping[str, Any], store_map):
    full_name = str(record.get("company_store") or "").strip()
    suffix = re.search(r"\(([^()]*)\)\s*$", full_name)
    country = suffix.group(1).strip() if suffix else ""
    base = re.sub(r"\s*\([^()]*\)\s*$", "", full_name).strip()
    candidates = list(store_map.get(base.casefold()) or store_map.get(full_name.casefold()) or [])
    if not candidates:
        return None
    site_id = _COUNTRY_TO_SITE.get(country)
    if site_id:
        site_candidates = [
            row for row in candidates
            if site_id in {str(setting.get("site_id") or "").upper()
                           for setting in row.get("site_settings") or []}
        ]
        if site_candidates:
            candidates = site_candidates
    return candidates[0] if len(candidates) == 1 else None


def _measurement(data: Mapping[str, Any], package_key: str) -> tuple[str, str]:
    package = (data.get("package") or {}).get(package_key) or {}
    weight = package.get("weight") or package.get("weights") or {}
    dims = package.get("dimensions") or {}
    if str(weight.get("unit") or "").lower() != "g" or str(dims.get("unit") or "").lower() != "cm":
        raise ValueError("运费补差接口没有提供克和厘米单位的重量尺寸")
    values = [weight.get("net"), dims.get("length"), dims.get("width"), dims.get("height")]
    numbers = []
    for raw in values:
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("运费补差接口缺少有效重量或尺寸") from exc
        if not value.is_finite() or value <= 0:
            raise ValueError("运费补差接口返回了非正数重量或尺寸")
        numbers.append(format(value.normalize(), "f"))
    return numbers[0], "x".join(numbers[1:])


def _money(costs: Mapping[str, Any], section: str) -> str:
    sender = ((costs.get(section) or {}).get("sender") or {})
    value = sender.get("cost")
    if value in (None, ""):
        return ""
    currency = str(costs.get("currency_id") or "").strip()
    return f"{value} {currency}".strip()


def _match_order_item(client: MercadoLibreClient, pack: Mapping[str, Any], product_id: str):
    wanted = str(product_id or "").strip().casefold()
    candidates = []
    for order_ref in pack.get("orders") or []:
        order_id = str((order_ref or {}).get("id") or "").strip()
        if not order_id:
            continue
        order = client.get_order(order_id)
        for order_line in order.get("order_items") or []:
            item = order_line.get("item") or {}
            candidates.append((item, order_line, order))
    if not candidates:
        raise ValueError("订单明细中没有可更新的产品")
    matching = [
        candidate for candidate in candidates
        if wanted and wanted in {
            str(candidate[0].get("seller_sku") or "").strip().casefold(),
            str(candidate[0].get("id") or "").strip().casefold(),
            str(candidate[0].get("parent_item_id") or "").strip().casefold(),
        }
    ]
    if len(matching) == 1:
        return matching[0]
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError("包裹含多个产品，且无法用产品 id 唯一匹配商品明细")


def _extract_net_proceeds(item: Mapping[str, Any]) -> Decimal | None:
    values = item.get("net_proceeds")
    for candidate in values if isinstance(values, list) else [values]:
        if not isinstance(candidate, Mapping):
            continue
        if str(candidate.get("currency_id") or "").upper() != "USD":
            continue
        try:
            amount = Decimal(str(candidate.get("amount")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if amount.is_finite() and amount > 0:
            return amount
    return None


def _run_read(task_id: str, store_rows, allowed_token_ids):
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return
        records = task["records"]
        task.update(status="querying", message="正在查询美客多订单与运费补差数据", total=len(records))
    store_map = _store_map(store_rows, allowed_token_ids)
    token_clients: dict[int, MercadoLibreClient] = {}
    token_errors: dict[int, str] = {}
    for index, record in enumerate(records, start=1):
        try:
            store = _resolve_store(record, store_map)
            if not store:
                raise ValueError("无法按公司店铺和站点匹配当前用户可用的店铺授权")
            token_id = int(store["id"])
            record["_token_id"] = token_id
            if token_id not in token_clients:
                if token_id not in token_errors:
                    try:
                        token = bit_mysql.get_mercado_store_token(token_id)
                        if not token:
                            raise ValueError("店铺授权不存在")
                        token_clients[token_id], _ = _client_and_token(dict(token))
                    except Exception as exc:
                        token_errors[token_id] = str(exc)
                if token_id in token_errors:
                    raise ValueError("店铺授权不可用：" + token_errors[token_id])
            client = token_clients[token_id]
            pack = client.request("GET", f"/marketplace/orders/pack/{record['order_number']}")
            if str(pack.get("id")) != record["order_number"]:
                raise ValueError("接口返回的包裹号与上传文件不一致")
            shipment_id = str((pack.get("shipment") or {}).get("id") or "").strip()
            if not shipment_id:
                raise ValueError("订单包裹没有运单号")
            compensation = client.request(
                "GET", f"/marketplace/shipments/{shipment_id}/compensation_costs",
                params={"weight_unit": "g", "dimensions_unit": "cm"},
            )
            declared_weight, declared_dims = _measurement(compensation, "declared")
            actual_weight, actual_dims = _measurement(compensation, "validated")
            record["declared_weight_g"] = declared_weight
            record["declared_dimensions_cm"] = declared_dims
            record["declared_freight"] = _money(compensation.get("costs") or {}, "declared")
            record["actual_weight_g"] = actual_weight
            record["actual_dimensions_cm"] = actual_dims
            record["actual_freight"] = _money(compensation.get("costs") or {}, "validated")
            record["shipment_id"] = record.get("shipment_id") or shipment_id
            item, order_line, _order = _match_order_item(client, pack, record.get("product_id", ""))
            marketplace_item_id = str(item.get("id") or "").strip()
            global_item_id = str(item.get("parent_item_id") or "").strip()
            if not marketplace_item_id:
                raise ValueError("订单明细没有美客多产品编号")
            item_detail = client.get_marketplace_item(
                marketplace_item_id,
                attributes=("id", "title", "secure_thumbnail", "thumbnail", "attributes", "net_proceeds", "cbt_item_id"),
            )
            record["marketplace_item_id"] = marketplace_item_id
            record["_global_item_id"] = global_item_id or str(item_detail.get("cbt_item_id") or "").strip()
            record["_net_proceeds_usd"] = _extract_net_proceeds(item_detail)
            record["title"] = record.get("title") or str(item.get("title") or item_detail.get("title") or "")
            record["image_url"] = record.get("image_url") or str(
                item_detail.get("secure_thumbnail") or item_detail.get("thumbnail") or ""
            )
            record["category"] = record.get("category") or str(item.get("category_id") or "")
            record["query_status"] = "已读取"
            if not record["_global_item_id"]:
                record["query_error"] = "商品没有可更新的 Global Selling 产品编号"
        except Exception as exc:
            record["query_status"] = "查询失败"
            record["query_error"] = str(exc)[:300]
        with _lock:
            task["processed"] = index
            task["message"] = f"已查询 {index}/{len(records)} 条订单"
    with _lock:
        task["status"] = "ready"
        task["finished_at"] = datetime.now().isoformat(timespec="seconds")
        task["message"] = f"查询完成，共 {len(records)} 条订单"


def start_upload(file_bytes: bytes, filename: str, owner: str,
                 store_rows: list[Mapping[str, Any]], allowed_token_ids: set[int] | None = None) -> dict[str, Any]:
    records = read_order_file(file_bytes, filename)
    task_id = uuid.uuid4().hex
    with _lock:
        for old_id in list(_tasks):
            if _tasks[old_id].get("owner") == owner and _tasks[old_id].get("status") in {"ready", "failed"}:
                del _tasks[old_id]
        _tasks[task_id] = {
            "task_id": task_id, "owner": owner, "status": "queued",
            "message": "订单文件已接收", "total": len(records), "processed": 0,
            "records": records, "execute_status": "idle", "execute_message": "",
            "execute_processed": 0, "execute_total": 0, "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    thread = threading.Thread(
        target=_run_read, args=(task_id, list(store_rows), allowed_token_ids),
        name=f"weight-dimensions-{task_id[:8]}", daemon=True,
    )
    thread.start()
    return {"task_id": task_id, "status": "queued", "total": len(records)}


def _get_owned_task(task_id: str, owner: str):
    with _lock:
        task = _tasks.get(str(task_id or ""))
        if not task or task.get("owner") != owner:
            raise KeyError("查询任务不存在或已过期")
        return task


def _public_record(row: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: row.get(key, "") for key, _label in PUBLIC_COLUMNS}
    result["execution_status"] = row.get("execution_status", "")
    result["execution_error"] = row.get("execution_error", "")
    result["current_net_proceeds_usd"] = (
        str(row["_net_proceeds_usd"]) if row.get("_net_proceeds_usd") is not None else ""
    )
    result["can_execute"] = bool(
        row.get("query_status") == "已读取" and row.get("_token_id")
        and row.get("_global_item_id") and row.get("marketplace_item_id")
        and row.get("_net_proceeds_usd") is not None
    )
    return result


def task_status(task_id: str, owner: str) -> dict[str, Any]:
    task = _get_owned_task(task_id, owner)
    with _lock:
        return {
            "task_id": task["task_id"], "status": task["status"],
            "message": task.get("message", ""), "total": task.get("total", 0),
            "processed": task.get("processed", 0), "records": [_public_record(row) for row in task["records"]],
            "created_at": task.get("created_at", ""), "finished_at": task.get("finished_at", ""),
            "execute_status": task.get("execute_status", "idle"),
            "execute_message": task.get("execute_message", ""),
            "execute_processed": task.get("execute_processed", 0),
            "execute_total": task.get("execute_total", 0),
        }


def export_xlsx(task_id: str, owner: str) -> bytes:
    task = _get_owned_task(task_id, owner)
    if task.get("status") != "ready":
        raise ValueError("查询完成后才能导出")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "重量尺寸变高记录"
    labels = [label for _key, label in EXPORT_COLUMNS]
    sheet.append(labels)
    for row in task["records"]:
        values = []
        for key, _label in EXPORT_COLUMNS:
            if key == "current_net_proceeds_usd":
                value = row.get("_net_proceeds_usd")
                values.append(str(value) if value is not None else "")
            else:
                values.append(row.get(key, ""))
        sheet.append(values)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    widths = [20, 24, 20, 16, 24, 28, 16, 18, 46, 20, 26, 18, 16, 18, 22, 18, 18, 22, 18, 22, 16, 44, 22, 18, 44]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[chr(64 + index)].width = width
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="203E5B")
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.number_format = "@" if cell.column in {1, 3, 7, 10, 11, 20} else "General"
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _package_attributes(client: MercadoLibreClient, global_item_id: str,
                        weight_g: str, dimensions_cm: str) -> None:
    pieces = [Decimal(part) for part in str(dimensions_cm).lower().replace("×", "x").split("x")]
    if len(pieces) != 3:
        raise ValueError("实际尺寸格式应为 长x宽x高")
    values = {
        "PACKAGE_WEIGHT": (Decimal(str(weight_g)), "g"),
        "PACKAGE_LENGTH": (pieces[0], "cm"),
        "PACKAGE_WIDTH": (pieces[1], "cm"),
        "PACKAGE_HEIGHT": (pieces[2], "cm"),
    }
    for number, unit in values.values():
        if not number.is_finite() or number <= 0:
            raise ValueError("重量和尺寸必须是大于 0 的有效数值")
    if any(value < 3 for value in pieces) or values["PACKAGE_WEIGHT"][0] < 50:
        raise ValueError("美客多要求包装长宽高至少 3 厘米、重量至少 50 克")
    remote = client.request("GET", f"/global/items/{global_item_id}")
    attrs = list(remote.get("attributes") or [])
    updates = {
        key: {"id": key, "value_name": f"{format(number.normalize(), 'f')} {unit}"}
        for key, (number, unit) in values.items()
    }
    seen = set()
    merged = []
    for attribute in attrs:
        attribute_id = str((attribute or {}).get("id") or "")
        if attribute_id in updates:
            merged.append({**attribute, **updates[attribute_id]})
            seen.add(attribute_id)
        else:
            merged.append(attribute)
    for key, value in updates.items():
        if key not in seen:
            merged.append(value)
    client.update_global_item(global_item_id, {"attributes": merged})


def _run_execute(task_id: str, owner: str):
    task = _get_owned_task(task_id, owner)
    with _lock:
        records = task["records"]
        task["execute_status"] = "running"
        task["execute_message"] = "正在更新商品重量尺寸"
    dim_groups: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    net_groups: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    invalid = []
    for row in records:
        if not row.get("can_execute") and not (
            row.get("query_status") == "已读取" and row.get("_token_id")
            and row.get("_global_item_id") and row.get("marketplace_item_id")
        ):
            continue
        if row.get("_net_proceeds_usd") is None:
            row["execution_status"] = "未执行"
            row["execution_error"] = "商品当前没有可读取的 USD 净收益，无法按原净收益重提"
            invalid.append(row)
            continue
        token_id = int(row["_token_id"])
        dim_groups[(token_id, row["_global_item_id"])].append(row)
        net_groups[(token_id, row["marketplace_item_id"])].append(row)
    conflicts = set()
    for key, grouped in dim_groups.items():
        variants = {(row["actual_weight_g"], row["actual_dimensions_cm"]) for row in grouped}
        if len(variants) > 1:
            conflicts.add(key)
            for row in grouped:
                row["execution_status"] = "数据冲突"
                row["execution_error"] = "同一 Global 商品对应多组实际重量尺寸，本批次未更新该商品"
    dim_groups = {key: rows for key, rows in dim_groups.items() if key not in conflicts}
    if not dim_groups:
        with _lock:
            task["execute_status"] = "completed"
            task["execute_message"] = "没有可执行的商品行"
            task["execute_total"] = 0
        return

    clients: dict[int, MercadoLibreClient] = {}
    errors: dict[int, str] = {}
    for token_id, _ in set(dim_groups) | set(net_groups):
        if token_id not in clients and token_id not in errors:
            try:
                token = bit_mysql.get_mercado_store_token(token_id)
                clients[token_id], _ = _client_and_token(dict(token or {}))
            except Exception as exc:
                errors[token_id] = str(exc)

    executed = 0
    total_operations = len(dim_groups) + len(net_groups)
    with _lock:
        task["execute_total"] = total_operations
    for (token_id, global_id), grouped in dim_groups.items():
        if token_id in errors:
            for row in grouped:
                row["execution_status"] = "失败"
                row["execution_error"] = "店铺授权不可用：" + errors[token_id]
            continue
        try:
            row = grouped[0]
            _package_attributes(
                clients[token_id], global_id, row["actual_weight_g"], row["actual_dimensions_cm"]
            )
            for item in grouped:
                item["_package_status"] = "成功"
                item["execution_status"] = "重量尺寸已更新，等待更新净收益"
        except Exception as exc:
            for item in grouped:
                item["_package_status"] = "失败"
                item["execution_status"] = "失败"
                item["execution_error"] = f"重量尺寸：{str(exc)[:250]}"
        executed += 1
        with _lock:
            task["execute_processed"] = executed
            task["execute_message"] = f"重量尺寸更新 {executed}/{len(dim_groups)}"

    # All package attributes are submitted before the second pass changes net proceeds.
    for (token_id, marketplace_id), grouped in net_groups.items():
        eligible = [row for row in grouped if row.get("_package_status") == "成功"]
        if not eligible:
            continue
        if token_id in errors:
            for row in eligible:
                row["_net_status"] = "失败"
                row["execution_status"] = "部分完成"
                row["execution_error"] = "重量尺寸已提交，但店铺授权不可用：" + errors[token_id]
            continue
        target_values = {str(row["_net_proceeds_usd"]) for row in eligible}
        if len(target_values) != 1:
            for row in eligible:
                row["_net_status"] = "冲突"
                row["execution_status"] = "部分完成"
                row["execution_error"] = "同一站点商品对应多个净收益目标，未提交净收益"
            continue
        try:
            target = Decimal(next(iter(target_values)))
            clients[token_id].update_global_item(marketplace_id, {"net_proceeds": float(target)})
            for row in eligible:
                row["_net_status"] = "成功"
                row["execution_status"] = "完成"
        except Exception as exc:
            for row in eligible:
                row["_net_status"] = "失败"
                row["execution_status"] = "部分完成"
                row["execution_error"] = f"重量尺寸已提交，净收益失败：{str(exc)[:250]}"
        executed += 1
        with _lock:
            task["execute_processed"] = executed
            task["execute_message"] = f"尺寸与净收益更新 {executed}/{total_operations}"
    with _lock:
        task["execute_status"] = "completed"
        completed_count = sum(row.get("execution_status") == "完成" for row in records)
        task["execute_message"] = f"执行结束，完成 {completed_count} 条订单记录"


def start_execute(task_id: str, owner: str) -> dict[str, Any]:
    task = _get_owned_task(task_id, owner)
    with _lock:
        if task.get("status") != "ready":
            raise ValueError("订单查询完成后才能执行更新")
        if task.get("execute_status") == "running":
            raise ValueError("当前任务正在执行")
        if task.get("execute_status") == "completed":
            raise ValueError("本批次已执行，请重新上传文件后再执行新批次")
        ready = sum(bool(row.get("can_execute")) for row in task["records"])
        if not ready:
            raise ValueError("没有同时具备实测值、产品编号和当前净收益的可执行记录")
        task["execute_status"] = "queued"
        task["execute_message"] = f"已排队，准备更新 {ready} 条商品记录"
    thread = threading.Thread(target=_run_execute, args=(task_id, owner),
                              name=f"weight-dimensions-update-{task_id[:8]}", daemon=True)
    thread.start()
    return {"status": "queued", "ready_count": ready}
