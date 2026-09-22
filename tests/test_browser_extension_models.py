import json

import pytest

from bit import browser_extension_models


def test_model_api_keys_are_encrypted_and_never_returned(tmp_path):
    path = tmp_path / "models.json"

    public = browser_extension_models.save_settings(
        7,
        {
            "deepseek_api_key": "deepseek-secret",
            "dashscope_api_key": "dashscope-secret",
        },
        "workbench-secret",
        path,
    )

    raw = path.read_text(encoding="utf-8")
    assert "deepseek-secret" not in raw
    assert "dashscope-secret" not in raw
    assert public["deepseek_configured"] is True
    assert public["dashscope_configured"] is True
    assert "api_key" not in json.dumps(public)
    assert browser_extension_models.get_api_key(
        7, "deepseek", "workbench-secret", path
    ) == "deepseek-secret"
    assert browser_extension_models.get_api_key(
        7, "dashscope", "workbench-secret", path
    ) == "dashscope-secret"


def test_blank_model_key_preserves_existing_value(tmp_path):
    path = tmp_path / "models.json"
    browser_extension_models.save_settings(
        7, {"deepseek_api_key": "first-secret"}, "workbench-secret", path
    )

    browser_extension_models.save_settings(
        7,
        {"deepseek_api_key": "", "dashscope_api_key": "second-secret"},
        "workbench-secret",
        path,
    )

    assert browser_extension_models.get_api_key(
        7, "deepseek", "workbench-secret", path
    ) == "first-secret"
    assert browser_extension_models.get_api_key(
        7, "dashscope", "workbench-secret", path
    ) == "second-secret"


def test_model_key_requires_matching_server_secret(tmp_path):
    path = tmp_path / "models.json"
    browser_extension_models.save_settings(
        7, {"deepseek_api_key": "secret"}, "workbench-secret", path
    )

    with pytest.raises(ValueError, match="无法解密"):
        browser_extension_models.get_api_key(7, "deepseek", "other-secret", path)
