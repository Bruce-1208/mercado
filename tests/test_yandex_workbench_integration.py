from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

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
        self.assertEqual(payload["data"]["url"], "/yandex-console/?embedded=1")
        self.assertEqual(payload["data"]["external_url"], "/yandex-console/")

    def test_yandex_proxy_requires_login_for_api(self) -> None:
        response = self.client.get("/yandex-console/api/health")
        self.assertEqual(response.status_code, 401)

    def test_yandex_proxy_forwards_to_local_service(self) -> None:
        self.login()
        upstream = MagicMock()
        upstream.status = 200
        upstream.read.return_value = b'{"status":"ok"}'
        upstream.headers = {"Content-Type": "application/json"}
        with patch.object(workbench, "urlopen", return_value=upstream) as open_url:
            response = self.client.get("/yandex-console/api/health?check=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})
        forwarded_request = open_url.call_args.args[0]
        self.assertEqual(
            forwarded_request.full_url,
            "http://127.0.0.1:8011/api/health?check=1",
        )
        self.assertEqual(
            forwarded_request.get_header("X-forwarded-prefix"),
            "/yandex-console",
        )

    def test_yandex_proxy_rewrites_assets_for_an_existing_process(self) -> None:
        self.login()
        upstream = MagicMock()
        upstream.status = 200
        upstream.read.return_value = (
            b'<link href="/static/styles.css">'
            b'<script>window.YANDEX_BASE_PATH = "";</script>'
            b'<script src="/static/app.js"></script>'
        )
        upstream.headers = {"Content-Type": "text/html; charset=utf-8"}
        with patch.object(workbench, "ensure_yandex_console", return_value=(True, "ok")), patch.object(
            workbench, "urlopen", return_value=upstream
        ):
            response = self.client.get("/yandex-console/?embedded=1")

        html = response.get_data(as_text=True)
        self.assertIn('href="/yandex-console/static/styles.css"', html)
        self.assertIn('src="/yandex-console/static/app.js"', html)
        self.assertIn('window.YANDEX_BASE_PATH = "/yandex-console";', html)

    def test_yandex_setup_command_matches_platform(self) -> None:
        self.assertEqual(
            workbench._yandex_console_setup_command("nt"),
            r".\yandex\run.ps1",
        )
        self.assertEqual(
            workbench._yandex_console_setup_command("posix"),
            "./yandex/run.sh",
        )


if __name__ == "__main__":
    unittest.main()
