from __future__ import annotations

import unittest
from unittest.mock import patch

import bit.bit_interface as workbench


class YandexWorkbenchIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        workbench.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.client = workbench.app.test_client()

    def login(self) -> None:
        with self.client.session_transaction() as session:
            session["workbench_user"] = {
                "id": 1,
                "username": "tester",
                "display_name": "测试用户",
            }

    def test_workbench_contains_yandex_tab(self) -> None:
        self.login()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-tab="yandex"'.encode(), response.data)
        self.assertIn('id="yandex-console-frame"'.encode(), response.data)

    def test_yandex_start_requires_login(self) -> None:
        response = self.client.post("/api/yandex-console/start", json={})
        self.assertEqual(response.status_code, 401)

    def test_yandex_start_returns_embedded_url(self) -> None:
        self.login()
        with patch.object(
            workbench,
            "ensure_yandex_console",
            return_value=(True, "Yandex 控制台已启动"),
        ):
            response = self.client.post("/api/yandex-console/start", json={})
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "success")
        self.assertTrue(payload["data"]["running"])
        self.assertTrue(payload["data"]["url"].endswith("/?embedded=1"))


if __name__ == "__main__":
    unittest.main()
