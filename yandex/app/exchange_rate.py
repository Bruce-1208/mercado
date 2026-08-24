from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

import httpx


CBR_DAILY_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
CACHE_TTL = timedelta(hours=6)


class ExchangeRateError(RuntimeError):
    pass


@dataclass(slots=True)
class ExchangeRateQuote:
    rate: float
    effective_date: str
    fetched_at: str

    def public_dict(self) -> dict[str, str | float]:
        return {
            "base": "RUR",
            "quote": "CNY",
            "rate": self.rate,
            "effective_date": self.effective_date,
            "fetched_at": self.fetched_at,
            "source": "俄罗斯央行",
            "source_url": CBR_DAILY_URL,
        }


def parse_cbr_daily_xml(content: bytes) -> tuple[float, str]:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ExchangeRateError("俄罗斯央行汇率数据格式异常") from exc
    for valute in root.findall("Valute"):
        if (valute.findtext("CharCode") or "").strip().upper() != "CNY":
            continue
        try:
            nominal = Decimal((valute.findtext("Nominal") or "").replace(",", "."))
            rubles = Decimal((valute.findtext("Value") or "").replace(",", "."))
            if nominal <= 0 or rubles <= 0:
                raise InvalidOperation
            # CBR publishes RUB for the stated CNY nominal; we need CNY for one RUB.
            rub_to_cny = nominal / rubles
        except (InvalidOperation, ZeroDivisionError) as exc:
            raise ExchangeRateError("俄罗斯央行人民币汇率数值无效") from exc
        return float(rub_to_cny), str(root.attrib.get("Date") or "")
    raise ExchangeRateError("俄罗斯央行汇率数据中没有找到人民币 CNY")


class ExchangeRateService:
    def __init__(self) -> None:
        self._cached: ExchangeRateQuote | None = None
        self._cached_at: datetime | None = None
        self._lock = asyncio.Lock()

    async def get_rub_to_cny(self, *, force_refresh: bool = False) -> ExchangeRateQuote:
        now = datetime.now(UTC)
        if (
            not force_refresh
            and self._cached
            and self._cached_at
            and now - self._cached_at < CACHE_TTL
        ):
            return self._cached
        async with self._lock:
            now = datetime.now(UTC)
            if (
                not force_refresh
                and self._cached
                and self._cached_at
                and now - self._cached_at < CACHE_TTL
            ):
                return self._cached
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(12.0),
                    follow_redirects=True,
                    headers={"User-Agent": "YandexResellerAssistant/0.3"},
                ) as client:
                    response = await client.get(CBR_DAILY_URL)
                    response.raise_for_status()
                rate, effective_date = parse_cbr_daily_xml(response.content)
            except ExchangeRateError:
                raise
            except (httpx.HTTPError, OSError) as exc:
                if self._cached:
                    return self._cached
                raise ExchangeRateError("无法获取俄罗斯央行 RUB/CNY 汇率，请稍后重试") from exc
            quote = ExchangeRateQuote(
                rate=rate,
                effective_date=effective_date,
                fetched_at=now.isoformat(timespec="seconds"),
            )
            self._cached = quote
            self._cached_at = now
            return quote


exchange_rate_service = ExchangeRateService()
