"""检查智赢商品标题和主图中的品牌文字，并标记疑似侵权商品。"""

import argparse
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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

import requests

from AI_Agent.deepseek import chat_deepseek
from bit.bit_mysql import (
    get_zying_risk_candidates,
    mark_zying_products_suspected,
)


DEFAULT_HOURS = max(1, int(os.environ.get("BIT_RISK_HOURS", "24")))
DEFAULT_BATCH_SIZE = max(1, int(os.environ.get("BIT_RISK_BATCH_SIZE", "20")))
DEFAULT_WORKERS = max(1, int(os.environ.get("BIT_RISK_WORKERS", "2")))
DEFAULT_OCR_CONFIDENCE = float(os.environ.get("BIT_RISK_OCR_CONFIDENCE", "0.55"))
MAX_IMAGE_BYTES = max(1, int(os.environ.get("BIT_RISK_MAX_IMAGE_MB", "12"))) * 1024 * 1024
IMAGE_TIMEOUT = max(5, int(os.environ.get("BIT_RISK_IMAGE_TIMEOUT", "25")))

_thread_state = threading.local()


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _get_ocr_engine():
    engine = getattr(_thread_state, "ocr_engine", None)
    if engine is not None:
        return engine
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise RuntimeError(
            "缺少图片 OCR 依赖，请执行："
            "E:\\python3.12.0\\python.exe -m pip install "
            "rapidocr-onnxruntime==1.4.4"
        ) from exc
    engine = RapidOCR()
    _thread_state.ocr_engine = engine
    return engine


def _get_http_session():
    session = getattr(_thread_state, "http_session", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/126 Safari/537.36"
                )
            }
        )
        _thread_state.http_session = session
    return session


def _download_image(url):
    response = _get_http_session().get(url, timeout=IMAGE_TIMEOUT, stream=True)
    response.raise_for_status()
    content = bytearray()
    for block in response.iter_content(64 * 1024):
        content.extend(block)
        if len(content) > MAX_IMAGE_BYTES:
            raise RuntimeError(f"图片超过 {MAX_IMAGE_BYTES // 1024 // 1024} MB")
    if not content:
        raise RuntimeError("图片内容为空")
    return bytes(content)


def recognize_image_text(image_url, min_confidence=DEFAULT_OCR_CONFIDENCE):
    """下载主图并提取其中可能属于品牌、Logo 或包装标题的文字。"""
    image_url = str(image_url or "").strip()
    if not image_url:
        return ""
    result, _ = _get_ocr_engine()(_download_image(image_url))
    texts = []
    for row in result or []:
        if len(row) < 3:
            continue
        text = re.sub(r"\s+", " ", str(row[1] or "")).strip()
        try:
            confidence = float(row[2])
        except (TypeError, ValueError):
            confidence = 0
        if text and confidence >= float(min_confidence) and text not in texts:
            texts.append(text)
    return " | ".join(texts)[:1200]


def enrich_records_with_ocr(records, workers=DEFAULT_WORKERS, min_confidence=DEFAULT_OCR_CONFIDENCE):
    records = [dict(record) for record in records]
    url_records = {}
    for record in records:
        url = str(record.get("main_image_url") or "").strip()
        if url:
            url_records.setdefault(url, []).append(record)
        else:
            record["image_ocr_text"] = ""
            record["ocr_error"] = "缺少主图链接"

    completed = 0
    worker_count = min(max(1, int(workers)), max(1, len(url_records)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(recognize_image_text, url, min_confidence): url
            for url in url_records
        }
        for future in as_completed(futures):
            url = futures[future]
            completed += 1
            try:
                ocr_text = future.result()
                error = ""
            except Exception as exc:
                ocr_text = ""
                error = str(exc)
            for record in url_records[url]:
                record["image_ocr_text"] = ocr_text
                record["ocr_error"] = error
            print(
                f"图片 OCR {completed}/{len(url_records)}："
                f"识别 {len(ocr_text)} 字符"
                + (f"，失败 {error}" if error else ""),
                flush=True,
            )
    return records


def _extract_json_payload(text):
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        array_start = text.find("[")
        array_end = text.rfind("]")
        if array_start >= 0 and array_end > array_start:
            return json.loads(text[array_start : array_end + 1])
        object_start = text.find("{")
        object_end = text.rfind("}")
        if object_start >= 0 and object_end > object_start:
            return json.loads(text[object_start : object_end + 1])
        raise


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
        suspected_value = item.get("suspected")
        suspected = suspected_value is True or str(suspected_value).strip().lower() in {
            "1",
            "true",
            "yes",
            "是",
        }
        normalized[row_id] = {
            "row_id": row_id,
            "suspected": suspected,
            "reason": str(item.get("reason") or "").strip()[:500],
        }

    return [
        normalized.get(
            int(record["row_id"]),
            {
                "row_id": int(record["row_id"]),
                "suspected": False,
                "reason": "AI 未返回该行，未标记",
            },
        )
        for record in records
    ]


def classify_risk_records(records, model=None):
    """让 DeepSeek 综合商品标题和图片 OCR 文字进行保守的侵权风险判断。"""
    products = [
        {
            "row_id": int(record["row_id"]),
            "product_id": str(record.get("product_id") or ""),
            "title": str(record.get("title") or "")[:1000],
            "image_ocr_text": str(record.get("image_ocr_text") or "")[:1200],
            "product_category": str(record.get("product_category") or "")[:500],
        }
        for record in records
    ]
    system_prompt = """你是跨境电商知识产权风险审核员。请保守但有效地识别疑似侵权商品。
判断依据只能来自商品标题和主图 OCR 文字：
1. 出现明确的品牌、商标、影视/动漫/游戏 IP、角色、名人、乐队、球队或受保护作品名称，判为 suspected=true；
2. 主图 OCR 出现品牌/Logo 文字，即使标题未写品牌，也判为 true；
3. 普通品类词、颜色、尺寸、材料、功能、无明确权利人的装饰词，不判侵权；
4. 不确定或证据不足时判为 false，禁止猜测图片中并未识别出的纯图形 Logo；
5. 只返回 JSON，不要 Markdown。格式必须为：
[{"row_id":123,"suspected":true,"reason":"标题或OCR中命中的品牌/IP及简短依据"}]"""
    user_prompt = "请审核以下商品：\n" + json.dumps(products, ensure_ascii=False)
    response = chat_deepseek(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        model=model,
        temperature=0,
        max_tokens=max(1200, len(records) * 100),
    )
    return _normalize_ai_results(_extract_json_payload(response), records)


def scan_recent_products(
    hours=DEFAULT_HOURS,
    limit=0,
    batch_size=DEFAULT_BATCH_SIZE,
    workers=DEFAULT_WORKERS,
    min_ocr_confidence=DEFAULT_OCR_CONFIDENCE,
    model=None,
    dry_run=False,
):
    records = get_zying_risk_candidates(hours=hours, limit=limit)
    records = [record for record in records if record.get("row_id") is not None]
    print(f"读取最近 {hours} 小时商品 {len(records)} 条", flush=True)
    if not records:
        return {"checked": 0, "suspected": 0, "updated": 0, "results": []}

    checked = 0
    updated = 0
    all_results = []
    batches = list(_chunks(records, max(1, int(batch_size))))
    for batch_index, batch in enumerate(batches, start=1):
        print(
            f"开始风险审核批次 {batch_index}/{len(batches)}，{len(batch)} 条",
            flush=True,
        )
        enriched = enrich_records_with_ocr(
            batch,
            workers=workers,
            min_confidence=min_ocr_confidence,
        )
        results = classify_risk_records(enriched, model=model)
        suspected_ids = [item["row_id"] for item in results if item["suspected"]]
        checked += len(batch)
        all_results.extend(results)
        for result, record in zip(results, enriched):
            if result["suspected"]:
                print(
                    f"疑似侵权：数据库行 {result['row_id']}，"
                    f"产品 {record.get('product_id') or '无编号'}，"
                    f"原因：{result['reason']}",
                    flush=True,
                )
        if suspected_ids and not dry_run:
            updated += mark_zying_products_suspected(suspected_ids)
        print(
            f"批次 {batch_index}/{len(batches)} 完成：审核 {len(batch)} 条，"
            f"疑似 {len(suspected_ids)} 条"
            + ("（演练模式未写库）" if dry_run else ""),
            flush=True,
        )

    summary = {
        "checked": checked,
        "suspected": sum(1 for item in all_results if item["suspected"]),
        "updated": updated,
        "results": all_results,
    }
    print(
        f"风险审核完成：检查 {summary['checked']} 条，"
        f"疑似侵权 {summary['suspected']} 条，数据库更新 {summary['updated']} 条",
        flush=True,
    )
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="读取智赢商品标题和主图，标记数据库中的疑似侵权商品"
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_HOURS,
        help=f"读取最近多少小时入库的数据（默认 {DEFAULT_HOURS}）",
    )
    parser.add_argument("--limit", type=int, default=0, help="最多审核多少条，0 表示不限")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"每次发送给 AI 的商品数（默认 {DEFAULT_BATCH_SIZE}）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"图片 OCR 并发数（默认 {DEFAULT_WORKERS}）",
    )
    parser.add_argument(
        "--ocr-confidence",
        type=float,
        default=DEFAULT_OCR_CONFIDENCE,
        help=f"OCR 最低置信度（默认 {DEFAULT_OCR_CONFIDENCE}）",
    )
    parser.add_argument("--model", default=None, help="DeepSeek 模型，默认读取项目配置")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只输出判断结果，不更新数据库",
    )
    args = parser.parse_args()
    try:
        scan_recent_products(
            hours=max(1, args.hours),
            limit=max(0, args.limit),
            batch_size=max(1, args.batch_size),
            workers=max(1, args.workers),
            min_ocr_confidence=min(1.0, max(0.0, args.ocr_confidence)),
            model=args.model,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        parser.exit(status=1, message=f"风险审核失败：{exc}\n")


if __name__ == "__main__":
    main()
