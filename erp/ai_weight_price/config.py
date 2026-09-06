import copy
import json
import math
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


DEFAULTS = {
    "daily_limit": 25, "consult_interval_seconds": 60, "max_waiting": 2,
    "poll_minutes": 15, "timeout_minutes": 30,
    "small_tolerance_g": 50, "large_tolerance_g": 30,
    "reference_mode": "erp", "match_threshold": 0.95,
    "writeback_enabled": False, "max_candidates": 5, "max_pages": 100,
    "phrases": ["您好，请问这款产品包装好之后重量大概多少克呢？",
                "你好，想问下这款商品连包装的重量是多少g？",
                "咨询下，这个货品打包完成包装重量多少克？"],
    "cdp_url": "http://127.0.0.1:9222",
    "erp_list_url": "https://seller.zying.net/#/product",
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
        "erp_category_control": ".ant-cascader", "erp_page_active": "li.ant-pagination-item-active",
        "erp_page_first": "li.ant-pagination-item[title='1']",
        "erp_edit_id": "", "erp_edit_sku": "", "erp_cost_input": "",
        "erp_weight_input": "", "erp_save": "", "erp_saved": "",
        "search_input": "", "search_button": "", "result_links": "a[href*='detail.1688.com/offer/']",
        "supplier_title": "", "supplier_image": "", "supplier_description": "",
        "supplier_merchant": "", "supplier_merchant_attribute": "data-member-id",
        "sku_rows": "", "sku_id_attribute": "data-sku-id", "sku_label": "", "sku_price": "",
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
        raise ValueError("匹配门槛必须 ≥0.95 且 <1；仅严格高于门槛才通过")
    if result["reference_mode"] not in ("erp", "manual", "disabled"):
        raise ValueError("重量对照模式无效")
    if type(result["writeback_enabled"]) is not bool:
        raise ValueError("回写开关必须是布尔值")
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
    safe_url(result["erp_list_url"])
    safe_url(result["supplier_home_url"], host="1688.com")
    safe_url(result["api_base_url"])
    if urlsplit(result["api_base_url"]).scheme != "https":
        safe_url(result["api_base_url"], local=True)
    return result


def selection_params(value, config):
    if not isinstance(value, dict):
        raise ValueError("请先选择分类（可留空）、起始页和结束页")
    if set(value) - {"category", "start_page", "end_page"}:
        raise ValueError("任务参数字段不正确")
    category = value.get("category", "")
    if not isinstance(category, str) or len(category) > 500:
        raise ValueError("分类格式不正确")
    result = {"category": category.strip()}
    for field in ("start_page", "end_page"):
        num = value.get(field)
        if type(num) is not int or not 1 <= num <= 10000:
            raise ValueError("起始页和结束页必须是1–10000的整数")
        result[field] = num
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
        return validate(json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {})

    def save(self, value):
        result = validate(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        return result
