"""仅根据 zying_product 商品标题审核侵权风险。

风险级别会写入“疑似侵权”字段：
0 = 未发现可疑性，1 = 有侵权风险/需人工复核，2 = 有明确侵权特征。
对于 1/2 级结果，同时把品牌或 IP 关键词写入“侵权关键词”字段。

主图链接和 Logo OCR 暂不参与判断。
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path


if __package__ in (None, ""):
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

try:
    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="backslashreplace",
        line_buffering=True,
    )
    sys.stderr.reconfigure(
        encoding="utf-8",
        errors="backslashreplace",
        line_buffering=True,
    )
except (AttributeError, ValueError):
    pass

from AI_Agent.deepseek import chat_deepseek
from bit.bit_mysql import get_zying_risk_candidates, update_zying_product_risks
from bit.bit_risk import (
    DEFAULT_BATCH_SIZE,
    _extract_json_payload,
)


DEFAULT_HOURS = max(0, int(os.environ.get("BIT_CHECK_RISK_HOURS", "0")))
DEFAULT_AI_RETRIES = max(0, int(os.environ.get("BIT_CHECK_RISK_AI_RETRIES", "1")))

RISK_LABELS = {
    0: "无可疑",
    1: "疑似/需复核",
    2: "明确侵权特征",
}


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _emit_log(message, log_callback=None):
    text = str(message or "").strip()
    if not text:
        return
    print(text, flush=True)
    if log_callback is not None:
        log_callback(text)


def _normalize_keywords(value):
    if isinstance(value, str):
        values = re.split(r"[,;，；|\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []

    keywords = []
    seen = set()
    for value in values:
        keyword = re.sub(r"\s+", " ", str(value or "")).strip(" .,:;，；|")
        key = keyword.casefold()
        if not keyword or key in seen:
            continue
        seen.add(key)
        keywords.append(keyword[:100])
        if len(keywords) >= 20:
            break
    return keywords


def _coerce_risk_level(value):
    if isinstance(value, bool):
        return 1 if value else 0
    text = str(value if value is not None else "").strip().lower()
    aliases = {
        "0": 0,
        "none": 0,
        "safe": 0,
        "false": 0,
        "无": 0,
        "无风险": 0,
        "1": 1,
        "risk": 1,
        "suspected": 1,
        "true": 1,
        "疑似": 1,
        "有风险": 1,
        "2": 2,
        "infringement": 2,
        "infringing": 2,
        "侵权": 2,
        "确定侵权": 2,
    }
    if text not in aliases:
        raise ValueError(f"无法识别风险级别 {value!r}")
    return aliases[text]


def _normalize_ai_results(payload, records):
    if isinstance(payload, dict):
        payload = payload.get("results") or payload.get("data") or []
    if not isinstance(payload, list):
        raise ValueError("DeepSeek 返回结果不是数组")

    wanted_ids = {int(record["row_id"]) for record in records}
    normalized = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            row_id = int(item.get("row_id"))
        except (TypeError, ValueError):
            continue
        if row_id not in wanted_ids:
            continue

        risk_value = item.get("risk_level", item.get("risk"))
        if risk_value is None and "suspected" in item:
            risk_value = item.get("suspected")
        risk_level = _coerce_risk_level(risk_value)
        keywords = _normalize_keywords(
            item.get("keywords")
            or item.get("brands")
            or item.get("brand")
            or item.get("infringement_keywords")
        )
        if risk_level == 0:
            keywords = []
        elif not keywords:
            raise ValueError(f"数据行 {row_id} 风险级别为 {risk_level}，但 AI 未返回品牌/IP 关键词")

        normalized[row_id] = {
            "row_id": row_id,
            "risk_level": risk_level,
            "keywords": keywords,
            "reason": str(item.get("reason") or "").strip()[:500],
        }

    missing_ids = wanted_ids.difference(normalized)
    if missing_ids:
        raise ValueError(
            "AI 未返回以下数据行，本批次不会写库："
            + ", ".join(str(row_id) for row_id in sorted(missing_ids))
        )
    return [normalized[int(record["row_id"])] for record in records]


def _build_products_payload(records):
    return [
        {
            "row_id": int(record["row_id"]),
            "product_id": str(record.get("product_id") or ""),
            "title": str(record.get("title") or "")[:1000],
            "product_category": str(record.get("product_category") or "")[:500],
            "zying_category": str(record.get("zying_category") or "")[:500],
        }
        for record in records
    ]


def classify_risk_records(records, model=None, retries=DEFAULT_AI_RETRIES):
    """让 AI 仅根据商品标题返回 0/1/2 级风险。"""
    products = _build_products_payload(records)
    system_prompt = """你是跨境电商知识产权风险审核员。请只根据商品标题做筛查。

风险级别定义：
- 0：普通品类词、功能、颜色、尺寸、材质等，未发现品牌、商标或受保护 IP。
- 1：有风险但证据不足，需人工复核。例如兼容性表述、疑似变体词/错拼品牌、不确定是否受保护的角色或图案文字。
- 2：标题明确把知名品牌、商标、影视/动漫/游戏 IP、角色、名人、球队等用作商品本体或装饰，具有明确侵权特征。

规则：
1. “适用于/兼容/for/compatible with”后的品牌通常先判 1，除非标题同时包含仿冒或受保护图案的直接证据。
2. 主图链接、图片内容和 Logo 不在本次检测范围内，禁止根据图片或常识补充判断。
3. risk_level 为 1 或 2 时，keywords 必须列出标题中命中的品牌、人物或 IP；为 0 时 keywords 必须是空数组。
4. 每个 row_id 必须返回且只返回一次。只返回 JSON 数组，不要 Markdown 或其他文字。

返回格式：
[{"row_id":123,"risk_level":0,"keywords":[],"reason":"未发现品牌或 IP"}]
"""
    user_prompt = "请逐条审核以下商品：\n" + json.dumps(products, ensure_ascii=False)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    last_error = None
    for attempt in range(max(0, int(retries)) + 1):
        response = chat_deepseek(
            messages,
            model=model,
            temperature=0,
            max_tokens=max(1600, len(records) * 140),
        )
        try:
            return _normalize_ai_results(_extract_json_payload(response), records)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt >= max(0, int(retries)):
                break
            messages.extend(
                [
                    {"role": "assistant", "content": response},
                    {
                        "role": "user",
                        "content": (
                            f"上次返回无法使用：{exc}。"
                            "请按要求重新返回包含所有 row_id 的完整 JSON 数组。"
                        ),
                    },
                ]
            )
    raise ValueError(f"AI 结果解析失败：{last_error}")


def scan_products(
    zying_category=None,
    hours=DEFAULT_HOURS,
    limit=0,
    batch_size=DEFAULT_BATCH_SIZE,
    model=None,
    retries=DEFAULT_AI_RETRIES,
    recheck=False,
    dry_run=False,
    candidate_reader=None,
    risk_writer=None,
    log_callback=None,
):
    candidate_reader = candidate_reader or get_zying_risk_candidates
    risk_writer = risk_writer or update_zying_product_risks
    records = candidate_reader(
        hours=hours,
        limit=limit,
        zying_category=zying_category,
        include_checked=recheck,
    )
    records = [record for record in records if record.get("row_id") is not None]
    scope = f"智赢分类 {zying_category!r}" if zying_category else "全部智赢分类"
    time_scope = f"最近 {hours} 小时" if hours else "不限入库时间"
    _emit_log(
        f"读取 {scope}、{time_scope}的候选商品 {len(records)} 条",
        log_callback,
    )
    if not records:
        return {
            "checked": 0,
            "risk_0": 0,
            "risk_1": 0,
            "risk_2": 0,
            "updated": 0,
            "results": [],
        }

    checked = 0
    updated = 0
    all_results = []
    batches = list(_chunks(records, max(1, int(batch_size))))
    for batch_index, batch in enumerate(batches, start=1):
        _emit_log(
            f"开始标题审核批次 {batch_index}/{len(batches)}，{len(batch)} 条",
            log_callback,
        )
        title_records = [dict(record) for record in batch]
        results = classify_risk_records(title_records, model=model, retries=retries)
        if not dry_run:
            updated += risk_writer(results)

        checked += len(results)
        all_results.extend(results)
        counts = {level: 0 for level in RISK_LABELS}
        records_by_id = {int(record["row_id"]): record for record in title_records}
        for result in results:
            level = result["risk_level"]
            counts[level] += 1
            if level:
                record = records_by_id[result["row_id"]]
                _emit_log(
                    f"{RISK_LABELS[level]}：数据库行 {result['row_id']}，"
                    f"产品 {record.get('product_id') or '无编号'}，"
                    f"关键词：{', '.join(result['keywords'])}，"
                    f"原因：{result['reason']}",
                    log_callback,
                )
        _emit_log(
            f"批次 {batch_index}/{len(batches)} 完成："
            f"0 级 {counts[0]} 条，1 级 {counts[1]} 条，2 级 {counts[2]} 条"
            + ("（演练模式未写库）" if dry_run else ""),
            log_callback,
        )

    summary = {
        "checked": checked,
        "risk_0": sum(item["risk_level"] == 0 for item in all_results),
        "risk_1": sum(item["risk_level"] == 1 for item in all_results),
        "risk_2": sum(item["risk_level"] == 2 for item in all_results),
        "updated": updated,
        "results": all_results,
    }
    _emit_log(
        f"风险审核完成：检查 {summary['checked']} 条，"
        f"0 级 {summary['risk_0']} 条，1 级 {summary['risk_1']} 条，"
        f"2 级 {summary['risk_2']} 条，数据库更新 {summary['updated']} 条",
        log_callback,
    )
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="读取 zying_product 标题，用 AI 写入 0/1/2 侵权风险"
    )
    parser.add_argument(
        "--category",
        "--zying-category",
        dest="zying_category",
        default=None,
        help="智赢分类编号、完整路径或末级名称；不填表示全部分类",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_HOURS,
        help=f"只审核最近多少小时入库的数据，0 表示不限（默认 {DEFAULT_HOURS}）",
    )
    parser.add_argument("--limit", type=int, default=0, help="最多审核多少条，0 表示不限")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"每次发送给 AI 的商品数（默认 {DEFAULT_BATCH_SIZE}）",
    )
    parser.add_argument("--model", default=None, help="DeepSeek 模型，默认读取项目配置")
    parser.add_argument(
        "--ai-retries",
        type=int,
        default=DEFAULT_AI_RETRIES,
        help=f"AI JSON 结果不完整时的重试次数（默认 {DEFAULT_AI_RETRIES}）",
    )
    parser.add_argument(
        "--recheck",
        action="store_true",
        help="重新审核已有 0/1/2 结果的商品",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只输出判断结果，不写入风险级别和关键词",
    )
    args = parser.parse_args()
    try:
        scan_products(
            zying_category=args.zying_category,
            hours=max(0, args.hours),
            limit=max(0, args.limit),
            batch_size=max(1, args.batch_size),
            model=args.model,
            retries=max(0, args.ai_retries),
            recheck=args.recheck,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        parser.exit(status=1, message=f"风险审核失败：{exc}\n")


if __name__ == "__main__":
    main()
