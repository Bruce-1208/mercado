"""Mercado Libre Global Selling 售前、售后和投诉通信接口。

接口路径与字段依据 Global Selling 官方文档实现。这个模块只负责 HTTP、
参数校验和分页，不持久化 access token；调用方可通过 ``refresh_access_token``
回调接入自己的一次性 refresh token 存储。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import date
from typing import Any

import requests


LOGGER = logging.getLogger(__name__)
RefreshAccessToken = Callable[[], str | Mapping[str, Any]]


class MercadoCommunicationError(RuntimeError):
    """美客多通信接口失败，保留 HTTP 状态码和服务端响应。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload

    @property
    def model_6_restricted(self) -> bool:
        """当前错误是否为官方声明的 CBT Model 6 接口限制。"""
        if self.status_code != 403:
            return False
        text = str(self)
        if isinstance(self.payload, Mapping):
            text = " ".join(
                (text, str(self.payload.get("message") or ""), str(self.payload.get("error") or ""))
            )
        return "model 6" in text.casefold() or "forbidden for cbt" in text.casefold()

    @property
    def claims_1_restricted(self) -> bool:
        """当前错误是否表示资源只能通过旧版 Claims 1.0 接口读取。"""
        text = str(self)
        if self.payload is not None:
            text = " ".join((text, str(self.payload)))
        return "claims 1.0" in text.casefold()


def _required_identifier(value: Any, label: str) -> str:
    identifier = str(value or "").strip()
    if not identifier:
        raise ValueError(f"缺少{label}")
    # 这里的三个资源 ID 均为数字。拒绝斜杠，避免调用方把任意路径拼进 API。
    if not identifier.isdigit():
        raise ValueError(f"{label}格式错误")
    return identifier


def _required_text(value: Any, label: str, *, max_length: int | None = None) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"请输入{label}")
    if max_length is not None and len(text) > max_length:
        raise ValueError(f"{label}不能超过 {max_length} 个字符")
    return text


def _bounded_int(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是整数") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"{label}必须在 {minimum} 到 {maximum} 之间")
    return number


class MercadoCommunicationsClient:
    """Global Selling Questions、Messages 和 Claims 客户端。"""

    BASE_URL = "https://api.mercadolibre.com"
    QUESTION_STATUSES = frozenset((
        "UNANSWERED",
        "ANSWERED",
        "CLOSED_UNANSWERED",
        "UNDER_REVIEW",
        "BANNED",
        "DELETED",
        "DISABLED",
    ))
    QUESTION_SORT_FIELDS = frozenset((
        "item_id",
        "from_id",
        "date_created",
        "seller_id",
    ))
    CLAIM_STATUSES = frozenset(("opened", "closed"))
    CLAIM_TYPES = frozenset((
        "mediations",
        "fulfillment",
        "returns",
        "ml_case",
        "cancel_sale",
        "cancel_purchase",
        "change",
        "service",
    ))
    CLAIM_STAGES = frozenset(("claim", "dispute", "recontact", "stale", "none"))
    CLAIM_RECEIVER_ROLES = frozenset(("complainant", "respondent", "mediator"))

    def __init__(
        self,
        access_token: str,
        *,
        refresh_access_token: RefreshAccessToken | None = None,
        session: requests.Session | None = None,
        timeout: int = 30,
        max_attempts: int = 4,
    ) -> None:
        token = str(access_token or "").strip()
        if not token:
            raise ValueError("缺少 Mercado Libre Access Token")
        self.access_token = token
        self.refresh_access_token = refresh_access_token
        self.session = session or requests.Session()
        self.timeout = max(1, int(timeout))
        self.max_attempts = max(1, int(max_attempts))

    @staticmethod
    def _response_payload(response: requests.Response) -> Any:
        content = getattr(response, "content", b"") or b""
        text = str(getattr(response, "text", "") or "")
        if not content and not text:
            return {}
        try:
            return response.json()
        except (TypeError, ValueError):
            return text

    @staticmethod
    def _error_message(payload: Any) -> str:
        if isinstance(payload, Mapping):
            message = payload.get("message") or payload.get("error") or "请求失败"
            cause = payload.get("cause")
            return f"{message}; cause={cause}" if cause else str(message)
        return str(payload or "请求失败")

    def _refresh(self) -> None:
        if self.refresh_access_token is None:
            raise MercadoCommunicationError("Access Token 已失效，请重新授权", status_code=401)
        refreshed = self.refresh_access_token()
        if isinstance(refreshed, Mapping):
            refreshed = refreshed.get("access_token")
        token = str(refreshed or "").strip()
        if not token:
            raise MercadoCommunicationError("Token 刷新成功，但没有返回新的 Access Token", status_code=401)
        self.access_token = token

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        files: Mapping[str, Any] | None = None,
    ) -> Any:
        """发送认证请求。

        401 会刷新一次 Token；只有幂等读取/删除请求会在网络错误、429 或 5xx
        后重试，避免自动重发 POST 造成重复消息或重复投诉动作。
        """
        request_method = str(method).upper()
        retryable_method = request_method in ("GET", "HEAD", "OPTIONS", "DELETE")
        url = path if path.startswith(("https://", "http://")) else f"{self.BASE_URL}{path}"
        refreshed = False
        for attempt in range(self.max_attempts):
            try:
                response = self.session.request(
                    request_method,
                    url,
                    params=dict(params or {}),
                    json=json,
                    files=files,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {self.access_token}",
                    },
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if retryable_method and attempt + 1 < self.max_attempts:
                    delay = min(2**attempt, 8)
                    LOGGER.warning("Mercado 通信接口网络中断，%s 秒后重试：%s", delay, exc)
                    time.sleep(delay)
                    continue
                raise MercadoCommunicationError(f"无法连接 Mercado Libre：{exc}") from exc

            if response.status_code == 401 and not refreshed and self.refresh_access_token is not None:
                self._refresh()
                refreshed = True
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if retryable_method and attempt + 1 < self.max_attempts:
                    try:
                        delay = min(float(response.headers.get("Retry-After", 2**attempt)), 30)
                    except (TypeError, ValueError):
                        delay = min(2**attempt, 30)
                    LOGGER.warning(
                        "Mercado 通信接口暂时不可用（%s），%.1f 秒后重试",
                        response.status_code,
                        delay,
                    )
                    time.sleep(delay)
                    continue

            payload = self._response_payload(response)
            if not response.ok:
                message = self._error_message(payload)
                if response.status_code == 403 and (
                    "model 6" in message.casefold() or "forbidden for cbt" in message.casefold()
                ):
                    message = "该店铺属于 CBT Model 6，官方未开放售前/售后消息或投诉接口"
                raise MercadoCommunicationError(
                    f"{request_method} {path} 失败（HTTP {response.status_code}）：{message}",
                    status_code=response.status_code,
                    payload=payload,
                )
            return payload

        raise MercadoCommunicationError(f"{request_method} {path} 多次重试后仍失败")

    # 售前 Questions & Answers
    def search_questions(
        self,
        *,
        seller_id: Any = None,
        item_id: Any = None,
        user_id: Any = None,
        status: str | None = None,
        sort_fields: str | Iterable[str] | None = None,
        sort_types: str = "DESC",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """按卖家、商品或用户搜索售前问题。"""
        params: dict[str, Any] = {
            "limit": _bounded_int(limit, "每页数量", minimum=1, maximum=100),
            "offset": _bounded_int(offset, "偏移量", minimum=0, maximum=1000),
        }
        # 官方搜索参数使用 item 和 from；方法参数保留更清楚的 item_id/user_id。
        identifiers = {"seller_id": seller_id, "item": item_id, "from": user_id}
        for key, value in identifiers.items():
            text = str(value or "").strip()
            if text:
                params[key] = text
        if not any(key in params for key in identifiers):
            raise ValueError("seller_id、item_id、user_id 至少填写一个")
        if status:
            normalized_status = str(status).strip().upper()
            if normalized_status not in self.QUESTION_STATUSES:
                raise ValueError("不支持的售前问题状态")
            params["status"] = normalized_status
        if sort_fields:
            if isinstance(sort_fields, str):
                fields = [value.strip() for value in sort_fields.split(",") if value.strip()]
            else:
                fields = [str(value).strip() for value in sort_fields if str(value).strip()]
            if not fields or any(field not in self.QUESTION_SORT_FIELDS for field in fields):
                raise ValueError("不支持的售前问题排序字段")
            normalized_sort_type = str(sort_types or "DESC").strip().upper()
            if normalized_sort_type not in ("ASC", "DESC"):
                raise ValueError("售前问题排序方向只支持 ASC 或 DESC")
            params["sort_fields"] = ",".join(fields)
            params["sort_types"] = normalized_sort_type
        result = self.request("GET", "/marketplace/questions/search", params=params)
        if not isinstance(result, dict):
            raise MercadoCommunicationError("售前问题接口返回格式错误", payload=result)
        return result

    def iter_questions(self, **filters: Any) -> Iterator[dict[str, Any]]:
        """自动分页遍历符合条件的售前问题。"""
        offset = max(0, int(filters.pop("offset", 0) or 0))
        limit = min(100, max(1, int(filters.pop("limit", 100) or 100)))
        while True:
            page = self.search_questions(offset=offset, limit=limit, **filters)
            questions = list(page.get("questions") or [])
            yield from (question for question in questions if isinstance(question, dict))
            offset += len(questions)
            total = int(page.get("total") or offset)
            if not questions or offset >= total:
                return

    def get_question(self, question_id: Any) -> dict[str, Any]:
        question = _required_identifier(question_id, "问题 ID")
        result = self.request("GET", f"/marketplace/questions/{question}")
        if not isinstance(result, dict):
            raise MercadoCommunicationError("售前问题详情返回格式错误", payload=result)
        return result

    def answer_question(
        self,
        question_id: Any,
        text: str,
        *,
        text_translated: str | None = None,
    ) -> dict[str, Any]:
        """回答售前问题；官方限制正文最多 2,000 字符。"""
        payload: dict[str, Any] = {
            "question_id": int(_required_identifier(question_id, "问题 ID")),
            "text": _required_text(text, "回复内容", max_length=2000),
        }
        if str(text_translated or "").strip():
            payload["text_translated"] = _required_text(
                text_translated, "翻译内容", max_length=2000
            )
        result = self.request("POST", "/marketplace/answers", json=payload)
        if not isinstance(result, dict):
            raise MercadoCommunicationError("售前回复接口返回格式错误", payload=result)
        return result

    def delete_question(self, question_id: Any) -> Any:
        question = _required_identifier(question_id, "问题 ID")
        return self.request("DELETE", f"/marketplace/questions/{question}")

    # 普通售后 Messages
    def get_post_sale_messages(
        self,
        pack_id: Any,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        pack = _required_identifier(pack_id, "Pack ID")
        result = self.request(
            "GET",
            f"/marketplace/messages/packs/{pack}",
            params={
                "limit": _bounded_int(limit, "每页数量", minimum=1, maximum=100),
                "offset": _bounded_int(offset, "偏移量", minimum=0, maximum=1_000_000),
            },
        )
        if not isinstance(result, dict):
            raise MercadoCommunicationError("售后消息接口返回格式错误", payload=result)
        return result

    def get_unread_post_sale_messages(
        self,
        user_id: Any,
        *,
        role: str = "seller",
    ) -> dict[str, Any]:
        seller = _required_identifier(user_id, "Seller ID")
        normalized_role = str(role or "seller").strip().lower()
        if normalized_role not in ("seller", "buyer"):
            raise ValueError("role 只支持 seller 或 buyer")
        result = self.request(
            "GET",
            "/marketplace/messages/unread",
            params={"role": normalized_role, "tag": "post_sale", "user_id": seller},
        )
        if not isinstance(result, dict):
            raise MercadoCommunicationError("未读售后消息接口返回格式错误", payload=result)
        return result

    def get_post_sale_messages_for_seller(
        self,
        pack_id: Any,
        seller_id: Any,
        *,
        limit: int = 50,
        offset: int = 0,
        mark_as_read: bool = False,
    ) -> dict[str, Any]:
        """读取卖家 Pack 会话，并显式控制是否标记为已读。"""
        pack = _required_identifier(pack_id, "Pack ID")
        seller = _required_identifier(seller_id, "Seller ID")
        result = self.request(
            "GET",
            f"/messages/packs/{pack}/sellers/{seller}",
            params={
                "limit": _bounded_int(limit, "每页数量", minimum=1, maximum=100),
                "offset": _bounded_int(offset, "偏移量", minimum=0, maximum=1_000_000),
                "tag": "post_sale",
                "mark_as_read": "true" if mark_as_read else "false",
            },
        )
        if not isinstance(result, dict):
            raise MercadoCommunicationError("售后消息接口返回格式错误", payload=result)
        return result

    def send_post_sale_message(
        self,
        pack_id: Any,
        text: str,
        *,
        text_translated: str | None = None,
        attachments: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        pack = _required_identifier(pack_id, "Pack ID")
        payload: dict[str, Any] = {"text": _required_text(text, "消息内容")}
        if str(text_translated or "").strip():
            payload["text_translated"] = _required_text(text_translated, "翻译内容")
        attachment_ids = [str(value).strip() for value in attachments or () if str(value).strip()]
        if len(attachment_ids) > 25:
            raise ValueError("每条售后消息最多包含 25 个附件")
        if attachment_ids:
            payload["attachments"] = attachment_ids
        result = self.request("POST", f"/marketplace/messages/packs/{pack}", json=payload)
        if not isinstance(result, dict):
            raise MercadoCommunicationError("发送售后消息的返回格式错误", payload=result)
        return result

    def upload_post_sale_attachment(
        self,
        site_id: str,
        file_name: str,
        file_content: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        site = str(site_id or "").strip().upper()
        if not site or site == "CBT":
            raise ValueError("附件必须使用买家所在远程站点 SITE_ID，不能使用 CBT")
        if len(file_content or b"") > 25 * 1024 * 1024:
            raise ValueError("售后附件不能超过 25 MB")
        result = self.request(
            "POST",
            "/marketplace/messages/attachments",
            params={"site_id": site},
            files={"file": (str(file_name), file_content, str(content_type))},
        )
        if not isinstance(result, dict) or not result.get("id"):
            raise MercadoCommunicationError("售后附件接口未返回附件 ID", payload=result)
        return result

    # 投诉 Claims
    def search_claims(
        self,
        user_id: Any,
        *,
        status: str | None = "opened",
        stage: str | None = None,
        claim_type: str | None = None,
        claim_id: Any = None,
        order_id: Any = None,
        pack_id: Any = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "last_updated:desc",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "user_id": _required_identifier(user_id, "Seller ID"),
            "limit": _bounded_int(limit, "每页数量", minimum=1, maximum=100),
            "offset": _bounded_int(offset, "偏移量", minimum=0, maximum=1_000_000),
            "sort": str(sort or "last_updated:desc").strip(),
        }
        if status:
            normalized_status = str(status).strip().lower()
            if normalized_status not in self.CLAIM_STATUSES:
                raise ValueError("投诉状态只支持 opened 或 closed")
            params["status"] = normalized_status
        if stage:
            normalized_stage = str(stage).strip().lower()
            if normalized_stage not in self.CLAIM_STAGES:
                raise ValueError("不支持的投诉阶段")
            params["stage"] = normalized_stage
        if claim_type:
            normalized_type = str(claim_type).strip().lower()
            if normalized_type not in self.CLAIM_TYPES:
                raise ValueError("不支持的索赔类型")
            params["type"] = normalized_type
        if str(claim_id or "").strip():
            params["id"] = _required_identifier(claim_id, "索赔 ID")
        if str(order_id or "").strip():
            params["order_id"] = _required_identifier(order_id, "订单 ID")
        if str(pack_id or "").strip():
            params["pack_id"] = _required_identifier(pack_id, "Pack ID")
        range_parts: list[str] = []
        if str(date_from or "").strip():
            start = date.fromisoformat(str(date_from).strip())
            range_parts.append(f"after:{start.isoformat()}T00:00:00.000+00:00")
        if str(date_to or "").strip():
            end = date.fromisoformat(str(date_to).strip())
            range_parts.append(f"before:{end.isoformat()}T23:59:59.999+00:00")
        if date_from and date_to and date.fromisoformat(str(date_from).strip()) > date.fromisoformat(str(date_to).strip()):
            raise ValueError("索赔起始日期不能晚于截止日期")
        if range_parts:
            params["range"] = f"date_created:{','.join(range_parts)}"
        result = self.request("GET", "/marketplace/v2/claims/search", params=params)
        if not isinstance(result, dict):
            raise MercadoCommunicationError("投诉搜索接口返回格式错误", payload=result)
        return result

    def _get_claim_compatible(self, claim_id: Any) -> tuple[dict[str, Any], bool]:
        claim = _required_identifier(claim_id, "投诉 ID")
        try:
            result = self.request("GET", f"/marketplace/v2/claims/{claim}")
            legacy = False
        except MercadoCommunicationError as exc:
            if not exc.claims_1_restricted:
                raise
            result = self.request("GET", f"/post-purchase/v1/claims/{claim}")
            legacy = True
        if not isinstance(result, dict):
            raise MercadoCommunicationError("投诉详情接口返回格式错误", payload=result)
        return result, legacy

    def get_claim(self, claim_id: Any) -> dict[str, Any]:
        return self._get_claim_compatible(claim_id)[0]

    def _get_claim_detail_compatible(
        self, claim_id: Any
    ) -> tuple[dict[str, Any], bool]:
        claim = _required_identifier(claim_id, "投诉 ID")
        try:
            result = self.request("GET", f"/marketplace/v2/claims/{claim}/detail")
            legacy = False
        except MercadoCommunicationError as exc:
            if not exc.claims_1_restricted:
                raise
            result = self.request(
                "GET", f"/post-purchase/v1/claims/{claim}/detail"
            )
            legacy = True
        if not isinstance(result, dict):
            raise MercadoCommunicationError("投诉处理说明返回格式错误", payload=result)
        return result, legacy

    def get_claim_detail(self, claim_id: Any) -> dict[str, Any]:
        return self._get_claim_detail_compatible(claim_id)[0]

    def get_claim_reason(self, reason_id: Any) -> dict[str, Any]:
        reason = str(reason_id or "").strip()
        if not reason or not reason.replace("_", "").isalnum():
            raise ValueError("投诉原因 ID 格式错误")
        result = self.request("GET", f"/marketplace/v2/claims/reasons/{reason}")
        if not isinstance(result, dict):
            raise MercadoCommunicationError("投诉原因接口返回格式错误", payload=result)
        return result

    def _get_claim_messages_compatible(
        self, claim_id: Any
    ) -> tuple[list[dict[str, Any]], bool]:
        claim = _required_identifier(claim_id, "投诉 ID")
        try:
            result = self.request("GET", f"/marketplace/v2/claims/{claim}/messages")
            legacy = False
        except MercadoCommunicationError as exc:
            if not exc.claims_1_restricted:
                raise
            result = self.request(
                "GET", f"/post-purchase/v1/claims/{claim}/messages"
            )
            legacy = True
        if not isinstance(result, list):
            raise MercadoCommunicationError("投诉消息接口返回格式错误", payload=result)
        return [message for message in result if isinstance(message, dict)], legacy

    def get_claim_messages(self, claim_id: Any) -> list[dict[str, Any]]:
        return self._get_claim_messages_compatible(claim_id)[0]

    def get_claim_affects_reputation(self, claim_id: Any) -> dict[str, Any]:
        claim = _required_identifier(claim_id, "投诉 ID")
        result = self.request("GET", f"/marketplace/v2/claims/{claim}/affects-reputation")
        if not isinstance(result, dict):
            raise MercadoCommunicationError("投诉声誉影响接口返回格式错误", payload=result)
        return result

    def get_claim_expected_resolutions(self, claim_id: Any) -> list[dict[str, Any]]:
        claim = _required_identifier(claim_id, "投诉 ID")
        result = self.request(
            "GET", f"/marketplace/v2/claims/{claim}/expected-resolutions"
        )
        if not isinstance(result, list):
            raise MercadoCommunicationError("投诉期望方案接口返回格式错误", payload=result)
        return [resolution for resolution in result if isinstance(resolution, dict)]

    def get_claim_bundle(self, claim_id: Any) -> dict[str, Any]:
        """返回工作台展示投诉所需的详情、处理说明、消息及声誉影响。"""
        claim, claim_legacy = self._get_claim_compatible(claim_id)
        reason_id = claim.get("reason_id")
        resource_errors: dict[str, str] = {}
        used_legacy = claim_legacy

        def optional_resource(name: str, default: Any, loader: Callable[[], Any]) -> Any:
            nonlocal used_legacy
            try:
                value = loader()
                if (
                    isinstance(value, tuple)
                    and len(value) == 2
                    and isinstance(value[1], bool)
                ):
                    used_legacy = used_legacy or value[1]
                    return value[0]
                return value
            except MercadoCommunicationError as exc:
                resource_errors[name] = str(exc)
                return default

        detail = optional_resource(
            "detail", {}, lambda: self._get_claim_detail_compatible(claim_id)
        )
        reason = (
            optional_resource("reason", {}, lambda: self.get_claim_reason(reason_id))
            if reason_id
            else {}
        )
        messages = optional_resource(
            "messages", [], lambda: self._get_claim_messages_compatible(claim_id)
        )
        affects_reputation = optional_resource(
            "affects_reputation",
            {},
            lambda: self.get_claim_affects_reputation(claim_id),
        )
        expected_resolutions = optional_resource(
            "expected_resolutions",
            [],
            lambda: self.get_claim_expected_resolutions(claim_id),
        )
        return {
            "claim": claim,
            "detail": detail,
            "reason": reason,
            "messages": messages,
            "affects_reputation": affects_reputation,
            "expected_resolutions": expected_resolutions,
            "api_version": "claims_1" if used_legacy else "claims_2",
            "resource_errors": resource_errors,
        }

    def send_claim_message(
        self,
        claim_id: Any,
        message: str,
        *,
        receiver_role: str,
        attachments: Iterable[str] | None = None,
    ) -> Any:
        claim = _required_identifier(claim_id, "投诉 ID")
        role = str(receiver_role or "").strip().lower()
        if role not in self.CLAIM_RECEIVER_ROLES:
            raise ValueError("接收方只支持 complainant、respondent 或 mediator")
        payload = {
            "receiver_role": role,
            "message": _required_text(message, "投诉回复内容"),
            "attachments": [
                str(value).strip() for value in attachments or () if str(value).strip()
            ],
        }
        try:
            return self.request(
                "POST",
                f"/marketplace/v2/claims/{claim}/actions/send-message",
                json=payload,
            )
        except MercadoCommunicationError as exc:
            if not exc.claims_1_restricted:
                raise
            return self.request(
                "POST",
                f"/post-purchase/v1/claims/{claim}/messages",
                json=payload,
            )

    def upload_claim_attachment(
        self,
        claim_id: Any,
        file_name: str,
        file_content: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        claim = _required_identifier(claim_id, "投诉 ID")
        if len(file_content or b"") > 5 * 1024 * 1024:
            raise ValueError("投诉附件不能超过 5 MB")
        normalized_name = str(file_name or "").strip()
        if not normalized_name or len(normalized_name) > 125:
            raise ValueError("投诉附件名不能为空且不能超过 125 个字符")
        result = self.request(
            "POST",
            f"/marketplace/v2/claims/{claim}/attachments",
            files={"file": (normalized_name, file_content, str(content_type))},
        )
        if not isinstance(result, dict) or not result.get("file_name"):
            raise MercadoCommunicationError("投诉附件接口未返回文件名", payload=result)
        return result
