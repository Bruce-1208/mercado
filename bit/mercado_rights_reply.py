"""Seller BPP responses. Never replay a write whose outcome is unknown.

API contract: https://global-selling.mercadolibre.com/devsite/devsite/reported-listings
"""
from datetime import datetime, timezone
import json
import logging
import re

import requests

from bit import bit_runtime_lock
from mercado_api.client import MercadoAPIError
from mercado_api.request_budget import admission

RIGHTS_URL = "https://global-selling.mercadolibre.com/noindex/pppi/infractions?tab=denounces&offset=0"
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
IMAGE_REASONS = {"PPPI4", "PPPI6", "PPPI7", "PPPI17"}


def reply_recorded(token_id, case_id):
    """Local inbox completion is separate from platform approval."""
    try:
        marker = bit_runtime_lock.RUNTIME_LOCK_DIR / f"rights-reply-{int(token_id)}-{case_identifier(case_id)}.json"
        return json.loads(marker.read_text(encoding="utf-8")).get("state") in {
            "sending", "accepted", "confirmed", "unknown",
        }
    except (OSError, ValueError, TypeError):
        return False


def case_identifier(value):
    value = str(value or "").strip()
    if not re.fullmatch(r"[0-9]{1,30}", value):
        raise ValueError("案件编号无效")
    return value


def store_client(token_id):
    from bit import bit_mysql, mercado_infraction_sync
    record = dict(bit_mysql.get_mercado_store_token(token_id) or {})
    return mercado_infraction_sync._client_and_token(record, timeout=30)


def read_case(client, case_id, expected_item):
    detail = client.request("GET", f"/moderations/pppi/case/{case_id}")
    if not isinstance(detail, dict):
        raise ValueError("平台案件详情格式异常")
    if str((detail.get("item_info") or {}).get("item_id") or "") != str(expected_item):
        raise ValueError("平台案件商品与当前店铺待办不一致")
    if detail.get("user_type") not in (None, "seller"):
        raise ValueError("当前授权不是该案件的卖家身份")
    return detail


def can_reply(detail):
    if detail.get("current_status") != "WAITING_DOCUMENTATION":
        return False
    due = detail.get("due_date")
    if due:
        try:
            parsed = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
            if parsed.tzinfo and parsed <= datetime.now(timezone.utc):
                return False
        except ValueError:
            return False
    return True


def inspect_case(token_id, row):
    case_id = case_identifier(row["infraction_id"])
    try:
        client, _ = store_client(token_id)
    except (KeyError, ValueError, MercadoAPIError):
        return {"mode": "browser", "message": "店铺 API 授权暂不可用，请在对应店铺浏览器中处理"}
    try:
        detail = read_case(client, case_id, row["item_id"])
        return {"mode": "api", "can_reply": can_reply(detail),
                "current_status": detail.get("current_status"),
                "reason": detail.get("reason_text") or row.get("infraction_reason") or "",
                "comment": detail.get("member_quittance") or "",
                "image_case": bool(detail.get("photos_denounced")) or detail.get("reason_id") in IMAGE_REASONS}
    except MercadoAPIError:
        return {"mode": "browser", "message": "平台 API 暂时无法读取案件，请在对应店铺浏览器中处理"}
    finally:
        client.session.close()


def validate_document(upload):
    if not upload or not upload.filename:
        return None
    extension = str(upload.filename).rsplit(".", 1)[-1].lower()
    content = upload.stream.read(MAX_DOCUMENT_BYTES + 1)
    signatures = {"pdf": content.startswith(b"%PDF-"), "png": content.startswith(b"\x89PNG\r\n\x1a\n"),
                  "jpg": content.startswith(b"\xff\xd8\xff"), "jpeg": content.startswith(b"\xff\xd8\xff")}
    if not content or len(content) > MAX_DOCUMENT_BYTES or not signatures.get(extension):
        raise ValueError("证明附件必须为 PDF、JPG 或 PNG，且不超过 5 MB")
    mime = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}[extension]
    return (f"evidence.{extension}", content, mime)


def _save_current_case(record, row, detail):
    """Update this case only; never reconcile a partial store snapshot."""
    from erp.mercadolibre_infraction_store import upsert_infraction_records
    from bit.mercado_infraction_sync import _rights_holder_state, _mysql_datetime
    upsert_infraction_records(record, [{
        "source_type": "rights_holder", "source_id": row["infraction_id"],
        "item_id": row["item_id"], "site_id": row.get("site_id"),
        "seller_id": row.get("seller_id"), "title": row.get("title"),
        "permalink": row.get("permalink"), "thumbnail_url": row.get("thumbnail_url"),
        "salesperson": row.get("salesperson"), "group_name": row.get("group_name"),
        "status": detail["current_status"], "reason_code": detail.get("reason_id") or "PPPI",
        "reason": detail.get("reason_text") or row.get("infraction_reason"),
        "rights_holder": detail.get("public_member_name") or row.get("rights_holder"),
        "occurred_at": row.get("infraction_date"), "due_at": _mysql_datetime(detail.get("due_date")),
        "raw_json": {"detail": detail}, **_rights_holder_state(detail["current_status"]),
    }])


def _confirm_uncertain_reply(client, record, row, marker, failure):
    """Read back once after an ambiguous write; never repeat the POST."""
    case_id = case_identifier(row["infraction_id"])
    state = {"state": "unknown", "at": datetime.now(timezone.utc).isoformat(), **failure}
    try:
        detail = read_case(client, case_id, row["item_id"])
        state["platform_status"] = detail.get("current_status")
        if detail.get("current_status") in {
            "DOCUMENTATION_PRESENTED", "DOCUMENTATION_APPROVED",
            "DOCUMENTATION_NOT_APPROVED", "MEMBER_NOT_RESPOND",
        }:
            state["state"] = "confirmed"
            marker.write_text(json.dumps(state), encoding="utf-8")
            try:
                _save_current_case(record, row, detail)
            except Exception:
                logging.exception("回查确认已回复，更新本地案件状态失败")
            return {"mode": "submitted", "current_status": detail["current_status"],
                    "message": "回查确认平台已有卖家回复；不重复发送"}
    except (MercadoAPIError, ValueError):
        state["verification"] = "unavailable"
    marker.write_text(json.dumps(state), encoding="utf-8")
    logging.warning("权利人回复未确认 token_id=%s case_id=%s diagnostic=%s",
                    record.get("id"), case_id, json.dumps(state))
    return {"mode": "unknown", "message": "已记录本次回复操作，已移出今日待办",
            "diagnostic": state}


def reply_case(token_id, row, text, upload=None):
    case_id = case_identifier(row["infraction_id"])
    text = str(text or "").strip()
    if not text or len(text) > 10000:
        raise ValueError("请填写回复内容，最多 10000 字")
    document = validate_document(upload)
    lock = bit_runtime_lock.InterProcessLock(f"rights-reply-{token_id}-{case_id}")
    if not lock.acquire():
        return {"mode": "busy", "message": "该案件正在提交，请稍后查看结果"}
    marker = bit_runtime_lock.RUNTIME_LOCK_DIR / f"rights-reply-{token_id}-{case_id}.json"
    client = None
    try:
        try:
            client, record = store_client(token_id)
        except (KeyError, ValueError, MercadoAPIError):
            return {"mode": "browser", "message": "店铺 API 授权暂不可用，尚未发送回复，请在店铺浏览器中处理"}
        try:
            detail = read_case(client, case_id, row["item_id"])
        except MercadoAPIError:
            return {"mode": "browser", "message": "API 无法读取案件，尚未发送回复，请通过店铺浏览器处理"}
        if not can_reply(detail):
            try:
                _save_current_case(record, row, detail)
            except Exception:
                logging.exception("更新权利人案件状态失败")
            return {"mode": "closed", "message": "平台案件已不在可回复状态，请刷新待办", "current_status": detail.get("current_status")}
        if marker.exists():
            try:
                previous = json.loads(marker.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                previous = {}
            return _confirm_uncertain_reply(client, record, row, marker, {
                key: previous[key] for key in ("http_status", "error_type") if key in previous
            })
        document_name = " "
        headers = {"Authorization": f"Bearer {client.access_token}"}
        if document:
            try:
                with admission(client.access_token):
                    response = client.session.put(
                        f"{client.BASE_URL}/moderations/pppi/case/files",
                        headers=headers, params={"case_id": case_id, "name": document[0]},
                        files={"": document}, timeout=30,
                    )
                if not response.ok:
                    return {"mode": "browser", "message": f"证明上传未成功（HTTP {response.status_code}），尚未发送回复，请在店铺浏览器处理"}
                document_name = response.json().get("file_name")
                if not isinstance(document_name, str) or not document_name.strip():
                    raise ValueError("证明上传未返回文件编号")
            except (requests.RequestException, ValueError):
                return {"mode": "browser", "message": "证明上传未确认，尚未发送回复，请在店铺浏览器处理"}
        # Write ahead of the request: a killed worker must not silently resend.
        marker.write_text(json.dumps({"state": "sending", "at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
        response_status = None
        try:
            with admission(client.access_token):
                response = client.session.post(
                    f"{client.BASE_URL}/moderations/pppi/case/{case_id}", headers=headers,
                    json={"seller_quittance": text, "document_name": document_name}, timeout=30,
                )
            response_status = response.status_code
            if 400 <= response.status_code < 500 and response.status_code != 408:
                marker.unlink(missing_ok=True)
                return {"mode": "browser", "message": f"平台拒绝 API 回复（HTTP {response.status_code}），请在店铺浏览器处理"}
            if not response.ok:
                raise requests.RequestException("平台返回异常状态")
        except requests.RequestException as exc:
            return _confirm_uncertain_reply(client, record, row, marker, {
                "http_status": response_status, "error_type": type(exc).__name__,
            })
        marker.write_text(json.dumps({"state": "accepted"}), encoding="utf-8")
        result = {"mode": "submitted", "message": "平台已接收 API 回复；待权利人审核"}
        try:
            latest = read_case(client, case_id, row["item_id"])
            _save_current_case(record, row, latest)
            result["current_status"] = latest.get("current_status")
        except Exception:
            logging.exception("回复已接收，刷新案件状态失败")
            result["message"] += "；状态同步稍后刷新"
        return result
    finally:
        if client is not None:
            client.session.close()
        lock.release()
