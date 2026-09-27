"""Repair persisted partial measurements without reloading orders or product details.

Run from the repository: python -m scripts.repair_weight_dimensions_measurements
Only legacy rows rejected by the old all-or-nothing validator are repaired.
The normal full-sync worker handles other missing or new records.
"""
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from bit import bit_mysql as db
from bit.weight_dimensions_records import _read_measurements
from bit.bit_store_link_sync import _client_and_token
from mercado_api.client import MercadoLibreClient


def main():
    rows = [row for row in db.list_weight_dimensions_records(filters={})
            if row.get("_measurement_version") != 2
            and any(message in str(row.get("query_error") or "") for message in (
                "非正数重量或尺寸", "缺少有效重量或尺寸", "没有提供克和厘米单位",
            ))]
    print(f"待修复记录：{len(rows)}", flush=True)
    templates = {}
    for token_id in {int(row.get('_token_id') or row.get('store_token_id') or 0) for row in rows}:
        if token_id:
            templates[token_id], _ = _client_and_token(db.get_mercado_store_token(token_id))
    local = threading.local()

    def repair(row):
        token_id = int(row.get('_token_id') or row.get('store_token_id') or 0)
        if not token_id or not row.get('shipment_id'):
            return None
        if not hasattr(local, 'clients'):
            local.clients = {}
        if token_id not in local.clients:
            template = templates[token_id]
            local.clients[token_id] = MercadoLibreClient(template.access_token, timeout=template.timeout)
        _read_measurements(row, local.clients[token_id], row['shipment_id'])
        row['query_status'] = '已读取' if any(row.get(key) for key in (
            'declared_weight_g', 'declared_dimensions_cm', 'actual_weight_g', 'actual_dimensions_cm',
        )) else '查询失败'
        return row

    saved = 0
    with ThreadPoolExecutor(max_workers=12) as pool:
        batch = []
        for future in as_completed([pool.submit(repair, row) for row in rows]):
            row = future.result()
            if row:
                batch.append(row)
            if len(batch) >= 25:
                db.save_weight_dimensions_records(batch, refresh=True)
                saved += len(batch)
                batch = []
                print(f"已修复并保存：{saved}/{len(rows)}", flush=True)
        if batch:
            db.save_weight_dimensions_records(batch, refresh=True)
            saved += len(batch)
    print(f"修复完成：{saved}/{len(rows)}", flush=True)


if __name__ == '__main__':
    main()
