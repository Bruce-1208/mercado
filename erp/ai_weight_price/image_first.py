"""Image-first workflow: block unmatched products; retain weight when missing."""
import time

from .browser import CircuitOpen, NoExactMatch, SearchTimeout, Stopped
from .models import number, erp_value_equal
from .pricing import usd_cost


def process(service, task, browser, models, config):
    key = task["erp_goods_id"]
    store = service.store
    try:
        service.progress(key, f"商品 {key}：主图搜货，比对前{config['max_candidates']}张图片，同款匹配分数须大于等于{config['match_threshold']:.0%}")
        candidates = browser.search_images(task)
        approved, evidence = models.match_images(task, candidates)
        # Keep the strongest score even when the model rejects the candidate as
        # a non-match.  The UI should show why a product was rejected instead
        # of displaying a blank score for every below-threshold result.
        image_scores = [item["review"]["confidence"] for item in evidence]
        store.update(key, image_match_evidence=evidence, candidate_count=len(candidates),
                     image_match_confidence=max(image_scores, default=None))
        for item in evidence:
            review = item["review"]
            verdict = "同款" if review.get("same_product") is True else "非同款"
            store.log(f"候选图 {review['index']}：{verdict}，匹配分数 {review['confidence']:.2%}；{review.get('reason', '')}", key)
        if not approved:
            raise NoExactMatch(f"前{len(candidates)}张候选图没有匹配分数大于等于{config['match_threshold']:.0%}的同款")
        # approved is sorted by score descending; equal scores preserve the
        # original 1688 result order. Open exactly the highest-scoring match.
        candidate = approved[0]
        service.record_visual(key, "image_matched",
                              f"已从前{len(candidates)}张中选择同款分数最高的候选（{candidate['image_confidence']:.2%}）；进入1688明细读取变体价格和重量",
                              page_url=candidate["url"])
        detail = browser.read_offer(task, candidate)
        match, reviews = models.match(task, detail)
        sku_evidence = [{"candidate": detail, "reviews": reviews}]
        store.update(key, match_evidence=sku_evidence)
        if not match:
            task = store.update(key, image_match_confidence=max(c["image_confidence"] for c in approved))
            variants = len(detail.get("skus") or [])
            if variants > 1:
                reason = (f"图片匹配成功，但1688详情有{variants}个变体，ERP资料不足以唯一确认目标SKU；"
                          "未执行回写，保留智赢原状态")
            elif variants == 1:
                reason = "图片匹配成功，但SKU复核证据不足以确认目标规格；未执行回写，保留智赢原状态"
            else:
                reason = "图片匹配成功，但1688详情未提供可确认的目标SKU价格；未执行回写，保留智赢原状态"
            return save_result(service, task, browser, config, "risk", reason, {})
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
            return save_result(service, task, browser, config, "risk", "已匹配，但页面未提供可确认的目标SKU最终成本，保留原价格、重量及状态", {})
        pricing = usd_cost(cost, service.exchange_rate(config))
        task = store.update(key, net_income_usd=pricing["net_income_usd"], pricing=pricing)
        store.log(f"含变体加价的成本 ¥{cost} ÷ 美元汇率 {pricing['cny_per_usd']}，向上取整为 ${pricing['net_income_usd']}；汇率日期 {pricing['rate_date']}", key)
        changes = {"net_income_usd": task["net_income_usd"]}
        if weight:
            changes["weight_g"] = str(number(weight))
            return save_result(service, task, browser, config, "success", "匹配成功，已回填重量和净收益", changes)
        return save_result(service, task, browser, config, "risk", "匹配成功但未读取到重量：保留原重量和状态，只回填净收益", changes)
    except SearchTimeout as exc:
        # A timeout is a technical failure, not a completed matching
        # decision. Store it as an exception; the service converts non-human
        # item exceptions into an automatic skip before advancing.
        store.exception(key, "1688主图搜索超时", str(exc) + "；未获得搜索结果，保留智赢原状态及数值")
        service.record_visual(key, "search_timeout", "搜索超时，自动跳过当前商品；智赢原数据保留")
    except NoExactMatch as exc:
        save_result(service, store.get(key), browser, config, "blocked", "匹配失败：" + str(exc) + "；保留智赢原状态", {})
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
    if not changes:
        store.update(key, status=result, stage="done", saved_at=time.time(),
                     write_verified=False, write_intent={}, exception_reason="", exception_detail="")
        store.log(f"无需回写重量或净收益，智赢商品状态及原数值保持不变；{reason}；继续下一件", key)
        service.record_visual(key, result, reason + "；未修改智赢商品状态，继续下一件")
        return
    if not config["writeback_enabled"]:
        store.exception(key, "尚未启用ERP回写，未同步智赢重量或净收益", reason)
        return
    attempt = None
    try:
        def before_save(old):
            nonlocal attempt
            attempt = {"before": old, "intent": {**changes, "at": time.time()}, "after": None, "verified": False}
            history = [*(store.get(key).get("write_history") or []), attempt]
            store.update(key, stage="writing", erp_before=old, erp_after=None, write_verified=False,
                         write_intent=attempt["intent"], write_history=history)
            store.log(f"回填前：重量 {old.get('weight_g')}g，净收益 ${old.get('net_income_usd')}，智赢状态保持 {old.get('review_status')}；计划修改：{changes}", key)
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
        store.exception(key, "ERP重量或净收益回写失败", exc)
