"""Image-first workflow: block unmatched products; retain weight when missing."""
import time

from .browser import CircuitOpen, NoExactMatch, SearchTimeout, Stopped
from .models import number, erp_value_equal
from .pricing import usd_cost


def process(service, task, browser, models, config):
    key = task["erp_goods_id"]
    store = service.store
    try:
        service.progress(key, f"商品 {key}：主图搜货，比对前{config['max_candidates']}张图片，置信度须高于{config['match_threshold']:.0%}")
        candidates = browser.search_images(task)
        approved, evidence = models.match_images(task, candidates)
        store.update(key, image_match_evidence=evidence, candidate_count=len(candidates),
                     image_match_confidence=max((item["review"]["confidence"] for item in evidence), default=None))
        for item in evidence:
            review = item["review"]
            store.log(f"候选图 {review['index']}：置信度 {review['confidence']:.2%}；{review.get('reason', '')}", key)
        if not approved:
            raise NoExactMatch(f"前{len(candidates)}张候选图没有置信度高于{config['match_threshold']:.0%}的同款")
        match, sku_evidence = None, []
        for candidate in approved:
            service.record_visual(key, "image_matched", f"图片识别成功，置信度 {candidate['image_confidence']:.2%}；读取详情中的目标SKU售价和重量", page_url=candidate["url"])
            detail = browser.read_offer(task, candidate)
            match, reviews = models.match(task, detail)
            sku_evidence.append({"candidate": detail, "reviews": reviews})
            store.update(key, match_evidence=sku_evidence)
            if match:
                break
        if not match:
            task = store.update(key, image_match_confidence=max(c["image_confidence"] for c in approved))
            return save_result(service, task, browser, config, "risk", "图片已匹配，但无法确认目标SKU及其最终售价", {"review_status": "风险"})
        sku = match["selected_sku"]
        task = store.update(key, stage="matched", supplier_url=match["url"],
                            supplier_sku_id=sku["id"], supplier_sku=sku["label"],
                            merchant_id=match.get("merchant_id"), match_confidence=match["confidence"],
                            cost_price=sku.get("price"), supplier_price_evidence=sku,
                            image_match_confidence=candidate["image_confidence"], weight_g=None,
                            net_income_usd=None, pricing=None, page_info_checked=False)
        text = service.page_text(match)
        info = models.supplier_info(task, text) if text.strip() else {}
        cost = sku.get("price") or info.get("cost_price")
        weight = info.get("weight_g")
        if weight and number(weight) > 1000000:
            weight = None
        task = store.update(key, cost_price=cost, weight_g=weight, page_info=info,
                            page_info_checked=True, supplier_page_text=text,
                            info_sources={"cost_price": "1688目标SKU最终单价（含变体加价）" if cost else "未读取",
                                          "weight_g": "1688页面" if weight else "未读取，保留ERP原重量"})
        if not cost:
            return save_result(service, task, browser, config, "risk", "已匹配，但页面未提供可确认的目标SKU最终成本，保留原价格和重量", {"review_status": "风险"})
        pricing = usd_cost(cost, service.exchange_rate(config))
        task = store.update(key, net_income_usd=pricing["net_income_usd"], pricing=pricing)
        store.log(f"含变体加价的成本 ¥{cost} ÷ 美元汇率 {pricing['cny_per_usd']}，向上取整为 ${pricing['net_income_usd']}；汇率日期 {pricing['rate_date']}", key)
        changes = {"net_income_usd": task["net_income_usd"]}
        if weight:
            changes["weight_g"] = str(number(weight))
            return save_result(service, task, browser, config, "success", "匹配成功，已回填重量和净收益", changes)
        changes["review_status"] = "风险"
        return save_result(service, task, browser, config, "risk", "匹配成功但未读取到重量：保留原重量，只回填净收益并标记风险", changes)
    except SearchTimeout as exc:
        store.skip(key, str(exc) + "；未获得搜索结果，保留智赢原状态及数值")
        service.record_visual(key, "search_timeout", "搜索超时，已记录并继续下一件；智赢原数据保留")
    except NoExactMatch as exc:
        save_result(service, store.get(key), browser, config, "blocked", "匹配失败：" + str(exc), {"review_status": "屏蔽"})
    except (CircuitOpen, Stopped):
        raise
    except Exception as exc:
        store.exception(key, "主图核重核价执行异常", exc)
    finally:
        if hasattr(browser, "release_search"):
            browser.release_search(task)


def save_result(service, task, browser, config, result, reason, changes):
    """Only requested fields are changed; every other form value is retained."""
    key, store = task["erp_goods_id"], service.store
    task = store.update(key, decision_status=result, decision_reason=reason, planned_changes=changes)
    if not config["writeback_enabled"]:
        store.exception(key, "尚未启用ERP回写，未同步智赢状态或价格", reason)
        return
    attempt = None
    try:
        def before_save(old):
            nonlocal attempt
            attempt = {"before": old, "intent": {**changes, "at": time.time()}, "after": None, "verified": False}
            history = [*(store.get(key).get("write_history") or []), attempt]
            store.update(key, stage="writing", erp_before=old, erp_after=None, write_verified=False,
                         write_intent=attempt["intent"], write_history=history)
            store.log(f"回填前：重量 {old.get('weight_g')}g，净收益 ${old.get('net_income_usd')}，状态 {old.get('review_status')}；计划修改：{changes}", key)
        actual = browser.write_patch(task, changes, before_save)
        if attempt is None or not isinstance(actual, dict):
            raise ValueError("ERP未提供修改前后回读记录")
        for field in ("weight_g", "net_income_usd", "review_status"):
            expected = changes.get(field, attempt["before"].get(field))
            if not erp_value_equal(field, actual.get(field), expected):
                raise ValueError(f"保存回读不一致：{field}（包括应保留的原值）")
        history = store.get(key)["write_history"]
        history[-1].update(after=actual, verified=True, saved_at=time.time())
        store.update(key, status=result, stage="done", saved_at=time.time(), erp_after=actual,
                     write_verified=True, write_history=history, exception_reason="", exception_detail="")
        store.log(f"保存成功并回读确认：重量 {actual.get('weight_g')}g，净收益 ${actual.get('net_income_usd')}，状态 {actual.get('review_status')}；{reason}；继续下一件", key)
        service.record_visual(key, result, reason + "；已同步智赢并记录，继续下一件")
    except (CircuitOpen, Stopped):
        if attempt is not None:
            store.exception(key, "ERP保存过程中断，请核对已保存结果")
        raise
    except Exception as exc:
        actual = getattr(exc, "actual", None)
        history = store.get(key).get("write_history") or []
        if attempt is not None and history:
            history[-1].update(after=actual, error=str(exc))
            store.update(key, erp_after=actual, write_verified=False, write_history=history)
        store.exception(key, "ERP回写或状态同步失败", exc)
