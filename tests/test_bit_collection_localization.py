import unittest
from unittest import mock

from bit import bit_reputation_info as reputation
from bit_playwright import bit_infractions_info as infractions


class FakeSeleniumDriver:
    def __init__(self, cards):
        self.cards = cards

    def execute_script(self, _script, *_args):
        return self.cards


class FakePlaywrightLocator:
    def __init__(self, text="", count=0):
        self._text = text
        self._count = count

    def inner_text(self, timeout=0):
        return self._text

    def count(self):
        return self._count


class FakePlaywrightPage:
    def __init__(self, url, body, structure_count=0):
        self.url = url
        self.body = body
        self.structure_count = structure_count

    def locator(self, selector):
        if selector == "body":
            return FakePlaywrightLocator(text=self.body)
        return FakePlaywrightLocator(count=self.structure_count)


class ReputationLocalizationTests(unittest.TestCase):
    def test_chinese_metric_aliases(self):
        self.assertEqual(reputation._metric_kind_from_title("买家投诉"), "complaints")
        self.assertEqual(reputation._metric_kind_from_title("未按时发货"), "shipments")
        self.assertEqual(reputation._metric_kind_from_title("卖家取消"), "cancellations")

    def test_unknown_translations_use_stable_card_order(self):
        driver = FakeSeleniumDriver(
            [
                {"title": "指标甲", "percentage": "1%"},
                {"title": "指标乙", "percentage": "2%"},
                {"title": "指标丙", "percentage": "3%"},
                {"title": "指标丁", "percentage": "4%"},
            ]
        )
        self.assertEqual(
            reputation._extract_reputation_metrics(driver),
            {"complaints": "1%", "cancellations": "3%", "shipments": "4%"},
        )

    def test_missing_shipment_card_is_not_silently_mislabeled(self):
        driver = FakeSeleniumDriver(
            [
                {"title": "Complaints", "percentage": "1%"},
                {"title": "Mediations", "percentage": "2%"},
                {"title": "Cancellations", "percentage": "3%"},
            ]
        )
        with self.assertRaises(reputation.MercadoPageStructureError):
            reputation._extract_reputation_metrics(driver)

    def test_color_and_gradient_localization(self):
        self.assertEqual(
            reputation._normalize_reputation_color(
                "MercadoLeader", "thermometer__level--leader"
            ),
            "绿色",
        )
        self.assertEqual(reputation._parse_gradient("下降 12.5%"), ("下滑", "12.5%"))
        self.assertEqual(reputation._parse_gradient("Increased 8%"), ("增长", "8%"))

    def test_login_and_window_failures_are_classified(self):
        state = {
            "current_url": "https://www.mercadolibre.com/jms/cbt/lgz/msl/login/x/legacy-user",
            "title": "Fill out your e-mail address to log in",
            "page_text": "Email Continue",
        }
        self.assertTrue(reputation._is_mercado_login_state(state))
        self.assertEqual(
            reputation._failure_status(reputation.MercadoAuthenticationError("login")),
            "失败：登录失效",
        )
        self.assertEqual(
            reputation._failure_status(
                reputation.BitBrowserWindowError("服务调用成功，但没有找到相应数据！")
            ),
            "失败：窗口ID不存在",
        )
        self.assertEqual(
            reputation._failure_status(
                reputation.BitBrowserWindowError("timeout of 30000ms exceeded")
            ),
            "失败：窗口打开超时",
        )

    def test_site_list_accepts_chinese_and_ascii_separators(self):
        self.assertEqual(
            reputation._split_sites("墨西哥，巴西,智利；阿根廷"),
            ["墨西哥", "巴西", "智利", "阿根廷"],
        )


class InfractionLocalizationTests(unittest.TestCase):
    def test_login_redirect_is_detected(self):
        page = FakePlaywrightPage(
            "https://www.mercadolibre.com/jms/cbt/lgz/msl/login/x/legacy-user",
            "Fill out your e-mail address to log in",
        )
        with self.assertRaises(infractions.MercadoAuthenticationError):
            infractions._raise_if_page_unavailable(page)

    def test_chinese_infraction_page_is_valid(self):
        page = FakePlaywrightPage(
            "https://global-selling.mercadolibre.com/noindex/pppi/infractions?tab=detections&offset=0",
            "知识产权侵权 - 墨西哥\n由 Mercado Libre 检测到",
        )
        self.assertTrue(infractions._validate_infractions_page(page))

    def test_chinese_pagination_offset_preserves_query(self):
        url = (
            "https://global-selling.mercadolibre.com/noindex/pppi/infractions"
            "?tab=detections&offset=0"
        )
        self.assertEqual(
            infractions._offset_url(url, ("1", "2", "3")),
            url.replace("offset=0", "offset=3"),
        )

    def test_infraction_failure_reason(self):
        self.assertEqual(
            infractions._failure_status(
                infractions.BitBrowserWindowError("窗口无效或不存在")
            ),
            "失败：窗口ID不存在",
        )

    def test_bitbrowser_open_retries_transient_timeout(self):
        success = {"success": True, "data": {"http": "127.0.0.1:12345"}}
        with mock.patch.object(
            infractions,
            "openBrowser",
            side_effect=[{"success": False, "msg": "timeout of 30000ms exceeded"}, success],
        ):
            self.assertEqual(
                infractions._open_bitbrowser("window-id", max_retries=2, retry_delay=0),
                success,
            )


if __name__ == "__main__":
    unittest.main()
