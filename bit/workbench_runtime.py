"""Resolve the workbench's server/client role before database modules load.

The same source tree (and the same frozen executable) can run in two roles:

* ``server`` owns the database connection and serves ``/api/db/*`` routes;
* ``client`` never connects to MySQL and accesses those routes over HTTP.

Runtime settings may be supplied with command-line flags, environment variables,
or a ``workbench-runtime.json`` file next to the executable/project.  Keeping the
resolver in a stdlib-only module lets entry points call it before importing any
module that chooses a database backend at import time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping, Sequence


DEFAULT_SERVER_DB_HOST = "192.168.1.11"
DEFAULT_DB_API_BASE_URL = "https://zeshun.nat100.top"
DEFAULT_CONFIG_FILENAME = "workbench-runtime.json"

SERVER_ROLE_ALIASES = frozenset(("server", "service", "direct", "local", "mysql"))
CLIENT_ROLE_ALIASES = frozenset(("client", "api", "remote"))


@dataclass(frozen=True)
class RuntimeSettings:
    role: str
    source: str
    config_path: str = ""
    api_base_url: str = ""
    api_token: str = ""
    db_host: str = ""

    @property
    def is_server(self) -> bool:
        return self.role == "server"

    @property
    def is_client(self) -> bool:
        return self.role == "client"


def normalize_runtime_role(value: object) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized in SERVER_ROLE_ALIASES:
        return "server"
    if normalized in CLIENT_ROLE_ALIASES:
        return "client"
    raise ValueError(
        f"不支持的工作台运行角色 {value!r}；请使用 server 或 client"
    )


def _parse_runtime_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--role", "--workbench-role", dest="role")
    parser.add_argument("--runtime-config", dest="config_path")
    parser.add_argument("--api-base-url", dest="api_base_url")
    parser.add_argument("--db-host", dest="db_host")
    options, _unknown = parser.parse_known_args(list(argv))
    return options


def _candidate_config_paths(
    explicit_path: str,
    environment: Mapping[str, str],
) -> list[Path]:
    configured_path = str(
        explicit_path or environment.get("BIT_RUNTIME_CONFIG") or ""
    ).strip()
    if configured_path:
        path = Path(configured_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return [path]

    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / DEFAULT_CONFIG_FILENAME)
    candidates.extend(
        (
            Path.cwd() / DEFAULT_CONFIG_FILENAME,
            Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_FILENAME,
        )
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve())
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def _load_runtime_config(
    explicit_path: str,
    environment: Mapping[str, str],
) -> tuple[dict, str]:
    candidates = _candidate_config_paths(explicit_path, environment)
    explicitly_requested = bool(
        str(explicit_path or environment.get("BIT_RUNTIME_CONFIG") or "").strip()
    )
    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取运行配置 {path}：{exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"运行配置 {path} 的顶层内容必须是 JSON 对象")
        return payload, str(path.resolve())
    if explicitly_requested:
        raise ValueError(f"找不到指定的运行配置：{candidates[0]}")
    return {}, ""


def _first_nonempty(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _legacy_role(environment: Mapping[str, str]) -> str:
    value = _first_nonempty(
        environment.get("BIT_DB_MODE"),
        environment.get("BIT_INTERFACE_DB_MODE"),
    )
    if value:
        return normalize_runtime_role(value)
    legacy_use_api = environment.get("BIT_INTERFACE_USE_DB_API")
    if legacy_use_api is not None:
        enabled = str(legacy_use_api).strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return "client" if enabled else "server"
    return ""


def resolve_runtime_settings(
    argv: Sequence[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> RuntimeSettings:
    environment = os.environ if environment is None else environment
    options = _parse_runtime_arguments(sys.argv[1:] if argv is None else argv)
    config, config_path = _load_runtime_config(options.config_path, environment)

    role_sources = (
        (options.role, "command line"),
        (environment.get("BIT_RUNTIME_ROLE"), "BIT_RUNTIME_ROLE"),
        (config.get("role"), config_path or DEFAULT_CONFIG_FILENAME),
        (_legacy_role(environment), "legacy database mode"),
        ("server", "backward-compatible default"),
    )
    role_value = ""
    role_source = ""
    for candidate, source in role_sources:
        if str(candidate or "").strip():
            role_value = str(candidate)
            role_source = source
            break
    role = normalize_runtime_role(role_value)

    api_base_url = _first_nonempty(
        options.api_base_url,
        environment.get("BIT_DB_API_BASE_URL"),
        config.get("api_base_url"),
        DEFAULT_DB_API_BASE_URL,
    ).rstrip("/")
    api_token = _first_nonempty(
        environment.get("BIT_DB_API_TOKEN"),
        config.get("api_token"),
    )
    db_host = _first_nonempty(
        options.db_host,
        environment.get("MYSQL_HOST"),
        environment.get("DB_HOST"),
        config.get("db_host"),
        DEFAULT_SERVER_DB_HOST,
    )
    return RuntimeSettings(
        role=role,
        source=role_source,
        config_path=config_path,
        api_base_url=api_base_url,
        api_token=api_token,
        db_host=db_host,
    )


def apply_runtime_settings(
    settings: RuntimeSettings,
    environment: MutableMapping[str, str] | None = None,
) -> RuntimeSettings:
    environment = os.environ if environment is None else environment
    environment["BIT_RUNTIME_ROLE"] = settings.role
    if settings.api_token:
        environment["BIT_DB_API_TOKEN"] = settings.api_token

    if settings.is_server:
        environment["BIT_DB_MODE"] = "mysql"
        environment["BIT_INTERFACE_DB_MODE"] = "direct"
        environment["MYSQL_HOST"] = settings.db_host or DEFAULT_SERVER_DB_HOST
        environment.pop("BIT_DB_DIRECT_DISABLED", None)
    else:
        environment["BIT_DB_MODE"] = "api"
        environment["BIT_INTERFACE_DB_MODE"] = "api"
        environment["BIT_DB_API_BASE_URL"] = (
            settings.api_base_url or DEFAULT_DB_API_BASE_URL
        )
        # bit_mysql uses this as a hard safety switch if legacy code tries to
        # bypass bit_db_api while the application is running as a client.
        environment["BIT_DB_DIRECT_DISABLED"] = "1"
    return settings


def bootstrap_runtime(
    argv: Sequence[str] | None = None,
    environment: MutableMapping[str, str] | None = None,
) -> RuntimeSettings:
    environment = os.environ if environment is None else environment
    settings = resolve_runtime_settings(argv=argv, environment=environment)
    return apply_runtime_settings(settings, environment=environment)


__all__ = (
    "CLIENT_ROLE_ALIASES",
    "DEFAULT_CONFIG_FILENAME",
    "DEFAULT_DB_API_BASE_URL",
    "DEFAULT_SERVER_DB_HOST",
    "RuntimeSettings",
    "SERVER_ROLE_ALIASES",
    "apply_runtime_settings",
    "bootstrap_runtime",
    "normalize_runtime_role",
    "resolve_runtime_settings",
)
