"""Pricing suggestions and human acknowledgement. No platform price mutations."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import time

from erp.price_competition_store import PriceCompetitionStore
from mercado_api.price_competition import PriceCompetitionClient


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def money(value):
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if value is not None else None


def validate_costs(payload):
    currency = str(payload.get("currency_id") or "").strip().upper()
    if not currency or len(currency) != 3 or not currency.isalpha():
        raise ValueError("请填写商品本地币种")
    costs = {"currency_id": currency}
    for key in ("fixed_cost", "variable_cost_percent", "minimum_profit"):
        value = number(payload.get(key))
        if value is None or value < 0 or value > Decimal("1e12") or (key == "variable_cost_percent" and value >= 100):
            raise ValueError("请填写完整的非负成本和最低利润；按售价计费比例须小于 100%")
        costs[key] = str(value)
    return costs


def normalize(observation):
    item, signal = observation["item"], observation.get("signal")
    return {"kind": observation["kind"], "item_id": item["id"], "current_price": item.get("price"),
            "currency_id": item.get("currency_id"), "item_status": item.get("status"),
            "signal": signal}


def suggestion(snapshot, costs=None):
    result = {"kind": (snapshot or {}).get("kind", "unknown"), "current_price": None,
              "currency_id": (snapshot or {}).get("currency_id"), "official_reference_price": None,
              "competition_target_price": None, "minimum_acceptable_price": None,
              "estimated_profit": None, "suggested_price": None, "ready": False,
              "reason": "尚未读取官方数据", "competition_status": "", "boosts": [], "reasons": []}
    if not snapshot:
        return result
    current = number(snapshot.get("current_price"))
    result["current_price"] = money(current)
    signal = snapshot.get("signal") or {}
    target, reason = None, ""
    kind = snapshot["kind"]
    result["competition_status"] = str(signal.get("status") or "")
    if kind == "ordinary":
        reference = number((signal.get("suggested_price") or {}).get("amount"))
        result["official_reference_price"] = money(reference) if reference and reference > 0 else None
        if not signal:
            reason = "官方参考价缺失"
        elif signal.get("applicable_suggestion") is not True:
            reason = "官方价格建议不适用"
        elif reference is None or reference <= 0:
            reason = "官方参考价缺失"
        elif current is not None and reference >= current:
            reason = "当前售价已不高于官方参考价"
        else:
            target = reference
        reported_current = number((signal.get("current_price") or {}).get("amount"))
    elif kind == "catalog":
        target = number(signal.get("price_to_win"))
        result["competition_target_price"] = money(target) if target and target > 0 else None
        result["boosts"] = signal.get("boosts") or []
        result["reasons"] = signal.get("reason") or []
        status = signal.get("status")
        if status in {"winning", "sharing_first_place"}:
            reason = "已获胜或共享首位，建议保持价格"
        elif status == "listed":
            reason = "尚未参与 Catalog 竞争，请先改善竞争条件"
        elif status not in {"competing", "losing"}:
            reason = "Catalog 竞争状态未知"
        elif target is None or target <= 0:
            reason = "平台未提供竞争目标价"
        elif current is not None and target >= current:
            reason = "目标价无需降价，请查看竞争条件"
        reported_current = number(signal.get("current_price"))
    else:
        reason = "官方未提供商品 Catalog 类型，无法选择价格数据源"
        reported_current = None
    if not snapshot.get("currency_id"):
        reason = "官方售价币种缺失"
    elif signal and signal.get("currency_id") != snapshot.get("currency_id"):
        reason = "官方价格信号与售价币种不一致"
    elif current is None or current <= 0:
        reason = "当前售价无效"
    elif reported_current is not None and reported_current != current:
        reason = "官方价格信号与当前售价不同步，请稍后刷新"
    if snapshot.get("item_status") != "active":
        reason = "商品当前不在售"
    if target is not None and target > 0:
        # A competition price must not exceed the official target. Recheck the floor below.
        target = target.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        result["suggested_price"] = float(target)
        if target <= 0 and not reason:
            reason = "目标价精度不足，无法生成有效建议价"
        elif current is not None and target >= current and not reason:
            reason = "目标价无需降价"
    if costs and costs.get("currency_id") == snapshot.get("currency_id"):
        fixed = number(costs.get("fixed_cost"))
        rate = number(costs.get("variable_cost_percent"))
        minimum = number(costs.get("minimum_profit"))
        if fixed is not None and rate is not None and minimum is not None and fixed >= 0 and 0 <= rate < 100 and minimum >= 0:
            retained = 1 - rate / 100
            floor = ((fixed + minimum) / retained).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
            result["minimum_acceptable_price"] = float(floor)
            if target is not None and target > 0:
                profit = target * retained - fixed
                result["estimated_profit"] = money(profit)
                if not reason and (target < floor or profit < minimum):
                    reason = "建议价低于最低可接受价，请保留利润底线"
        elif not reason:
            reason = "利润资料无效，请重新核实"
    elif not reason:
        reason = "请核实同币种成本与最低利润" if not costs else "成本币种与商品币种不一致，请重新核实"
    result["ready"] = not bool(reason)
    result["reason"] = reason or "可人工确认价格建议"
    return result


def _link(link_id, allowed_token_ids):
    from erp.mercadolibre_store_link_store import get_store_links_by_ids
    if type(link_id) is not int or link_id <= 0:
        raise ValueError("商品链接编号无效")
    row = get_store_links_by_ids([link_id])[0]
    if allowed_token_ids is not None and int(row["token_id"]) not in allowed_token_ids:
        raise PermissionError("当前账号不能访问该店铺")
    if not row.get("is_current") or row.get("status") == "deleted":
        raise ValueError("商品链接已失效，请重新同步店铺链接")
    return row


def _inspect(row):
    from bit import bit_mysql
    from bit.bit_store_link_sync import _client_and_token
    token = bit_mysql.get_mercado_store_token(int(row["token_id"]))
    if not token:
        raise ValueError("店铺授权已失效或已关闭")
    client, token = _client_and_token(token)
    adapter = PriceCompetitionClient(client, caller_id=token.get("meli_user_id"), client_id=token.get("client_id"))
    return normalize(adapter.inspect(row["item_id"]))


def _decorate(row, store):
    observed = store.get(row["token_id"], row["item_id"])
    result = {**row, "pricing": suggestion(observed.get("snapshot"), observed.get("costs")),
              "costs": observed.get("costs"), "checked_at": observed.get("checked_at"),
              "error": observed.get("error", ""),
              "approval": store.latest_approval(row["token_id"], row["item_id"])}
    if observed.get("error"):
        result["pricing"].update(ready=False, reason=observed["error"])
    if observed.get("checked_at") and time.time() - observed["checked_at"] > 300:
        result["pricing"].update(ready=False, reason="官方数据已超过 5 分钟，请重新检测")
    if result["approval"]:
        approval = result["approval"]
        approved_snapshot, approved_costs = approval.pop("snapshot"), approval.pop("costs")
        approval["matches_current"] = (approved_snapshot == observed.get("snapshot")
                                        and approved_costs == observed.get("costs") and not observed.get("error"))
    return result


def dispatch(action, payload, *, allowed_token_ids=None, actor="", store=None):
    store = store or PriceCompetitionStore()
    if action == "list":
        from bit import bit_db_api
        token_id = payload.get("token_id")
        if token_id not in (None, ""):
            token_id = int(token_id)
            if token_id <= 0:
                raise ValueError("店铺编号无效")
            if allowed_token_ids is not None and token_id not in allowed_token_ids:
                raise PermissionError("当前账号不能访问该店铺")
        else:
            token_id = None
        page = int(payload.get("page") or 1)
        if not 1 <= page <= 1000000:
            raise ValueError("分页参数无效")
        data = bit_db_api.list_mercado_store_links(token_ids=allowed_token_ids, token_id=token_id,
            site_id=str(payload.get("site_id") or ""), search=str(payload.get("search") or ""),
            include_categories=False, page=page, page_size=20, sort_by="last_synced_at")
        return {"rows": [_decorate(row, store) for row in data.get("rows") or []],
                **{key: data.get(key) for key in ("total", "page", "pages")}}
    if action == "stores":
        from bit.mercado_promotions import visible_store_options
        return visible_store_options(allowed_token_ids)
    if action == "confirm":
        if payload.get("confirmed") is not True:
            raise ValueError("请核对价格、成本与利润后再确认")
        preview = store.get_preview(str(payload.get("preview_id") or ""))
        if allowed_token_ids is not None and preview["token_id"] not in allowed_token_ids:
            raise PermissionError("当前账号不能访问该店铺")
        if preview["actor"] != actor:
            raise PermissionError("只能确认本人生成的预览")
        if preview["confirmed_at"] or preview["expires"] <= time.time():
            raise ValueError("预览已过期或已确认，请重新预览")
        row = _link(preview["payload"]["link_id"], allowed_token_ids)
        if (row["token_id"], row["item_id"]) != (preview["token_id"], preview["item_id"]):
            raise ValueError("商品链接已变化，请重新预览")
        fresh = _inspect(row)
        observed = store.get(row["token_id"], row["item_id"])
        if fresh != preview["payload"]["snapshot"] or observed.get("costs") != preview["payload"]["costs"]:
            store.observe(row["token_id"], row["item_id"], fresh)
            raise ValueError("官方价格、竞争条件或成本已变化，请重新预览")
        proposed = suggestion(fresh, observed.get("costs"))
        if not proposed["ready"]:
            raise ValueError(proposed["reason"])
        store.observe(row["token_id"], row["item_id"], fresh)
        store.confirm(preview["id"], token_id=row["token_id"], item_id=row["item_id"],
                      snapshot=fresh, costs=observed["costs"])
        return {"status": "confirmed", "platform_price_changed": False,
                "message": "建议已确认，待人工改价；平台价格未修改", "row": _decorate(row, store)}
    row = _link(payload.get("link_id"), allowed_token_ids)
    if action == "costs":
        costs = validate_costs(payload)
        store.save_costs(row["token_id"], row["item_id"], costs)
        return _decorate(row, store)
    if action not in {"refresh", "preview"}:
        raise ValueError("不支持的价格竞争操作")
    try:
        snapshot = _inspect(row)
    except Exception:
        store.observe(row["token_id"], row["item_id"], error="官方价格读取失败，请重试或核实店铺授权")
        if action == "preview":
            raise ValueError("官方价格读取失败，不能生成确认预览")
        return _decorate(row, store)
    store.observe(row["token_id"], row["item_id"], snapshot)
    decorated = _decorate(row, store)
    if action == "refresh":
        return decorated
    proposed = decorated["pricing"]
    if not proposed["ready"]:
        raise ValueError(proposed["reason"])
    return store.preview(row["token_id"], row["item_id"], actor,
        {"link_id": row["id"], "snapshot": snapshot, "costs": decorated["costs"], "suggestion": proposed})
