import copy
import json
import math
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


DEFAULTS = {
    "workflow_mode": "image_first",
    "daily_limit": 25, "consult_interval_seconds": 60, "max_waiting": 2,
    "poll_minutes": 15, "timeout_minutes": 30,
    "small_tolerance_g": 50, "large_tolerance_g": 30,
    "reference_mode": "erp", "match_threshold": 0.95,
    "writeback_enabled": False, "max_candidates": 10, "max_pages": 100,
    "supplier_auto_adapt": True,
    "sku_price_mode": "final", "usd_cny_rate": None,
    "phrases": ["您好，请问这款产品包装好之后重量大概多少克呢？",
                "你好，想问下这款商品连包装的重量是多少g？",
                "咨询下，这个货品打包完成包装重量多少克？"],
    "cdp_url": "http://127.0.0.1:9222",
    "erp_list_url": "https://meli.zying.net/#/product",
    "supplier_home_url": "https://www.1688.com/",
    "api_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key_env": "DASHSCOPE_API_KEY", "model": "qwen3-vl-flash",
    "review_model": "qwen3-vl-plus", "weight_model": "qwen3-vl-flash",
    "api_timeout_seconds": 60,
    "selectors": {
        "erp_rows": ".product-item", "erp_id": ".product-id",
        "erp_title": ".product-title", "erp_image": "img.product-pic",
        "erp_description": "", "erp_sku": "", "erp_reference": "",
        "erp_edit_link": "", "erp_next": "li.ant-pagination-next:not(.ant-pagination-disabled) button",
        "erp_category_control": ".ant-cascader", "erp_search": "", "erp_page_active": "li.ant-pagination-item-active",
        "erp_page_first": "li.ant-pagination-item[title='1']",
        "erp_edit_id": ".curd-detail-wrap .crud-detail-header .h1", "erp_edit_sku": "", "erp_cost_input": "",
        "erp_net_income_input": ".curd-detail-wrap #netproceed",
        "erp_weight_input": ".curd-detail-wrap #weight", "erp_save": "", "erp_saved": "",
        "search_input": "", "search_button": "", "result_links": "a[href*='detail.1688.com/offer/']",
        "image_search_open": "", "image_search_upload": "input[type='file']", "image_search_submit": "",
        "supplier_title": "", "supplier_image": "", "supplier_description": "",
        "supplier_weight": "", "sku_weight": "",
        "supplier_merchant": "", "supplier_merchant_attribute": "data-member-id",
        "sku_rows": "", "sku_id_attribute": "data-sku-id", "sku_label": "", "sku_price": "",
        "sku_surcharge": "",
        "chat_open": "", "chat_identity": "", "chat_identity_attribute": "data-member-id",
        "chat_input": "", "chat_send": "", "chat_messages": "",
        "chat_message_id_attribute": "data-message-id", "chat_message_time_attribute": "data-timestamp",
        "risk": "iframe[src*='captcha'], #nc_1_wrapper, .baxia-dialog",
    },
}


def data_dir():
    root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    return Path(os.environ.get("AI_WEIGHT_PRICE_DATA_DIR") or root / "bit" / "runtime_locks" / "ai_weight_price")


def safe_url(value, host=None, local=False):
    parsed = urlsplit(str(value))
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("地址必须是 HTTP(S)，且不能包含用户名或密码")
    if local and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Edge 调试地址只能使用本机回环地址")
    if host and parsed.hostname != host and not parsed.hostname.endswith("." + host):
        raise ValueError("页面域名不符合配置")
    return str(value)


def validate(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("配置字段不正确")
    result = copy.deepcopy(DEFAULTS)
    result.update(value)
    if not isinstance(value.get("selectors", {}), dict):
        raise ValueError("selectors 必须是 DOM 字段对象")
    result["selectors"] = {**DEFAULTS["selectors"], **value.get("selectors", {})}
    if set(result["selectors"]) - set(DEFAULTS["selectors"]):
        raise ValueError("未知 DOM 字段")
    for key, low, high in [("daily_limit", 1, 1000), ("consult_interval_seconds", 60, 86400),
                           ("max_waiting", 1, 2), ("poll_minutes", 1, 1440),
                           ("timeout_minutes", 1, 10080), ("small_tolerance_g", 0, 50),
                           ("large_tolerance_g", 0, 30), ("max_candidates", 1, 20),
                           ("max_pages", 1, 10000), ("api_timeout_seconds", 5, 300)]:
        number = result[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or int(number) != number or not low <= number <= high:
            raise ValueError(f"{key} 必须是 {low}–{high} 的整数")
        result[key] = int(number)
    threshold = result["match_threshold"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not .95 <= threshold < 1:
        raise ValueError("匹配门槛必须 ≥0.95 且 <1；达到门槛即可通过")
    if result["reference_mode"] not in ("erp", "manual", "disabled"):
        raise ValueError("重量对照模式无效")
    if result["workflow_mode"] not in ("image_first", "legacy_consult"):
        raise ValueError("核重核价流程模式无效")
    if type(result["writeback_enabled"]) is not bool:
        raise ValueError("回写开关必须是布尔值")
    if type(result["supplier_auto_adapt"]) is not bool:
        raise ValueError("1688自动适配开关必须是布尔值")
    if result["sku_price_mode"] not in ("final", "base_plus_surcharge"):
        raise ValueError("变体价格模式必须为最终单价或基础价加变体加价")
    if result["usd_cny_rate"] not in (None, ""):
        from .models import number
        result["usd_cny_rate"] = str(number(result["usd_cny_rate"]))
    else:
        result["usd_cny_rate"] = None
    phrases = result["phrases"]
    if not isinstance(phrases, list) or not 2 <= len(phrases) <= 100 or any(not isinstance(p, str) or not p.strip() or len(p) > 300 for p in phrases) or len(set(phrases)) < 2:
        raise ValueError("请提供至少两条不同的咨询话术，每条不超过300字")
    for key in ("api_key_env", "model", "review_model", "weight_model"):
        if not isinstance(result[key], str) or not result[key].strip():
            raise ValueError(f"{key} 不得为空")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", result["api_key_env"]):
        raise ValueError("密钥请填写环境变量名称")
    for key, val in result["selectors"].items():
        if not isinstance(val, str) or len(val) > 2000:
            raise ValueError(f"DOM 字段 {key} 无效")
    safe_url(result["cdp_url"], local=True)
    safe_url(result["erp_list_url"], host="meli.zying.net")
    if result["erp_list_url"] != "https://meli.zying.net/#/product":
        raise ValueError("智赢商品页固定使用美客多版 https://meli.zying.net/#/product")
    safe_url(result["supplier_home_url"], host="1688.com")
    safe_url(result["api_base_url"])
    if urlsplit(result["api_base_url"]).scheme != "https":
        safe_url(result["api_base_url"], local=True)
    return result


def selection_params(value, config):
    if not isinstance(value, dict):
        raise ValueError("请先选择分类（可留空）、起始产品编号（可留空）和最多商品数")
    if set(value) - {"category", "start_page", "end_page", "start_item", "start_product_id"}:
        raise ValueError("任务参数字段不正确")
    category = value.get("category", "")
    if not isinstance(category, str) or len(category) > 500:
        raise ValueError("分类格式不正确")
    result = {"category": category.strip()}
    if "start_product_id" in value:
        start_product_id = value.get("start_product_id", "")
        if not isinstance(start_product_id, str) or len(start_product_id) > 64:
            raise ValueError("起始产品编号格式不正确")
        start_product_id = start_product_id.strip()
        if start_product_id and not re.fullmatch(r"[1-9]\d*", start_product_id):
            raise ValueError("起始产品编号必须是正整数，或留空从分类首件开始")
        if set(value) & {"start_page", "end_page", "start_item"}:
            raise ValueError("起始产品编号不能与旧版页码范围同时提交")
        result["start_product_id"] = start_product_id
        return result
    for field in ("start_page", "end_page"):
        num = value.get(field)
        if type(num) is not int or not 1 <= num <= 10000:
            raise ValueError("起始页和结束页必须是1–10000的整数")
        result[field] = num
    # Keep old saved selections compatible while allowing a deterministic
    # one-based item offset on the first selected page.  The UI always sends
    # this field; old API callers may omit it and therefore mean item 1.
    if "start_item" in value:
        num = value.get("start_item")
        if type(num) is not int or not 1 <= num <= 10000:
            raise ValueError("本页起始商品序号必须是1–10000的整数")
        result["start_item"] = num
    if result["end_page"] < result["start_page"]:
        raise ValueError("结束页不能小于起始页")
    if result["end_page"] - result["start_page"] + 1 > config["max_pages"]:
        raise ValueError(f"所选范围超过单次最多 {config['max_pages']} 页，请缩小范围或调整参数")
    return result


def selection_key(selection, config):
    import hashlib
    identity = {**selection, "url": config["erp_list_url"]}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Config:
    def __init__(self, root):
        self.path = Path(root) / "config.json"

    def load(self):
        value = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        # Existing installations may still contain the general-console URL.
        # Migrate it before strict validation so no workflow can return there.
        if value.get("erp_list_url") != DEFAULTS["erp_list_url"]:
            value["erp_list_url"] = DEFAULTS["erp_list_url"]
        return validate(value)

    def save(self, value):
        result = validate(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        return result
