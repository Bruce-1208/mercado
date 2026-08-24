from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv


PACKAGE_ROOT = Path(__file__).resolve().parent


def main() -> None:
    env_file = PACKAGE_ROOT / ".env"
    if env_file.exists():
        load_dotenv(env_file)
    uvicorn.run(
        "yandex.app.main:app",
        host=os.getenv("YANDEX_HOST", "127.0.0.1"),
        port=int(os.getenv("YANDEX_PORT", "8000")),
        env_file=str(env_file) if env_file.exists() else None,
    )


if __name__ == "__main__":
    main()
