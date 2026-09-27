"""Server-side Mercado collection measurements and AI audit persistence."""
from decimal import Decimal, InvalidOperation
from erp import mercadolibre_collection_store as db

FIELDS = ("weight_g", "package_length_cm", "package_width_cm", "package_height_cm")


def positive(value):
    try:
        result = Decimal(str(value))
        return float(result) if result.is_finite() and result > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def measurements(detail, selected):
    # A selected SKU's measurements take precedence; never infer missing dimensions.
    result = {}
    for field in FIELDS:
        value = positive(selected.get(field)) or positive(detail.get(field))
        if value is not None:
            result[field] = value
    return result


def products(ids):
    ids = db._normalize_row_ids(ids, empty_message="请勾选需要AI核查的采集商品")
    if len(ids) > 100:
        raise ValueError("每批最多核查100件商品")
    connection = db._connect()
    try:
        with connection.cursor() as cursor:
            db.ensure_collection_tables(cursor)
            cursor.execute(f"SELECT * FROM `{db.COLLECTION_TABLE}` WHERE `id` IN ({','.join(['%s'] * len(ids))})", tuple(ids))
            rows = {int(row['id']): row for row in cursor.fetchall()}
        connection.commit()
        if len(rows) != len(ids):
            raise ValueError("部分采集商品已删除，请刷新列表")
        result = []
        for key in ids:
            row = rows[key]
            if not row.get('title') or not row.get('main_image_url'):
                raise ValueError(f"商品 {key} 缺少标题或主图")
            result.append({**db._json_safe_row(row), 'collection_item_id': key,
                           'erp_goods_id': f'collection:{key}', 'source_type': 'mercado_collection',
                           'collection_before': {field: db._json_safe_row(row).get(field) for field in FIELDS}})
        return result
    finally:
        connection.close()


def save_result(task, status, reason, changes=None, evidence=None):
    """Commit measurements, audit and product mirror together, with stale-input check."""
    item_id = int(task['collection_item_id'])
    changes = changes or {}
    if set(changes) - set(FIELDS) or any(positive(v) is None for v in changes.values()):
        raise ValueError('重量尺寸必须是有效正数')
    connection = db._connect()
    try:
        with connection.cursor() as cursor:
            db.ensure_collection_tables(cursor)
            cursor.execute(f"SELECT * FROM `{db.COLLECTION_TABLE}` WHERE `id`=%s FOR UPDATE", (item_id,))
            before = cursor.fetchone()
            if not before:
                raise ValueError('采集商品已删除，不能回写')
            audit = db._loads(before.get('ai_weight_price_json'), {})
            delta = {k: v for k, v in changes.items() if positive(before.get(k)) != positive(v)}
            if delta and any(positive(before.get(k)) != positive((task.get("collection_before") or task).get(k)) for k in changes):
                status, reason, delta = 'conflict', '核查期间重量尺寸已被修改，请重新核查', {}
            now = db._now()
            history = list(audit.get('history') or [])
            if delta:
                history.append({'at': now, 'task_id': task['erp_goods_id'],
                                'actor_id': task.get('owner_user_id'),
                                'before': {k: db._json_safe_row(before).get(k) for k in delta},
                                'after': delta, 'evidence': evidence or {}})
            audit.update(status=status, reason=reason, checked_at=now if status in {'checked', 'missing_evidence', 'unmatched', 'conflict'} else audit.get('checked_at'),
                         modified=bool(history), modified_at=now if delta else audit.get('modified_at'),
                         last_result='modified' if delta else 'unchanged', history=history)
            if evidence is not None:
                audit['evidence'] = evidence
            assignments, values = [], []
            if delta:
                assignments, values, _ = db._product_content_update_plan(delta)
                assignments = [s.replace("'manual_edit'", "'ai_1688_verified'") for s in assignments]
            assignments.append('`ai_weight_price_json`=%s')
            values.append(db._dumps(audit))
            cursor.execute(f"UPDATE `{db.COLLECTION_TABLE}` SET {', '.join(assignments)} WHERE `id`=%s", tuple(values + [item_id]))
            # Existing linked products keep the same audit and measurement values.
            cursor.execute(f"UPDATE `{db.PRODUCT_TABLE}` SET {', '.join(assignments)} WHERE `collection_item_id`=%s", tuple(values + [item_id]))
        connection.commit()
        return audit
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
