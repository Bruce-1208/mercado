"""Official freight adjustments caused by incorrect package measurements."""
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation


def amount(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def dimension_adjustment(payload):
    if not isinstance(payload, Mapping):
        return Decimal(0)
    return sum((amount(entry.get("amount")) or Decimal(0)
                for sender in payload.get("senders") or []
                for entry in sender.get("compensations") or []
                if entry.get("reason") == "incorrect_dimensions"), Decimal(0))


def apply_dimension_costs(record, payload):
    """Use official checkout costs plus only measurement-related adjustments."""
    delta = dimension_adjustment(payload)
    record["_dimension_compensation_confirmed"] = bool(delta)
    if not delta:
        return
    currency = str(payload.get("currency_id") or "").upper()
    record["_shipment_costs"] = payload
    costs = [amount(sender.get("cost")) for sender in payload.get("senders") or []]
    base = sum(costs, Decimal(0)) if costs and all(cost is not None for cost in costs) else None
    record["declared_freight"] = f"{base.normalize():f} {currency}".strip() if base is not None else ""
    record["actual_freight"] = f"{(base + delta).normalize():f} {currency}".strip() if base is not None else ""
    record["freight_difference"] = f"{delta.normalize():f} {currency}".strip()
    record["freight_difference_usd"] = str(delta) if currency == "USD" else ""


def dimension_adjustment_sql(payload):
    # Match reason and amount on the same entry, including negative refunds.
    return f"""COALESCE((SELECT SUM(adjustment.amount) FROM JSON_TABLE(
        CASE WHEN JSON_VALID({payload}) THEN {payload} ELSE '{{}}' END,
        '$.senders[*].compensations[*]'
        COLUMNS(reason VARCHAR(80) PATH '$.reason',
                amount DECIMAL(20,4) PATH '$.amount' NULL ON ERROR)
    ) AS adjustment WHERE adjustment.reason = 'incorrect_dimensions'), 0)"""


def saved_cost_payload_sql():
    return """(SELECT costs.payload_json FROM mercado_shipment_costs AS costs
        WHERE costs.shipping_id = JSON_UNQUOTE(JSON_EXTRACT(zying_weight_dimensions_records.record_json, '$.shipment_id'))
        AND costs.token_id = zying_weight_dimensions_records.wdr_store_token_id)"""


def saved_dimension_adjustment_sql():
    return f"""COALESCE((SELECT {dimension_adjustment_sql('costs.payload_json')}
        FROM mercado_shipment_costs AS costs
        WHERE costs.shipping_id = JSON_UNQUOTE(JSON_EXTRACT(zying_weight_dimensions_records.record_json, '$.shipment_id'))
        AND costs.token_id = zying_weight_dimensions_records.wdr_store_token_id), 0)"""
