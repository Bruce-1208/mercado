from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from urllib.parse import urlsplit

from yandex.app.config import settings


class SearchRequest(BaseModel):
    keyword: str = Field(min_length=1, max_length=200)
    count: int = Field(default=200, ge=1, le=settings.max_products)

    @field_validator("keyword")
    @classmethod
    def normalize_keyword(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("关键词不能为空")
        return value


class TokenRequest(BaseModel):
    token: SecretStr

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().strip()) < 16:
            raise ValueError("token 格式不正确")
        return value


class StoreCreateRequest(TokenRequest):
    alias: str = Field(min_length=1, max_length=80)

    @field_validator("alias")
    @classmethod
    def normalize_alias(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("自定义店铺名不能为空")
        return value


class StoreUpdateRequest(BaseModel):
    alias: str = Field(min_length=1, max_length=80)

    @field_validator("alias")
    @classmethod
    def normalize_alias(cls, value: str) -> str:
        return StoreCreateRequest.normalize_alias(value)


def _normalize_http_url(value: str, *, field_name: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name}必须是完整的 http/https 链接")
    return value


class ZeshunStoreCreateRequest(BaseModel):
    alias: str = Field(min_length=1, max_length=80)
    tg_code: str = Field(min_length=1, max_length=120)
    authorization_url: str = Field(default="", max_length=4000)

    @field_validator("alias")
    @classmethod
    def normalize_alias(cls, value: str) -> str:
        return StoreCreateRequest.normalize_alias(value)

    @field_validator("tg_code")
    @classmethod
    def normalize_tg_code(cls, value: str) -> str:
        value = value.strip()
        if not value or any(character.isspace() for character in value):
            raise ValueError("TG 码不能为空且不能包含空格")
        return value

    @field_validator("authorization_url")
    @classmethod
    def normalize_authorization_url(cls, value: str) -> str:
        return _normalize_http_url(value, field_name="授权链接")


class ZeshunStoreUpdateRequest(BaseModel):
    alias: str = Field(min_length=1, max_length=80)
    authorization_url: str = Field(default="", max_length=4000)

    @field_validator("alias")
    @classmethod
    def normalize_alias(cls, value: str) -> str:
        return StoreCreateRequest.normalize_alias(value)

    @field_validator("authorization_url")
    @classmethod
    def normalize_authorization_url(cls, value: str) -> str:
        return _normalize_http_url(value, field_name="授权链接")


class ZeshunStoreAuthorizeRequest(BaseModel):
    authorized_url: str = Field(default="", max_length=8000)
    token: SecretStr | None = None

    @field_validator("authorized_url")
    @classmethod
    def normalize_authorized_url(cls, value: str) -> str:
        return _normalize_http_url(value, field_name="授权后的链接")

    @field_validator("token")
    @classmethod
    def validate_optional_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value().strip()) < 16:
            raise ValueError("token 格式不正确")
        return value

    @model_validator(mode="after")
    def require_authorization_result(self) -> "ZeshunStoreAuthorizeRequest":
        if not self.authorized_url and self.token is None:
            raise ValueError("请填写授权后的链接或 token")
        return self


class PackageDimensions(BaseModel):
    length: float = Field(gt=0, le=1000, allow_inf_nan=False)
    width: float = Field(gt=0, le=1000, allow_inf_nan=False)
    height: float = Field(gt=0, le=1000, allow_inf_nan=False)
    weight: float = Field(gt=0, le=1000, allow_inf_nan=False)


class PublishRequest(BaseModel):
    store_id: int = Field(gt=0)
    product_ids: list[int] = Field(min_length=1, max_length=500)
    price_percent: float = Field(default=200, ge=1, le=1000, allow_inf_nan=False)
    package: PackageDimensions
    initial_stock: int = Field(ge=1, le=2_000_000_000)


class OrderListRequest(BaseModel):
    store_id: int = Field(gt=0)
    statuses: list[str] = Field(default_factory=list, max_length=12)
    date_from: date | None = None
    date_to: date | None = None
    page_token: str = Field(default="", max_length=1000)
    limit: int = Field(default=50, ge=1, le=50)
    force_sync: bool = False

    @field_validator("statuses")
    @classmethod
    def normalize_statuses(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            status = str(value).strip().upper()
            if status and status not in normalized:
                normalized.append(status)
        return normalized

    @field_validator("page_token")
    @classmethod
    def normalize_page_token(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_date_range(self) -> "OrderListRequest":
        if self.date_from and self.date_to:
            if self.date_from > self.date_to:
                raise ValueError("订单开始日期不能晚于结束日期")
            if self.date_to - self.date_from > timedelta(days=29):
                raise ValueError("订单查询时间范围不能超过 30 天")
        return self


class OrderActionRequest(BaseModel):
    store_id: int = Field(gt=0)
    order_id: int = Field(gt=0)
    action: Literal["READY_TO_SHIP", "CANCEL"]


class InventoryListRequest(BaseModel):
    store_id: int = Field(gt=0)
    offer_ids: list[str] = Field(default_factory=list, max_length=500)
    archived: bool = False
    page_token: str = Field(default="", max_length=1000)
    limit: int = Field(default=100, ge=1, le=100)

    @field_validator("offer_ids")
    @classmethod
    def normalize_offer_ids(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            offer_id = str(value).strip()
            if not offer_id:
                continue
            if len(offer_id) > 255:
                raise ValueError("SKU 不能超过 255 个字符")
            if offer_id not in normalized:
                normalized.append(offer_id)
        return normalized

    @field_validator("page_token")
    @classmethod
    def normalize_page_token(cls, value: str) -> str:
        return value.strip()


class InventoryStockUpdateRequest(BaseModel):
    store_id: int = Field(gt=0)
    offer_id: str = Field(min_length=1, max_length=255)
    count: int = Field(ge=0, le=2_000_000_000)

    @field_validator("offer_id")
    @classmethod
    def normalize_offer_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("SKU 不能为空")
        return value


class ListingListRequest(BaseModel):
    store_id: int = Field(gt=0)
    offer_ids: list[str] = Field(default_factory=list, max_length=200)
    statuses: list[str] = Field(default_factory=list, max_length=10)
    page_token: str = Field(default="", max_length=1000)
    limit: int = Field(default=100, ge=1, le=200)

    @field_validator("offer_ids")
    @classmethod
    def normalize_listing_offer_ids(cls, values: list[str]) -> list[str]:
        return InventoryListRequest.normalize_offer_ids(values)

    @field_validator("statuses")
    @classmethod
    def normalize_listing_statuses(cls, values: list[str]) -> list[str]:
        allowed = {
            "PUBLISHED", "CHECKING", "DISABLED_BY_PARTNER",
            "DISABLED_AUTOMATICALLY", "REJECTED_BY_MARKET", "CREATING_CARD",
            "NO_CARD", "NO_STOCKS", "ARCHIVED", "READY_FOR_PUBLICATION",
        }
        normalized = list(
            dict.fromkeys(str(value).strip().upper() for value in values if str(value).strip())
        )
        unknown = [value for value in normalized if value not in allowed]
        if unknown:
            raise ValueError(f"不支持的链接状态：{', '.join(unknown)}")
        return normalized

    @field_validator("page_token")
    @classmethod
    def normalize_listing_page_token(cls, value: str) -> str:
        return value.strip()


class ListingPriceUpdateRequest(BaseModel):
    store_id: int = Field(gt=0)
    offer_id: str = Field(min_length=1, max_length=255)
    value: float = Field(gt=0, le=100_000_000, allow_inf_nan=False)
    currency_id: str = Field(min_length=3, max_length=3)
    discount_base: float | None = Field(
        default=None, gt=0, le=100_000_000, allow_inf_nan=False
    )

    @field_validator("offer_id")
    @classmethod
    def normalize_listing_offer_id(cls, value: str) -> str:
        return InventoryStockUpdateRequest.normalize_offer_id(value)

    @field_validator("currency_id")
    @classmethod
    def normalize_listing_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if not value.isalpha():
            raise ValueError("价格币种必须是三位字母代码")
        return value

    @model_validator(mode="after")
    def validate_listing_discount(self) -> "ListingPriceUpdateRequest":
        if self.discount_base is None:
            return self
        discount = 1 - (self.value / self.discount_base)
        if discount < 0.05 or discount > 0.99:
            raise ValueError("划线价对应的折扣必须在 5%–99% 之间")
        return self


class ListingDeleteRequest(BaseModel):
    store_id: int = Field(gt=0)
    offer_ids: list[str] = Field(min_length=1, max_length=500)

    @field_validator("offer_ids")
    @classmethod
    def normalize_listing_delete_ids(cls, values: list[str]) -> list[str]:
        return InventoryListRequest.normalize_offer_ids(values)


class ListingDimensionsUpdateRequest(BaseModel):
    store_id: int = Field(gt=0)
    offer_id: str = Field(min_length=1, max_length=255)
    package: PackageDimensions

    @field_validator("offer_id")
    @classmethod
    def normalize_listing_dimensions_offer_id(cls, value: str) -> str:
        return InventoryStockUpdateRequest.normalize_offer_id(value)


class ListingVisibilityUpdateRequest(BaseModel):
    store_id: int = Field(gt=0)
    offer_ids: list[str] = Field(min_length=1, max_length=500)
    paused: bool

    @field_validator("offer_ids")
    @classmethod
    def normalize_listing_visibility_ids(cls, values: list[str]) -> list[str]:
        return InventoryListRequest.normalize_offer_ids(values)


class ReturnListRequest(BaseModel):
    store_id: int = Field(gt=0)
    return_type: Literal["", "RETURN", "UNREDEEMED"] = ""
    statuses: list[str] = Field(default_factory=list, max_length=16)
    shipment_statuses: list[str] = Field(default_factory=list, max_length=16)
    date_from: date | None = None
    date_to: date | None = None
    page_token: str = Field(default="", max_length=1000)
    limit: int = Field(default=100, ge=1, le=100)

    @field_validator("statuses", "shipment_statuses")
    @classmethod
    def normalize_return_statuses(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value).strip().upper() for value in values if str(value).strip()))

    @field_validator("page_token")
    @classmethod
    def normalize_return_page_token(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_return_date_range(self) -> "ReturnListRequest":
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("退货开始日期不能晚于结束日期")
        return self


class ReturnItemDecisionInput(BaseModel):
    return_item_id: int = Field(gt=0)
    decision_type: Literal[
        "FAST_REFUND_MONEY", "REFUND_MONEY", "REFUND_MONEY_INCLUDING_SHIPMENT",
        "REPAIR", "REPLACE", "SEND_TO_EXAMINATION", "DECLINE_REFUND",
        "PARTIAL_MONEY_REFUND", "OTHER_DECISION",
    ]
    reason_type: Literal[
        "ISSUE_WITH_THE_PRODUCT_WAS_NOT_CONFIRMED", "MECHANICAL_DAMAGE",
        "WARRANTY_PERIOD_HAS_EXPIRED", "CONFIGURATION_OR_PACKAGING_COMPROMISED",
        "PRODUCT_APPEARANCE_COMPROMISED", "WARRANTY_TERMS_VIOLATED", "DEVICE_ACTIVATED",
    ] | None = None
    comment: str = Field(default="", max_length=1000)
    compensation_value: float | None = Field(default=None, gt=0, le=100_000_000, allow_inf_nan=False)
    currency_id: str = Field(default="RUR", min_length=3, max_length=3)

    @field_validator("currency_id")
    @classmethod
    def normalize_compensation_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if not value.isalpha():
            raise ValueError("补偿币种必须是三位字母代码")
        return value

    @field_validator("comment")
    @classmethod
    def normalize_decision_comment(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_decision_fields(self) -> "ReturnItemDecisionInput":
        if self.decision_type == "DECLINE_REFUND" and self.reason_type is None:
            raise ValueError("拒绝退款必须选择平台允许的原因")
        if self.decision_type == "PARTIAL_MONEY_REFUND" and self.compensation_value is None:
            raise ValueError("部分退款必须填写补偿金额")
        if self.decision_type != "PARTIAL_MONEY_REFUND" and self.compensation_value is not None:
            raise ValueError("只有部分退款需要填写补偿金额")
        return self


class ReturnDecisionRequest(BaseModel):
    store_id: int = Field(gt=0)
    order_id: int = Field(gt=0)
    return_id: int = Field(gt=0)
    decisions: list[ReturnItemDecisionInput] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_return_items(self) -> "ReturnDecisionRequest":
        item_ids = [item.return_item_id for item in self.decisions]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("退货商品不能重复提交决定")
        return self


class SettlementReportRequest(BaseModel):
    store_id: int = Field(gt=0)
    date_from: date
    date_to: date

    @model_validator(mode="after")
    def validate_settlement_period(self) -> "SettlementReportRequest":
        if self.date_from > self.date_to:
            raise ValueError("对账开始日期不能晚于结束日期")
        month_index = self.date_from.year * 12 + self.date_from.month - 1 + 3
        year, month_zero_based = divmod(month_index, 12)
        month = month_zero_based + 1
        latest_end = date(year, month, min(self.date_from.day, calendar.monthrange(year, month)[1]))
        if self.date_to > latest_end:
            raise ValueError("支付报表周期不能超过三个月")
        return self


class TodoListRequest(BaseModel):
    store_ids: list[int] = Field(default_factory=list, max_length=100)

    @field_validator("store_ids")
    @classmethod
    def normalize_todo_store_ids(cls, values: list[int]) -> list[int]:
        return list(dict.fromkeys(int(value) for value in values if int(value) > 0))


class ChatListRequest(BaseModel):
    store_id: int = Field(gt=0)
    statuses: list[
        Literal[
            "NEW",
            "WAITING_FOR_CUSTOMER",
            "WAITING_FOR_PARTNER",
            "WAITING_FOR_ARBITER",
            "WAITING_FOR_MARKET",
            "FINISHED",
        ]
    ] = Field(default_factory=list, max_length=6)
    context_types: list[Literal["ORDER", "RETURN", "DIRECT"]] = Field(
        default_factory=list, max_length=3
    )
    types: list[Literal["CHAT", "ARBITRAGE"]] = Field(
        default_factory=list, max_length=2
    )
    page_token: str = Field(default="", max_length=1000)
    limit: int = Field(default=20, ge=1, le=20)

    @field_validator("statuses", "context_types", "types")
    @classmethod
    def unique_chat_filters(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @field_validator("page_token")
    @classmethod
    def normalize_chat_page_token(cls, value: str) -> str:
        return value.strip()


class ChatHistoryRequest(BaseModel):
    store_id: int = Field(gt=0)
    chat_id: int = Field(gt=0)
    page_token: str = Field(default="", max_length=1000)
    message_id_from: int | None = Field(default=None, gt=0)
    limit: int = Field(default=100, ge=1, le=100)

    @field_validator("page_token")
    @classmethod
    def normalize_chat_history_page_token(cls, value: str) -> str:
        return value.strip()


class ChatReplyRequest(BaseModel):
    store_id: int = Field(gt=0)
    chat_id: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=4096)

    @field_validator("text")
    @classmethod
    def normalize_chat_reply_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("消息内容不能为空")
        return value


class ChatCreateRequest(BaseModel):
    store_id: int = Field(gt=0)
    context_type: Literal["ORDER", "RETURN"]
    context_id: int = Field(gt=0)


class FeedbackListRequest(BaseModel):
    store_id: int = Field(gt=0)
    reaction_status: Literal["ALL", "NEED_REACTION"] = "ALL"
    rating_values: list[int] = Field(default_factory=list, max_length=5)
    offer_ids: list[str] = Field(default_factory=list, max_length=20)
    page_token: str = Field(default="", max_length=1000)
    limit: int = Field(default=50, ge=1, le=50)

    @field_validator("rating_values")
    @classmethod
    def validate_rating_values(cls, values: list[int]) -> list[int]:
        normalized = list(dict.fromkeys(int(value) for value in values))
        if any(value < 1 or value > 5 for value in normalized):
            raise ValueError("评价星级必须在 1–5 之间")
        return normalized

    @field_validator("offer_ids")
    @classmethod
    def normalize_feedback_offer_ids(cls, values: list[str]) -> list[str]:
        return InventoryListRequest.normalize_offer_ids(values)

    @field_validator("page_token")
    @classmethod
    def normalize_feedback_page_token(cls, value: str) -> str:
        return value.strip()


class FeedbackReplyRequest(BaseModel):
    store_id: int = Field(gt=0)
    feedback_id: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=4096)

    @field_validator("text")
    @classmethod
    def normalize_feedback_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("回复内容不能为空")
        return value


class FeedbackSkipRequest(BaseModel):
    store_id: int = Field(gt=0)
    feedback_ids: list[int] = Field(min_length=1, max_length=50)

    @field_validator("feedback_ids")
    @classmethod
    def normalize_feedback_ids(cls, values: list[int]) -> list[int]:
        normalized = list(dict.fromkeys(int(value) for value in values))
        if any(value <= 0 for value in normalized):
            raise ValueError("评价 ID 必须大于 0")
        return normalized


class QuestionListRequest(BaseModel):
    store_id: int = Field(gt=0)
    need_answer: bool = False
    date_from: date | None = None
    date_to: date | None = None
    page_token: str = Field(default="", max_length=1000)
    limit: int = Field(default=50, ge=1, le=50)

    @field_validator("page_token")
    @classmethod
    def normalize_question_page_token(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_question_date_range(self) -> "QuestionListRequest":
        if self.date_from and self.date_to:
            if self.date_from > self.date_to:
                raise ValueError("问题开始日期不能晚于结束日期")
            if self.date_to - self.date_from > timedelta(days=30):
                raise ValueError("问题查询时间范围不能超过 31 天")
        return self


class QuestionReplyRequest(BaseModel):
    store_id: int = Field(gt=0)
    question_id: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=5000)

    @field_validator("text")
    @classmethod
    def normalize_question_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("回答内容不能为空")
        return value


class ProductRecord(BaseModel):
    id: int | None = None
    run_id: int | None = None
    source_url: str
    market_sku: int | None = None
    offer_id: str
    name: str
    description: str = ""
    vendor: str = ""
    vendor_code: str = ""
    category_name: str = ""
    market_category_id: int | None = None
    price: float | None = None
    old_price: float | None = None
    currency: str = "RUR"
    pictures: list[str] = Field(default_factory=list)
    specifications: dict[str, Any] = Field(default_factory=dict)
    seller_name: str = ""
    rating: float | None = None
    reviews_count: int | None = None
    is_foreign: bool = False
    foreign_evidence: str = ""
    raw_data: dict[str, Any] = Field(default_factory=dict)
    publish_status: str = "not_published"
    publish_message: str = ""

    @property
    def missing_publish_fields(self) -> list[str]:
        missing: list[str] = []
        for field in ("name", "vendor"):
            if not getattr(self, field):
                missing.append(field)
        picture_count = len(dict.fromkeys(self.pictures))
        if picture_count < 1:
            missing.append("pictures（至少1张）")
        usable_specifications = {
            str(key).strip(): str(value).strip()
            for key, value in self.specifications.items()
            if str(key).strip() and str(value).strip()
        }
        if not usable_specifications:
            missing.append("specifications（至少1项）")
        if not self.market_category_id:
            missing.append("marketCategoryId")
        if not self.market_sku:
            missing.append("marketSku")
        return missing
