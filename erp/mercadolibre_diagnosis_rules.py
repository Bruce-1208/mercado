"""Evidence-based product diagnosis. No generated platform scores or deadlines."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

RISK_ORDER = {"high": 0, "medium": 1, "low": 2, "pending": 3, "healthy": 4}
RULE_LABELS = {
    "HAS_GTIN": ("商品编码待完善", "核对并补充真实 GTIN 商品编码。"),
    "PICTURES_QUANTITY_MIN": ("商品图片数量不足", "按平台要求补充不同角度的商品图片。"),
    "PICTURES_QUALITY": ("商品图片质量待改善", "检查主图清晰度、背景和商品展示。"),
    "TITLE": ("商品标题待改善", "按平台提示核对标题与商品实际信息。"),
    "FREE_SHIPPING": ("配送条件有改进机会", "核对免运费条件及成本，再决定是否调整。"),
}


def score(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) and 0 <= result <= 100 else None


def plain(value):
    if isinstance(value, dict):
        text = str(value.get("text") or "")
        for index, replacement in enumerate(value.get("placeholders") or []):
            text = text.replace("{" + str(index) + "}", str(replacement))
    else:
        text = str(value or "")
    return re.sub(r"<[^>]*>", "", text).strip()[:4000]


def safe_link(value):
    value = str(value or "").strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    return value if parsed.scheme == "https" and parsed.hostname and not parsed.username else ""


def pending_quality_rules(payload):
    """Rules may live under nested buckets/variables in platform responses."""
    seen = set()

    def walk(node):
        if isinstance(node, dict):
            for rule in node.get("rules") or []:
                if not isinstance(rule, dict) or rule.get("status") != "PENDING":
                    continue
                key = str(rule.get("key") or "UNKNOWN")
                words = rule.get("wordings") or {}
                evidence = plain(words.get("title") or words.get("label") or node.get("title"))
                identity = (key, evidence)
                if identity in seen:
                    continue
                seen.add(identity)
                label, suggestion = RULE_LABELS.get(key, ("平台提示刊登资料待改善", "按平台原始提示核对并完善商品资料。"))
                if key not in RULE_LABELS:
                    if "PICTURE" in key:
                        label, suggestion = "商品图片待改善", "按平台提示核对图片数量和质量。"
                    elif "TITLE" in key:
                        label, suggestion = RULE_LABELS["TITLE"]
                    elif "GTIN" in key:
                        label, suggestion = RULE_LABELS["HAS_GTIN"]
                    elif "ATTRIBUTE" in key:
                        label, suggestion = "商品属性待完善", "核对类目必填属性，并填写真实商品参数。"
                yield {"key": "quality:" + key, "category": "quality", "source": "official",
                       "severity": "medium" if rule.get("mode") == "WARNING" else "low",
                       "title": label, "evidence": evidence or key, "suggestion": suggestion,
                       "official_url": safe_link(words.get("link")), "rule_key": key}
            for key in ("buckets", "variables"):
                for child in node.get(key) or []:
                    yield from walk(child)

    yield from walk(payload)


def evaluate(item, sources, *, checked_at=None):
    now = checked_at or datetime.now(timezone.utc)
    issues = []
    quality = sources.get("quality", {})
    experience = sources.get("experience", {})
    inventory = sources.get("item", {})
    quality_data = quality.get("data") or {}
    exp = experience.get("data") or {}
    quality_score = score(quality_data.get("score")) if quality.get("status") == "ok" else None
    reputation = exp.get("reputation") or {}
    experience_score = score(reputation.get("value")) if experience.get("status") == "ok" else None
    if quality.get("status") == "ok":
        issues.extend(pending_quality_rules(quality_data))
        if quality_score is not None and str(quality_data.get("level") or "").lower() in {"bad", "average", "basic", "standard"} and not issues:
            issues.append({"key": "quality:score", "category": "quality", "source": "official", "severity": "medium",
                           "title": "刊登质量待改善", "evidence": f"平台质量分 {quality_score:g}；等级 {quality_data.get('level')}",
                           "suggestion": "进入平台检查待完善的刊登资料。"})
    if experience.get("status") == "ok":
        color = str(reputation.get("color") or "").lower()
        title = plain(exp.get("title"))
        reasoning = exp.get("reasoning") or {}
        reasoning_text = [text for row in reasoning.get("subtitles") or [] if (text := plain(row))]
        suggestions = exp.get("recommendations") or {}
        recommendation_text = [text for row in suggestions.get("subtitles") or [] if (text := plain(row))]
        problems = (exp.get("metrics_details") or {}).get("problems") or []
        for index, problem in enumerate(problems):
            if not isinstance(problem, dict):
                continue
            level_two, level_three = problem.get("level_two") or {}, problem.get("level_three") or {}
            evidence = "；".join(filter(None, [plain(level_two.get("title")), plain(level_three.get("title")), plain(problem.get("quantity"))]))
            issues.append({"key": f"experience:{problem.get('key') or 'problem'}:{index}", "category": "experience",
                           "source": "official", "severity": "high" if str(problem.get("color") or color) in {"red", "orange"} else "medium",
                           "title": "配送体验问题" if problem.get("key") == "SHIPPING" else "商品购物体验问题",
                           "evidence": evidence or title or str(problem.get("key") or "平台购物体验提示"),
                           "suggestion": "结合平台提示核对相关订单、投诉与商品描述。",
                           "official_suggestion": plain(level_three.get("remedy"))})
        exp_status = str((exp.get("status") or {}).get("id") or "")
        if color in {"red", "orange", "yellow"} or recommendation_text or exp_status in {"moderated", "paused"}:
            issues.append({"key": "experience:summary", "category": "experience", "source": "official",
                           "severity": "high" if color in {"red", "orange"} or exp_status == "moderated" else "medium",
                           "title": "购物体验需要关注", "evidence": "；".join(filter(None, [title, *reasoning_text])) or f"平台购物体验颜色：{color}",
                           "suggestion": "核对平台原因与建议，检查履约流程和商品质量。",
                           "official_suggestion": "；".join(filter(None, [plain(exp.get("principal_actionable")), *recommendation_text])),
                           "freeze_notice": plain(exp.get("freeze"))})
    if inventory.get("status") in {"ok", "partial"}:
        current = inventory.get("data") or {}
        status = str(current.get("status") or "")
        qty = current.get("available_quantity")
        if status == "active" and type(qty) in (int, float) and qty == 0:
            issues.append({"key": "inventory:out_of_stock", "category": "inventory", "source": "local_rule", "severity": "high",
                           "title": "在售商品可售库存为 0", "evidence": "平台商品状态 active，可售库存 0。",
                           "suggestion": "核对平台库存及在途补货，确认是否需要采购。"})
        if status in {"paused", "under_review", "closed"}:
            issues.append({"key": "listing:" + status, "category": "listing", "source": "local_rule", "severity": "medium",
                           "title": {"paused": "商品已暂停", "under_review": "商品正在审核", "closed": "商品已关闭"}[status],
                           "evidence": f"平台当前商品状态：{status}。", "suggestion": "核对暂停、审核或关闭原因及平台允许的处理动作。"})
    complete = all(sources.get(key, {}).get("status") == "ok" for key in ("item", "quality", "experience"))
    risk = min((issue["severity"] for issue in issues), key=RISK_ORDER.get) if issues else "healthy" if complete else "pending"
    issues.sort(key=lambda issue: RISK_ORDER[issue["severity"]])
    return {**item, "quality_score": quality_score, "experience_score": experience_score,
            "risk": risk, "data_status": "complete" if complete else "partial",
            "issues": issues, "sources": sources, "checked_at": now.isoformat(timespec="seconds"),
            "valid_until": (now + timedelta(hours=24)).isoformat(timespec="seconds"),
            "issue_count": len(issues)}
