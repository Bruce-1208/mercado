"""Image-first workflow: block unmatched products; retain weight when missing."""
import time

from .browser import CircuitOpen, NoExactMatch, SearchTimeout, Stopped, WritebackMismatch
from .models import number, erp_value_equal, parse_weight_evidence
from .pricing import protect_net_income, usd_cost


def process(service, task, browser, models, config):
    key = task["erp_goods_id"]
    store = service.store
    try:
        service.progress(key, f"商品 {key}：主图搜货，比对前{config['max_candidates']}张图片，同款匹配分数须大于等于{config['match_threshold']:.0%}")
        cached = cached_candidate(task, config)
        # A historical task can be reprocessed after a previous successful or
        # partial run.  Do not let the old score/link/pricing leak into the new
        # run when 1688 returns a different result set.  Keep ERP read-back and
        # write history for audit; only reset the current supplier conclusion.
        reset = {
            "image_match_evidence": [], "match_evidence": [], "candidate_count": 0,
            "image_match_confidence": None, "supplier_url": None,
            "supplier_sku_id": None, "supplier_sku": "", "merchant_id": None,
            "match_confidence": None, "supplier_price_evidence": {},
            "cost_price": None, "weight_g": None, "net_income_usd": None,
            "pricing": None, "page_info": {}, "page_info_checked": False,
            "supplier_page_text": "", "info_sources": {},
            "decision_status": "", "decision_reason": "", "skip_reason": "",
        }
        if not cached:
            reset.update(best_match_url="", best_match_title="", best_match_image_url="",
                         best_match_confidence=None, best_match_approved=False)
        store.update(key, **reset)
        task = store.get(key)
        if cached:
            # A manual ERP SKU correction after an ambiguous result can reuse
            # the already-approved image lead. Reopen its detail directly;
            # image search and the first vision call are unnecessary work.
            candidates = [cached]
            approved = [cached]
            evidence = [{"candidate": cached, "review": {
                "index": 1, "same_product": True,
                "confidence": cached["image_confidence"],
                "reason": "复用上次已通过图片匹配的最佳候选",
                "cached_candidate": True,
            }}]
            service.progress(key, f"商品 {key}：复用上次最佳匹配，直接核对1688变体")
        else:
            candidates = browser.search_images(task)
            approved, evidence = models.match_images(task, candidates)
        # Keep the strongest score even when the model rejects the candidate as
        # a non-match.  The UI should show why a product was rejected instead
        # of displaying a blank score for every below-threshold result.
        image_scores = [item["review"]["confidence"] for item in evidence]
        store.update(key, image_match_evidence=evidence, match_evidence=[],
                     candidate_count=len(candidates),
                     image_match_confidence=max(image_scores, default=None))
        for item in evidence:
            review = item["review"]
            verdict = "同款" if review.get("same_product") is True else "非同款"
            store.log(f"候选图 {review['index']}：{verdict}，匹配分数 {review['confidence']:.2%}；{review.get('reason', '')}", key)
        # Preserve the top detail URL even when every image score is below the
        # approval threshold, so the operator can inspect the best lead from
        # the task list and retry after supplementing the ERP SKU.
        ranked = sorted(
            (item for item in evidence if item.get("candidate", {}).get("url")),
            key=lambda item: item["review"].get("confidence", 0), reverse=True,
        )
        if ranked:
            lead = ranked[0]["candidate"]
            lead_review = ranked[0]["review"]
            lead_approved = (lead_review.get("same_product") is True
                             and lead_review.get("confidence", 0) >= config["match_threshold"])
            store.update(key, best_match_url=lead.get("url", ""),
                         best_match_title=lead.get("title", ""),
                         best_match_image_url=lead.get("main_image_url", ""),
                         best_match_confidence=lead_review.get("confidence"),
                         best_match_approved=lead_approved)
        if not approved:
            raise NoExactMatch(f"前{len(candidates)}张候选图没有匹配分数大于等于{config['match_threshold']:.0%}的同款")
        # The user-selected pricing policy deliberately does not identify an
        # ERP target variant. Use the strongest image-approved offer and price
        # conservatively from the most expensive verified SKU in that offer.
        candidate = approved[0]
        store.log(
            f"同款图片分数 {candidate['image_confidence']:.2%} 已达到门槛 "
            f"{config['match_threshold']:.2%}；正在读取该1688链接的最高变体价",
            key,
        )
        service.record_visual(
            key, "image_matched", "图片同款已确认；不匹配具体变体，读取当前1688链接的最高最终单价",
            page_url=candidate["url"],
        )
        detail = browser.read_offer(task, candidate)
        detail_url = detail.get("url") or candidate.get("url", "")
        # read_offer navigates to the real offer page even when the image-search
        # card itself only has an air.1688.com shell URL. Persist that resolved
        # detail URL before any later pricing/weight branch can return a risk.
        task = store.update(
            key,
            best_match_url=detail_url,
            best_match_title=detail.get("title", candidate.get("title", "")),
            best_match_image_url=detail.get("main_image_url", candidate.get("main_image_url", "")),
            best_match_confidence=candidate.get("image_confidence"),
            best_match_approved=True,
            supplier_url=detail_url or None,
        )
        sku = highest_priced_variant(detail.get("skus") or [])
        if sku is None:
            return save_result(service, store.get(key), browser, config, "risk",
                               "图片匹配成功，但1688详情的变体价格不完整，无法确认最高最终单价；未执行回写",
                               {})
        sku_count = len(detail["skus"])
        # On the current 1688 build packaging weight is often rendered inside
        # the SKU label (for example "火烈鸟冲浪板117*54cm(0.29kg / 看产品介绍")
        # rather than in productPackInfo. Keep the selected highest-price
        # row's explicit label evidence as a defensive fallback.
        sku = {**sku, "raw_weight": sku.get("raw_weight") or sku.get("label", "")}
        price_evidence = {**sku, "pricing_policy": "highest_variant_price",
                          "sku_count": sku_count, "variant_ambiguous": sku_count > 1}
        store.update(key, match_evidence=[{
            "candidate": detail,
            "reviews": [],
            "image_confidence": candidate["image_confidence"],
            "pricing_policy": "highest_variant_price",
            "selected_price": sku["price"],
        }])
        store.log(f"1688详情共 {sku_count} 个变体；最高最终单价为 ¥{sku['price']}（{sku['label']}）", key)
        task = store.update(key, stage="matched", supplier_url=detail_url,
                            supplier_sku_id=sku["id"], supplier_sku=sku["label"],
                            merchant_id=detail.get("merchant_id"), match_confidence=candidate["image_confidence"],
                            cost_price=sku.get("price"), supplier_price_evidence=price_evidence,
                            supplier_sku_ambiguous=sku_count > 1,
                            image_match_confidence=candidate["image_confidence"], weight_g=None,
                            net_income_usd=None, pricing=None, page_info_checked=False)
        # The official SKU module already provides a final price and often an
        # explicit package weight. Avoid sending those same facts through a
        # second model call; extract from the row locally and ask the model
        # only when one of the fields is genuinely missing.
        cost = sku.get("price")
        weight = parse_weight_evidence(sku.get("raw_weight", ""))
        info = {}
        text = service.page_text({**detail, "selected_sku": sku})
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
            return save_result(service, task, browser, config, "success",
                               f"图片匹配成功；未匹配具体变体，已按{sku_count}个变体中的最高价回填净收益，并从最高价变体的页面证据回填重量",
                               changes)
        return save_result(service, task, browser, config, "risk",
                           f"图片匹配成功；未匹配具体变体，已按{sku_count}个变体中的最高价回填净收益；最高价变体未提供可解析重量，保留原重量和状态",
                           changes)
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


def cached_candidate(task, config):
    """Build a reusable candidate from a previously approved image lead."""
    url = task.get("best_match_url")
    try:
        confidence = float(task.get("best_match_confidence"))
    except (TypeError, ValueError):
        return None
    approved = task.get("best_match_approved") is True
    if not approved:
        for item in task.get("image_match_evidence") or []:
            candidate = item.get("candidate") or {}
            review = item.get("review") or {}
            if (candidate.get("url") == url and review.get("same_product") is True
                    and review.get("confidence", 0) >= config["match_threshold"]):
                approved = True
                break
    if not url or confidence < config["match_threshold"] or not task.get("erp_sku") or not approved:
        return None
    return {"url": url, "title": task.get("best_match_title", ""),
            "main_image_url": task.get("best_match_image_url", ""),
            "image_confidence": confidence}


def highest_priced_variant(skus):
    """Return the most expensive SKU only when every final price is valid."""
    if not skus:
        return None
    try:
        priced = [(number(sku.get("price")), sku) for sku in skus]
    except ValueError:
        return None
    _, selected = max(priced, key=lambda item: item[0])
    return selected


def save_result(service, task, browser, config, result, reason, changes):
    """Only requested fields are changed; every other form value is retained."""
    key, store = task["erp_goods_id"], service.store
    changes = dict(changes)
    task = store.update(key, decision_status=result, decision_reason=reason, planned_changes=changes)
    if not changes:
        store.update(key, status=result, stage="done", saved_at=time.time(),
                     write_verified=False, write_intent={}, exception_reason="", exception_detail="")
        store.log(f"无需回写重量或净收益，智赢商品状态及原数值保持不变；{reason}；继续下一件", key)
        service.record_visual(key, result, reason + "；未修改智赢商品状态，继续下一件")
        return
    if not config["writeback_enabled"]:
        # A dry run still has a business result. Keep it reviewable as a risk
        # because the calculated values were not synchronized to ERP; do not
        # turn an intentional no-write test into an exception that the batch
        # silently auto-skips.
        dry_reason = reason + "；测试模式未启用ERP回写，仅保留本地核重核价结论"
        store.update(key, status="risk", stage="done", saved_at=time.time(),
                     write_verified=False, write_intent={}, exception_reason="",
                     exception_detail="", decision_status="risk", decision_reason=dry_reason)
        store.log(dry_reason + "；智赢商品状态及原数值保持不变，继续下一件", key)
        service.record_visual(key, "risk", dry_reason + "；未修改智赢商品")
        return
    attempt = None
    try:
        def before_save(old):
            nonlocal attempt, reason, task
            if "net_income_usd" in changes:
                protected = protect_net_income(old.get("net_income_usd"), task.get("pricing") or {})
                if protected.get("net_income_retained_original"):
                    changes.pop("net_income_usd")
                    task = store.update(key, net_income_usd=protected["net_income_writeback_usd"],
                                        pricing=protected, planned_changes=changes,
                                        decision_reason=(reason + "；" + protected["net_income_adjustment"]))
                    reason = task["decision_reason"]
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
        detail = str(exc)
        if isinstance(exc, WritebackMismatch):
            detail += "；外部写入结果不确定，请人工回读确认后再继续"
        store.exception(key, "ERP重量或净收益回写失败", detail)
