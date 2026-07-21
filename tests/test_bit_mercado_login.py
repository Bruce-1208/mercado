import pytest
from openpyxl import Workbook

from bit import bit_mercado_login as mercado_login


def test_load_shop_login_config_uses_headers_instead_of_fixed_columns(tmp_path):
    config_path = tmp_path / "比特配置文件.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["站点", "邮箱", "账号名", "窗口ID"])
    sheet.append(["巴西", "wrong@example.com", "测试店铺", "window-other"])
    sheet.append(["墨西哥", "shop@example.com", "测试店铺", "window-1"])
    workbook.save(config_path)

    config = mercado_login.load_shop_login_config(
        "测试店铺",
        window_id="window-1",
        config_path=config_path,
    )

    assert config["email"] == "shop@example.com"
    assert config["window_id"] == "window-1"
    assert config["email_column_exists"] is True


@pytest.mark.parametrize("stage", ["email", "password", "verification", "captcha", "login"])
def test_login_detection_never_performs_automatic_login(monkeypatch, stage):
    monkeypatch.setattr(mercado_login, "is_mercado_login_page", lambda driver: True)
    monkeypatch.setattr(mercado_login, "detect_login_stage", lambda driver: stage)

    # object() 没有输入、点击或提交能力；若检测代码尝试自动登录，本测试会报错。
    result = mercado_login.ensure_mercado_login(object(), "龙凤呈祥")

    assert result["ok"] is False
    assert result["status"] == mercado_login.LOGIN_NOT_LOGGED_IN
    assert result["login_stage"] == stage
    assert "不执行任何自动登录操作" in result["message"]
    assert mercado_login.is_login_blocking_result(result["status"])


def test_command_line_accepts_shop_name():
    args = mercado_login.build_command_line_parser().parse_args(
        ["--shop", "龙凤呈祥", "--wait-seconds", "12", "--no-navigate"]
    )

    assert args.shop == "龙凤呈祥"
    assert args.wait_seconds == 12
    assert args.no_navigate is True


def test_login_check_always_uses_global_selling_home(monkeypatch):
    class Driver:
        def __init__(self):
            self.cdp_commands = []
            self.current_url = "https://www.mercadolibre.com/jms/cbt/lgz/msl/login/x/legacy-user"
            self.title = "Login"
            self.switch_to = self

        def default_content(self):
            return None

        def execute_cdp_cmd(self, command, params):
            self.cdp_commands.append((command, params))
            return {}

        def execute_script(self, script, *args):
            if "document.body" in script:
                return "Fill out your e-mail address to log in"
            return None

    driver = Driver()
    ensure_calls = []
    monkeypatch.setattr(mercado_login.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(mercado_login, "is_mercado_login_page", lambda value: True)
    monkeypatch.setattr(
        mercado_login,
        "ensure_mercado_login",
        lambda *args, **kwargs: ensure_calls.append((args, kwargs)) or {
            "ok": False,
            "status": mercado_login.LOGIN_NOT_LOGGED_IN,
            "message": mercado_login.LOGIN_NOT_LOGGED_IN,
        },
    )

    result = mercado_login.ensure_mercado_login_from_home(
        driver,
        "龙凤呈祥",
        window_id="window-1",
    )

    assert driver.cdp_commands[0] == (
        "Page.navigate",
        {"url": "https://global-selling.mercadolibre.com/"},
    )
    assert all(command != "Page.stopLoading" for command, _ in driver.cdp_commands)
    assert len(ensure_calls) == 1
    assert result["login_check_url"] == mercado_login.MERCADO_HOME_URL
    assert result["login_detected_before"] is True
