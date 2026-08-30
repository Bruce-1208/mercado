from unittest import mock

from bit import bit_traffic_check as traffic_check
from bit.bit_reputation_info import MercadoAuthenticationError


class FakeLease:
    def acquire(self, timeout=0):
        return True

    def release(self):
        return None


def test_failed_browser_attach_does_not_close_someone_elses_window(monkeypatch):
    monkeypatch.setattr(traffic_check, "create_window_lease", lambda *a, **k: FakeLease())
    monkeypatch.setattr(
        traffic_check,
        "_connect_browser",
        mock.Mock(side_effect=RuntimeError("浏览器正在打开中")),
    )
    close_browser = mock.Mock()
    monkeypatch.setattr(traffic_check, "closeBrowser", close_browser)

    results = traffic_check._check_shop(
        ("window-1", "测试店铺", "", "墨西哥", "", "", ""),
        lease_wait_seconds=0,
        attempts=1,
    )

    assert results[0]["ok"] is False
    close_browser.assert_not_called()


def test_authentication_failure_is_applied_to_remaining_shop_sites(monkeypatch):
    monkeypatch.setattr(traffic_check, "create_window_lease", lambda *a, **k: FakeLease())
    monkeypatch.setattr(traffic_check, "_connect_browser", lambda *a, **k: object())
    get_visits = mock.Mock(side_effect=MercadoAuthenticationError("登录失效"))
    monkeypatch.setattr(traffic_check, "get_recent_visits_info", get_visits)
    monkeypatch.setattr(traffic_check, "closeBrowser", mock.Mock())

    results = traffic_check._check_shop(
        ("window-1", "测试店铺", "", "墨西哥，巴西，阿根廷", "", "", ""),
        lease_wait_seconds=0,
        attempts=2,
    )

    assert len(results) == 3
    assert all(result["status"] == "失败：登录失效" for result in results)
    assert get_visits.call_count == 1
