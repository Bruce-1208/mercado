"""从环境变量加载 Mercado Libre 同步任务所需配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """读取简单的 ``.env`` 文件，且不覆盖进程中已经存在的环境变量。

    项目没有强制依赖 ``python-dotenv``，因此这里只处理 ``KEY=VALUE``、空行
    和注释这几种本项目实际需要的格式。
    """
    file_path = Path(path)
    if not file_path.exists():
        return
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass(frozen=True)
class Settings:
    """同步任务配置。

    ``seller_id`` 是接收订单的站点子账号 ID；``listing_user_id`` 可以是 CBT
    父账号 Merchant ID，也可以是某个站点的 Seller ID。
    """

    # 店铺与认证配置。
    seller_id: str
    listing_user_id: str
    access_token: str
    refresh_token: str | None
    client_id: str | None
    client_secret: str | None

    # 本地持久化及网络配置。
    database_path: Path
    token_file: Path
    sync_interval_minutes: int = 30
    request_timeout_seconds: int = 30

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Settings":
        """从环境变量构建配置，并校验运行所必需的字段。"""
        load_dotenv(env_file)
        seller_id = os.getenv("MELI_SELLER_ID", "").strip()
        access_token = os.getenv("MELI_ACCESS_TOKEN", "").strip()
        missing = [name for name, value in (("MELI_SELLER_ID", seller_id), ("MELI_ACCESS_TOKEN", access_token)) if not value]
        if missing:
            raise ValueError("缺少环境变量: " + ", ".join(missing))
        return cls(
            seller_id=seller_id,
            listing_user_id=os.getenv("MELI_LISTING_USER_ID", seller_id).strip(),
            access_token=access_token,
            refresh_token=os.getenv("MELI_REFRESH_TOKEN") or None,
            client_id=os.getenv("MELI_CLIENT_ID") or None,
            client_secret=os.getenv("MELI_CLIENT_SECRET") or None,
            database_path=Path(os.getenv("MELI_DATABASE_PATH", "mercado_api/mercado.sqlite3")),
            token_file=Path(os.getenv("MELI_TOKEN_FILE", "mercado_api/tokens.json")),
            sync_interval_minutes=int(os.getenv("MELI_SYNC_INTERVAL_MINUTES", "30")),
            request_timeout_seconds=int(os.getenv("MELI_REQUEST_TIMEOUT_SECONDS", "30")),
        )
