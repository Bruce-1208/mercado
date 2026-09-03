import json

import pytest

from bit.workbench_runtime import (
    DEFAULT_SERVER_DB_HOST,
    RuntimeSettings,
    apply_runtime_settings,
    normalize_runtime_role,
    resolve_runtime_settings,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("server", "server"),
        ("mysql", "server"),
        ("direct", "server"),
        ("client", "client"),
        ("api", "client"),
        ("remote", "client"),
    ),
)
def test_normalize_runtime_role(value, expected):
    assert normalize_runtime_role(value) == expected


def test_invalid_runtime_role_is_rejected():
    with pytest.raises(ValueError, match="server 或 client"):
        normalize_runtime_role("hybrid")


def test_command_line_can_temporarily_override_persistent_role(tmp_path):
    config_path = tmp_path / "workbench-runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "role": "server",
                "db_host": DEFAULT_SERVER_DB_HOST,
                "api_base_url": "http://from-config:5000",
            }
        ),
        encoding="utf-8",
    )

    settings = resolve_runtime_settings(
        argv=[
            "--runtime-config",
            str(config_path),
            "--role",
            "client",
            "--api-base-url",
            "http://temporary-server:5000/",
        ],
        environment={},
    )

    assert settings.role == "client"
    assert settings.source == "command line"
    assert settings.api_base_url == "http://temporary-server:5000"


def test_environment_role_overrides_config_role(tmp_path):
    config_path = tmp_path / "workbench-runtime.json"
    config_path.write_text('{"role": "server"}', encoding="utf-8")

    settings = resolve_runtime_settings(
        argv=["--runtime-config", str(config_path)],
        environment={"BIT_RUNTIME_ROLE": "client"},
    )

    assert settings.role == "client"
    assert settings.source == "BIT_RUNTIME_ROLE"


def test_apply_client_role_forces_api_and_disables_direct_database():
    environment = {
        "BIT_DB_MODE": "mysql",
        "BIT_INTERFACE_DB_MODE": "direct",
    }
    settings = RuntimeSettings(
        role="client",
        source="test",
        api_base_url="http://server:5000",
        api_token="shared-token",
    )

    apply_runtime_settings(settings, environment)

    assert environment["BIT_RUNTIME_ROLE"] == "client"
    assert environment["BIT_DB_MODE"] == "api"
    assert environment["BIT_INTERFACE_DB_MODE"] == "api"
    assert environment["BIT_DB_API_BASE_URL"] == "http://server:5000"
    assert environment["BIT_DB_API_TOKEN"] == "shared-token"
    assert environment["BIT_DB_DIRECT_DISABLED"] == "1"


def test_apply_server_role_forces_direct_mysql_default_host():
    environment = {"BIT_DB_DIRECT_DISABLED": "1"}
    settings = RuntimeSettings(
        role="server",
        source="test",
        db_host=DEFAULT_SERVER_DB_HOST,
    )

    apply_runtime_settings(settings, environment)

    assert environment["BIT_RUNTIME_ROLE"] == "server"
    assert environment["BIT_DB_MODE"] == "mysql"
    assert environment["BIT_INTERFACE_DB_MODE"] == "direct"
    assert environment["MYSQL_HOST"] == "192.168.1.11"
    assert "BIT_DB_DIRECT_DISABLED" not in environment


def test_missing_explicit_runtime_config_is_rejected(tmp_path):
    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError, match="找不到指定的运行配置"):
        resolve_runtime_settings(
            argv=["--runtime-config", str(missing)],
            environment={},
        )
