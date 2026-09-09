from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    data_dir: Path = PROJECT_ROOT / ".data"
    db_path: Path = Path(os.getenv("YANDEX_DB_PATH", ".data/yandex_reseller.db"))
    headless: bool = _env_bool("YANDEX_HEADLESS", True)
    request_delay_ms: int = int(os.getenv("YANDEX_REQUEST_DELAY_MS", "1200"))
    scraper_processes: int = _env_int(
        "YANDEX_SCRAPER_PROCESSES", 6, minimum=1, maximum=12
    )
    worker_headless: bool = _env_bool("YANDEX_WORKER_HEADLESS", True)
    max_products: int = int(os.getenv("YANDEX_MAX_PRODUCTS", "500"))
    order_new_sync_seconds: int = _env_int(
        "YANDEX_NEW_ORDER_SYNC_SECONDS", 15 * 60, minimum=60, maximum=24 * 60 * 60
    )
    order_status_sync_seconds: int = _env_int(
        "YANDEX_OLD_ORDER_STATUS_SYNC_SECONDS",
        12 * 60 * 60,
        minimum=5 * 60,
        maximum=7 * 24 * 60 * 60,
    )
    order_sync_poll_seconds: int = _env_int(
        "YANDEX_ORDER_SYNC_POLL_SECONDS", 30, minimum=5, maximum=5 * 60
    )
    order_sync_retry_seconds: int = _env_int(
        "YANDEX_ORDER_SYNC_RETRY_SECONDS", 5 * 60, minimum=30, maximum=60 * 60
    )
    order_sync_enabled: bool = not _env_bool("YANDEX_ORDER_SYNC_DISABLED", False)
    locale: str = os.getenv("YANDEX_LOCALE", "ru-RU")
    timezone: str = os.getenv("YANDEX_TIMEZONE", "Europe/Moscow")
    market_base_url: str = "https://market.yandex.ru"
    seller_api_base_url: str = "https://api.partner.market.yandex.ru"
    zeshun_authorization_url_template: str = os.getenv(
        "ZESHUN_AUTHORIZATION_URL_TEMPLATE", ""
    ).strip()

    def resolved_db_path(self) -> Path:
        return self.db_path if self.db_path.is_absolute() else self.project_root / self.db_path


settings = Settings()
