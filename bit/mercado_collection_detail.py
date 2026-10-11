"""Build a complete, display-safe view of a saved Mercado collection record."""
import json
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlsplit


def parsed(value, default):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return default
    return value if isinstance(value, type(default)) else default


def safe_url(value):
    value = str(value or "").strip()
    if value.startswith("//"):
        value = "https:" + value
    try:
        url = urlsplit(value)
        return value if url.scheme.lower() in {"http", "https"} and url.hostname else ""
    except ValueError:
        return ""


def display(value):
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value)


def metric(value, unit=""):
    try:
        number = Decimal(str(value))
        if number.is_finite():
            return f"{number:,.2f}".rstrip("0").rstrip(".") + (" " + unit if unit else "")
    except (InvalidOperation, ValueError):
        pass
    return "—"


class PlainDescription(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden and tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in {"p", "div", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def description_text(value):
    if isinstance(value, dict):
        plain = value.get("plain_text") or value.get("text")
        if plain:
            return str(plain)
        value = value.get("body") or value.get("html") or ""
    if not value:
        return ""
    parser = PlainDescription()
    parser.feed(str(value))
    return "".join(parser.parts).strip()


def attribute_rows(value):
    if isinstance(value, dict):
        value = [{"name": key, "value": item} for key, item in value.items()]
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("id") or "属性"
        content = item.get("value_name")
        if content in (None, ""):
            content = item.get("value")
        if content in (None, "") and item.get("values"):
            content = " / ".join(display(v.get("name") or v.get("value_name") or v.get("id"))
                                 if isinstance(v, dict) else display(v) for v in item["values"])
        if content in (None, "") and item.get("value_struct"):
            struct = item["value_struct"]
            content = f"{struct.get('number', '')} {struct.get('unit', '')}".strip()
        if content in (None, ""):
            content = item.get("value_id")
        result.append({"name": str(name), "value": display(content)})
    return result


def build_collection_detail(row):
    from erp.mercadolibre_collection_quality import collection_quality_issues, finite_number
    source = parsed(row.get("source_json") or row.get("source"), {})
    page = parsed(row.get("page_snapshot_json") or row.get("page_snapshot"), {})
    plugin = parsed(row.get("plugin_snapshot_json") or row.get("plugin_snapshot"), {})
    copy = parsed(row.get("ai_copy_json") or row.get("ai_copy"), {})
    audit = parsed(row.get("ai_weight_price_json") or row.get("ai_weight_price"), {})
    raw_description = row.get("description_json") or row.get("description") or {}
    if isinstance(raw_description, str):
        try:
            description = json.loads(raw_description)
        except ValueError:
            description = raw_description
    else:
        description = raw_description
    images, picture_ids = [], {}

    def add_picture(picture):
        if isinstance(picture, dict):
            url = next((safe_url(picture.get(key)) for key in
                        ("secure_url", "url", "source", "image_url", "src") if safe_url(picture.get(key))), "")
            if picture.get("id") and url:
                picture_ids[str(picture["id"])] = url
        else:
            url = safe_url(picture)
        if url and url not in images:
            images.append(url)
        return url

    add_picture(row.get("main_image_url"))
    for data in (source, page, plugin):
        for key in ("pictures", "images", "image_urls"):
            for picture in data.get(key) or []:
                add_picture(picture)
    variants = []
    for variant in source.get("variations") or []:
        if not isinstance(variant, dict):
            continue
        variant_images = []
        for key in ("pictures", "images", "image_urls"):
            for picture in variant.get(key) or []:
                url = add_picture(picture)
                if url:
                    variant_images.append(url)
        variant_images.extend(picture_ids[str(pid)] for pid in variant.get("picture_ids") or []
                              if str(pid) in picture_ids)
        variants.append({
            "id": display(variant.get("id")),
            "attributes": attribute_rows(variant.get("attribute_combinations")) + attribute_rows(variant.get("attributes")),
            "price": metric(variant.get("price"), row.get("currency_id") or source.get("currency_id") or ""),
            "quantity": display(variant.get("available_quantity")),
            "sku": display(variant.get("seller_custom_field") or variant.get("seller_sku")),
            "images": list(dict.fromkeys(variant_images)),
            "other": [{"name": {"sold_quantity": "已售数量", "catalog_product_id": "目录商品编号"}.get(key, key),
                       "value": display(value)} for key, value in variant.items()
                      if key not in {"id", "attribute_combinations", "attributes", "price", "available_quantity",
                                     "seller_custom_field", "seller_sku", "pictures", "images", "image_urls", "picture_ids"}
                      and value not in (None, "", [], {})],
        })
    attributes = []
    seen = set()
    for items in (source.get("attributes"), row.get("attributes"), page.get("specs"), plugin.get("attributes")):
        for item in attribute_rows(items):
            key = (item["name"], item["value"])
            if key not in seen:
                attributes.append(item)
                seen.add(key)

    def fields(pairs):
        return [{"name": name, "value": display(value)} for name, value in pairs]

    money = finite_number(row.get("sale_price_usd"))
    shipping = finite_number(row.get("shipping_fee_usd"))
    commission, net = None, None
    if money is not None and money > 0:
        commission = Decimal(str(money)) * Decimal("0.15")
        if shipping is not None and shipping >= 0:
            net = Decimal(str(money)) - commission - Decimal(str(shipping))
    current_description = (row.get("description_text") or description_text(description)
                           or description_text(source.get("description")) or description_text(page.get("description")))
    descriptions = [{"name": "商品描述", "title": "", "text": current_description}]
    for language, label in (("es", "西班牙语文案"), ("pt", "葡萄牙语文案")):
        if copy.get(f"title_{language}") or copy.get(f"description_{language}"):
            descriptions.append({"name": label, "title": copy.get(f"title_{language}") or "",
                                 "text": copy.get(f"description_{language}") or ""})
    if copy.get("source_description") and copy["source_description"] != current_description:
        descriptions.append({"name": "原始采集描述", "title": copy.get("source_title") or "",
                             "text": copy["source_description"]})
    review = {"unreviewed": "未审核", "approved": "通过", "suspected": "疑似", "infringing": "侵权", "risk": "风险"}
    publish = {"unpublished": "未上架", "pending_review": "待审核", "pending": "等待上架", "publishing": "上架中",
               "published": "上架成功", "failed": "上架失败"}
    status = {"pending": "待采集", "ok": "采集成功", "partial": "资料待补充", "failed": "采集失败"}
    audit_labels = {"queued": "待处理", "checking": "进行中", "checked": "核重核价成功", "failed": "核重核价失败",
                    "unmatched": "未匹配", "missing_evidence": "数据不足", "conflict": "资料冲突", "stopped": "已停止"}
    risk_labels = {0: "低风险", 1: "疑似风险", 2: "高风险"}
    source_labels = {"seller_id": "卖家编号", "sold_quantity": "已售数量", "warranty": "保修说明",
                     "catalog_product_id": "目录商品编号", "domain_id": "商品域", "tags": "商品标签",
                     "shipping": "平台物流信息", "location": "商品所在地", "seller_address": "卖家地址",
                     "video_id": "商品视频编号", "seller_custom_field": "卖家 SKU", "status": "源商品状态"}
    used = {"id", "title", "site_id", "price", "currency_id", "condition", "available_quantity", "permalink",
            "pictures", "images", "image_urls", "attributes", "variations", "sale_terms", "description", "category_id"}
    return {
        "quality_issues": collection_quality_issues(row),
        "id": row.get("id"), "item_id": row.get("source_item_id") or source.get("id") or "—",
        "title": row.get("title") or source.get("title") or "未识别标题",
        "source_url": safe_url(row.get("final_url") or row.get("source_url") or source.get("permalink")),
        "images": images, "attributes": attributes, "variants": variants, "descriptions": descriptions,
        "basic": fields([
            ("商品编号", row.get("source_item_id") or source.get("id")), ("站点", source.get("site_id")),
            ("泽顺业务员", row.get("salesperson") or row.get("owner_name")),
            ("商品分类", row.get("category_name") or row.get("category_id") or source.get("category_id")),
            ("商品成色", {"new": "全新", "used": "二手", "not_specified": "未注明"}.get(source.get("condition"), source.get("condition"))),
            ("可售数量", source.get("available_quantity")), ("刊登类型", row.get("listing_type_name") or row.get("listing_type_id")),
            ("原始采集标题", copy.get("source_title") or page.get("page_title") or source.get("title")),
        ]),
        "financial": fields([
            ("原价", metric(row.get("price"), row.get("currency_id") or source.get("currency_id") or "")),
            ("美元售价", metric(money, "USD")), ("佣金（固定 15%）", metric(commission, "USD")),
            ("当前运费", metric(shipping, "USD")), ("净收益", metric(net, "USD")),
            ("实际重量", metric(row.get("weight_g"), "g")),
            ("包装长 / 宽 / 高", " × ".join(metric(row.get(key)) for key in
             ("package_length_cm", "package_width_cm", "package_height_cm")) + " cm"),
            ("计泡重", metric(row.get("volumetric_weight_kg"), "kg")),
            ("计费重量", metric(row.get("billable_weight_g"), "g")),
            ("换算汇率", row.get("exchange_rate_to_usd")), ("汇率更新时间", row.get("exchange_rate_updated_at")),
            ("运费更新时间", row.get("profitability_updated_at")),
        ]),
        "records": fields([
            ("采集状态", status.get(row.get("scrape_status"), row.get("scrape_status"))),
            ("审核状态", review.get(row.get("review_status") or "unreviewed", row.get("review_status"))),
            ("上架状态", publish.get(row.get("last_publish_status") or "unpublished", row.get("last_publish_status"))),
            ("已加入产品列表", bool(row.get("added_to_products"))), ("采集时间", row.get("collected_at")),
            ("更新时间", row.get("updated_at")), ("采集提示", row.get("error_message")),
            ("运费提示", row.get("profitability_error")), ("重量与尺寸来源", plugin.get("source") or row.get("weight_basis")),
            ("插件重量", plugin.get("weight_display")), ("插件尺寸", plugin.get("dimensions_display")),
            ("核重核价状态", audit_labels.get(audit.get("status"), audit.get("status") or "尚未执行核重核价")),
            ("核重核价说明", audit.get("reason")), ("核重核价时间", audit.get("checked_at")),
            ("侵权风险", risk_labels.get(row.get("infringement_risk_level"), "未检测")),
            ("侵权检测说明", row.get("infringement_reason")), ("侵权检测关键词", row.get("infringement_keywords")),
            ("侵权检测时间", row.get("infringement_checked_at")),
        ]),
        "terms": attribute_rows(source.get("sale_terms")),
        "other": fields([(source_labels.get(key, key), value) for key, value in source.items()
                         if key not in used and value not in (None, "", [], {})]),
    }
