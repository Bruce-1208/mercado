import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bit import bit_collection_control as control
from bit import bit_main
from bit import bit_reputation_info as reputation
from bit_playwright import bit_infractions_info as infractions


class CollectionControlTests(unittest.TestCase):
    def test_active_rate_limit_pause_is_shared_without_repeated_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "rate-limit.json"
            with mock.patch.object(control, "RATE_LIMIT_STATE_PATH", state_path):
                first = control.trip_batch_rate_limit(
                    "声誉",
                    "429",
                    pause_seconds=300,
                    now=100,
                )
                second = control.trip_batch_rate_limit(
                    "侵权",
                    "429 again",
                    pause_seconds=300,
                    now=200,
                )
                self.assertEqual(first["pause_until"], 400)
                self.assertEqual(second["pause_until"], 400)
                self.assertEqual(control.batch_pause_remaining(now=250), 150)

    def test_only_non_success_outcomes_are_failed(self):
        self.assertFalse(control.outcome_failed([("任务", "店铺", "MX", "成功", "now")]))
        self.assertTrue(
            control.outcome_failed(
                [("任务", "店铺", "MX", "跳过：窗口被其他任务占用", "now")]
            )
        )

    def test_unreadable_site_report_contains_only_final_failures(self):
        rows = [
            ("获取声誉信息", "正常店", "墨西哥", "成功", "2026-07-24 01:00:00"),
            ("获取声誉信息", "异常店", "巴西", "失败：页面结构变化", "2026-07-24 01:01:00"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output_path = control.write_unreadable_site_report(
                "声誉采集",
                rows,
                output_dir=directory,
            )
            content = output_path.read_text(encoding="utf-8-sig")
        self.assertIn("异常店", content)
        self.assertIn("巴西", content)
        self.assertNotIn("正常店", content)


class CollectionOrchestrationTests(unittest.TestCase):
    def test_scheduler_runs_reputation_then_cooldown_then_infraction_and_pauses_ai(self):
        events = []
        process_lock = mock.Mock()
        process_lock.acquire.return_value = True

        with (
            mock.patch.object(bit_main, "InterProcessLock", return_value=process_lock),
            mock.patch.object(
                bit_main.bit_reputation_info,
                "main",
                side_effect=lambda **_kwargs: events.append("reputation") or {"ok": True},
            ) as reputation_main,
            mock.patch.object(
                bit_main.bit_infractions_info,
                "main",
                side_effect=lambda **_kwargs: events.append("infraction") or {"ok": True},
            ) as infraction_main,
            mock.patch.object(bit_main.random, "uniform", return_value=240),
            mock.patch.object(
                bit_main.time,
                "sleep",
                side_effect=lambda seconds: events.append(("sleep", seconds)),
            ),
            mock.patch.object(bit_main, "wait_for_batch_resume"),
            mock.patch.object(bit_main, "_get_bool_env", return_value=False),
            mock.patch.dict("os.environ", {}, clear=True),
        ):
            result = bit_main.run_reputation_infraction_then_daily()

        self.assertEqual(events, ["reputation", ("sleep", 240), "infraction"])
        self.assertIsNone(result["ai_appeal"])
        self.assertEqual(reputation_main.call_args.kwargs["max_workers"], 10)
        self.assertEqual(infraction_main.call_args.kwargs["max_workers"], 10)
        self.assertEqual(reputation_main.call_args.kwargs["stagger_min_seconds"], 5)
        self.assertEqual(reputation_main.call_args.kwargs["stagger_max_seconds"], 10)
        self.assertEqual(result["errors"], {})
        process_lock.release.assert_called_once_with()

    def test_infraction_still_runs_when_reputation_post_processing_fails(self):
        events = []
        process_lock = mock.Mock()
        process_lock.acquire.return_value = True

        with (
            mock.patch.object(bit_main, "InterProcessLock", return_value=process_lock),
            mock.patch.object(
                bit_main.bit_reputation_info,
                "main",
                side_effect=RuntimeError("声誉数据库写入失败"),
            ),
            mock.patch.object(
                bit_main.bit_infractions_info,
                "main",
                side_effect=lambda **_kwargs: events.append("infraction") or {"ok": True},
            ),
            mock.patch.object(bit_main, "_wait_between_collections"),
            mock.patch.object(bit_main, "_run_ai_appeal_loop", return_value=None),
        ):
            result = bit_main.run_reputation_infraction_then_daily()

        self.assertEqual(events, ["infraction"])
        self.assertEqual(result["errors"], {"reputation": "声誉数据库写入失败"})
        self.assertEqual(result["infraction"], {"ok": True})

    def test_retry_plan_contains_only_failed_repairable_shops(self):
        successful = ("id-ok", "正常店", "", "墨西哥", "", "", "")
        occupied = ("id-busy", "占用店", "", "墨西哥", "", "", "")
        invalid = ("id-invalid", "无效店", "", "墨西哥", "", "", "")
        outcomes = {
            control.row_key(successful): (
                successful,
                [],
                [("获取声誉信息", "正常店", "墨西哥", "成功", "now")],
            ),
            control.row_key(occupied): (
                occupied,
                [],
                [("获取声誉信息", "占用店", "墨西哥", "跳过：窗口被其他任务占用", "now")],
            ),
            control.row_key(invalid): (
                invalid,
                [],
                [("获取声誉信息", "无效店", "墨西哥", "失败：窗口ID不存在", "now")],
            ),
        }
        with mock.patch.object(
            reputation,
            "list_config_rows",
            return_value=[successful, occupied, invalid],
        ):
            retry_plan = reputation._prepare_reputation_retry_rows(outcomes)
        self.assertEqual(retry_plan, [(control.row_key(occupied), occupied)])

    def test_final_infraction_rate_limit_attempt_trips_whole_batch(self):
        response = {"success": False, "msg": "HTTP 429 too many requests"}
        with (
            mock.patch.object(infractions, "openBrowser", return_value=response),
            mock.patch.object(infractions, "trip_batch_rate_limit") as trip,
        ):
            with self.assertRaises(infractions.MercadoRateLimitError):
                infractions._open_bitbrowser(
                    "window-id",
                    max_retries=1,
                    retry_delay=0,
                    batch_control=True,
                )
        trip.assert_called_once()

    def test_concurrent_reputation_page_does_not_switch_global_clash(self):
        state = {
            "current_url": "https://global-selling.mercadolibre.com/reputation",
            "title": "",
            "page_text": "Hubo un error accediendo a esta página",
            "page_source": "",
        }
        driver = mock.Mock()
        with (
            mock.patch.object(reputation.time, "sleep"),
            mock.patch.object(reputation, "_get_mercado_page_state", return_value=state),
        ):
            with self.assertRaises(reputation.MercadoRateLimitError):
                reputation._open_reputation_page_with_validation(
                    driver,
                    "店铺",
                    "墨西哥",
                    allow_global_ip_switch=False,
                )
        self.assertFalse(hasattr(reputation, "switch_random_hongkong_node"))


if __name__ == "__main__":
    unittest.main()
