import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bit import bit_collection_control as control
from bit import bit_main
from bit import bit_reputation_info as reputation
from bit_playwright import bit_infractions_info as infractions


class CollectionControlTests(unittest.TestCase):
    def test_filter_config_rows_limits_shops_and_site_intersection(self):
        rows = [
            ("id-1", "店铺甲", "", "墨西哥，巴西，阿根廷", "", "", ""),
            ("id-2", "店铺乙", "", "墨西哥，智利", "", "", ""),
            ("id-3", "店铺丙", "", "巴西", "", "", ""),
        ]

        filtered = control.filter_config_rows(
            rows,
            selected_shops=["店铺甲", "店铺乙"],
            selected_sites=["巴西", "智利"],
        )

        self.assertEqual(
            filtered,
            [
                ("id-1", "店铺甲", "", "巴西", "", "", ""),
                ("id-2", "店铺乙", "", "智利", "", "", ""),
            ],
        )

    def test_filter_config_rows_accepts_single_string_selection(self):
        rows = [("id", "店铺", "", "墨西哥/巴西", "", "", "")]
        self.assertEqual(
            control.filter_config_rows(
                rows,
                selected_shops="店铺",
                selected_sites="巴西",
            ),
            [("id", "店铺", "", "巴西", "", "", "")],
        )

    def test_site_retry_merge_preserves_other_successful_sites(self):
        row = ("id", "店铺", "", "墨西哥，巴西", "", "", "")
        original = (
            row,
            [["店铺", "墨西哥", "绿色"], ["店铺", "巴西", "执行失败"]],
            [
                ("获取声誉信息", "店铺", "墨西哥", "成功", "old"),
                ("获取声誉信息", "店铺", "巴西", "失败：超时", "old"),
            ],
        )
        retry = (
            ("id", "店铺", "", "巴西", "", "", ""),
            [["店铺", "巴西", "黄色"]],
            [("获取声誉信息", "店铺", "巴西", "成功", "new")],
        )
        merged = control.merge_site_retry_outcome(original, retry)
        self.assertEqual(merged[1], [["店铺", "墨西哥", "绿色"], ["店铺", "巴西", "黄色"]])
        self.assertEqual([item[3] for item in merged[2]], ["成功", "成功"])

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
    def test_infraction_scope_comes_from_explicit_authorization_switches(self):
        rows = [("id-1", "店铺甲", "", "巴西", "", "", "")]
        token_data = {
            "rows": [
                {
                    "display_name": "店铺甲",
                    "site_settings": [
                        {"site_id": "MLM", "visit_stats_enabled": True},
                        {"site_id": "MLB", "visit_stats_enabled": False},
                    ],
                }
            ]
        }

        self.assertEqual(
            infractions._authorized_visit_stats_rows(rows, token_data),
            [("id-1", "店铺甲", "", "墨西哥", "", "", "")],
        )

    def test_infraction_collector_executes_only_selected_shop_site_rows(self):
        rows = [
            ("id-1", "店铺甲", "", "墨西哥，巴西", "", "", ""),
            ("id-2", "店铺乙", "", "巴西，智利", "", "", ""),
        ]
        captured = []

        def stop_after_capture(filtered_rows, **_kwargs):
            captured.extend(filtered_rows)
            raise RuntimeError("stop-after-selection")

        with (
            mock.patch.object(infractions, "list_config_rows", return_value=rows),
            mock.patch.object(
                infractions,
                "list_mercado_store_tokens",
                return_value={
                    "rows": [
                        {
                            "display_name": "店铺甲",
                            "site_settings": [
                                {"site_id": "MLM", "visit_stats_enabled": True},
                                {"site_id": "MLB", "visit_stats_enabled": True},
                            ],
                        },
                        {
                            "display_name": "店铺乙",
                            "site_settings": [
                                {"site_id": "MLB", "visit_stats_enabled": True},
                                {"site_id": "MLC", "visit_stats_enabled": False},
                            ],
                        },
                    ]
                },
            ),
            mock.patch.object(
                infractions,
                "_execute_infraction_rows",
                side_effect=stop_after_capture,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop-after-selection"):
                infractions.get_infractions_info_all(
                    selected_shops=["店铺乙"],
                    selected_sites=["巴西"],
                )

        self.assertEqual(
            captured,
            [("id-2", "店铺乙", "", "巴西", "", "", "")],
        )

    def test_infraction_focused_appeal_sequence_runs_ten_rounds(self):
        started_at = bit_main.datetime.now()
        task_lock = mock.Mock()
        task_lock.acquired = True
        calls = []
        with (
            mock.patch.object(bit_main, "_get_bool_env", return_value=True) as enabled,
            mock.patch.object(
                bit_main.bit_daily_task,
                "acquire_daily_task_lock",
                return_value=task_lock,
            ),
            mock.patch.object(
                bit_main.bit_daily_task,
                "run_ai_appeal_once",
                side_effect=lambda appeal_type, **kwargs: calls.append(
                    (appeal_type, kwargs)
                ) or {"ok": True},
            ),
            mock.patch.dict(
                "os.environ",
                {
                    "BIT_DAILY_ROUND_INTERVAL": "0",
                    "BIT_DAILY_MAX_WORKERS": "30",
                    "BIT_MAIN_BROWSER_WORKER_LIMIT": "30",
                },
                clear=True,
            ),
        ):
            result = bit_main._run_ai_appeal_loop(started_at)

        enabled.assert_called_once_with("BIT_ENABLE_AI_APPEAL_LOOP", True)
        expected_round = [
            appeal_type
            for _step_key, appeal_type in bit_main.MAIN_APPEAL_SEQUENCE
        ]
        self.assertEqual([appeal_type for appeal_type, _kwargs in calls], expected_round * 10)
        self.assertEqual(len(calls), 70)
        self.assertEqual(
            sum(
                appeal_type == bit_main.bit_daily_task.APPEAL_TYPE_INFRACTION
                for appeal_type, _kwargs in calls
            ),
            40,
        )
        self.assertTrue(all(kwargs["max_workers"] == 15 for _type, kwargs in calls))
        self.assertTrue(all(kwargs["top_n"] == 0 for _type, kwargs in calls))
        self.assertTrue(all(kwargs["_task_lock"] is task_lock for _type, kwargs in calls))
        self.assertEqual(result["round_count"], 10)
        self.assertEqual(result["appeal_types"], list(bit_main.MAIN_APPEAL_TYPES))
        self.assertEqual(result["appeal_sequence"], expected_round)
        task_lock.release.assert_called_once_with()

    def test_infraction_still_runs_after_reputation_appeal_failure(self):
        started_at = bit_main.datetime.now()
        task_lock = mock.Mock()
        calls = []

        def run_once(appeal_type, **_kwargs):
            calls.append(appeal_type)
            if appeal_type == bit_main.bit_daily_task.APPEAL_TYPE_DELAY:
                raise RuntimeError("延误申诉失败")
            return {"ok": True}

        with (
            mock.patch.object(
                bit_main.bit_daily_task,
                "acquire_daily_task_lock",
                return_value=task_lock,
            ),
            mock.patch.object(
                bit_main.bit_daily_task,
                "run_ai_appeal_once",
                side_effect=run_once,
            ),
            mock.patch.dict(
                "os.environ",
                {
                    "BIT_DAILY_APPEAL_ROUNDS": "1",
                    "BIT_DAILY_ROUND_INTERVAL": "0",
                },
                clear=True,
            ),
        ):
            result = bit_main._run_ai_appeal_loop(started_at)

        expected_round = [
            appeal_type
            for _step_key, appeal_type in bit_main.MAIN_APPEAL_SEQUENCE
        ]
        self.assertEqual(calls, expected_round)
        self.assertEqual(
            result["rounds"][0]["errors"]["delay"],
            {
                "appeal_type": bit_main.bit_daily_task.APPEAL_TYPE_DELAY,
                "error": "延误申诉失败",
            },
        )
        self.assertIn(
            "after_delay_infraction",
            result["rounds"][0]["results"],
        )
        task_lock.release.assert_called_once_with()

    def test_main_loop_waits_two_hours_after_complete_chain(self):
        with (
            mock.patch.object(
                bit_main,
                "run_infraction_reputation_then_appeal",
                side_effect=[{"cycle": 1}, {"cycle": 2}],
            ) as run_chain,
            mock.patch.object(bit_main.time, "sleep") as sleep,
        ):
            result = bit_main.run_main_loop(max_cycles=2)

        self.assertEqual(result, [{"cycle": 1}, {"cycle": 2}])
        self.assertEqual(run_chain.call_count, 2)
        sleep.assert_called_once_with(2 * 60 * 60)

    def test_order_print_runs_at_most_once_per_24_hours_across_state_reloads(self):
        first_started_at = 1_000_000
        task_lock = mock.Mock()
        print_summary = {"printed": 2, "no_orders": 3, "failed": 0}

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "order-print-state.json"
            with (
                mock.patch.object(bit_main, "ORDER_PRINT_STATE_PATH", state_path),
                mock.patch.object(
                    bit_main.bit_print,
                    "acquire_order_print_lock",
                    return_value=task_lock,
                ) as acquire_lock,
                mock.patch.object(
                    bit_main.bit_print,
                    "print_orders_all",
                    return_value=print_summary,
                ) as print_orders,
            ):
                first = bit_main._run_order_print_if_due(now=first_started_at)
                within_24_hours = bit_main._run_order_print_if_due(
                    now=first_started_at + bit_main.ORDER_PRINT_INTERVAL_SECONDS - 1
                )
                at_24_hours = bit_main._run_order_print_if_due(
                    now=first_started_at + bit_main.ORDER_PRINT_INTERVAL_SECONDS
                )

        self.assertEqual(first["status"], "completed")
        self.assertEqual(within_24_hours["reason"], "within_24_hours")
        self.assertEqual(within_24_hours["wait_seconds"], 1)
        self.assertEqual(at_24_hours["status"], "completed")
        self.assertEqual(print_orders.call_count, 2)
        self.assertEqual(acquire_lock.call_count, 2)
        self.assertEqual(task_lock.release.call_count, 2)

    def test_ai_appeal_round_limit_stops_after_twenty_without_final_sleep(self):
        with (
            mock.patch.object(bit_main.bit_daily_task, "run_ai_appeal_once") as run_once,
            mock.patch.object(bit_main.bit_daily_task.time, "sleep") as sleep,
        ):
            bit_main.bit_daily_task._loop_ai_appeal_locked(
                bit_main.bit_daily_task.APPEAL_TYPE_INFRACTION,
                round_interval=0,
                max_rounds=20,
                task_lock=object(),
            )

        self.assertEqual(run_once.call_count, 20)
        self.assertEqual(sleep.call_count, 19)

    def test_scheduler_runs_infraction_then_cooldown_then_reputation_then_appeal(self):
        events = []
        process_lock = mock.Mock()
        process_lock.acquire.return_value = True

        with (
            mock.patch.object(bit_main, "InterProcessLock", return_value=process_lock),
            mock.patch.object(
                bit_main,
                "_run_order_print_if_due",
                side_effect=lambda: events.append("order_print") or {"status": "completed"},
            ),
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
            mock.patch.object(
                bit_main,
                "_run_ai_appeal_loop",
                side_effect=lambda _started_at: events.append("appeal") or {"ok": True},
            ),
            mock.patch.dict("os.environ", {}, clear=True),
        ):
            result = bit_main.run_infraction_reputation_then_appeal()

        self.assertEqual(
            events,
            ["order_print", "infraction", ("sleep", 240), "reputation", "appeal"],
        )
        self.assertEqual(result["order_print"], {"status": "completed"})
        self.assertEqual(result["ai_appeal"], {"ok": True})
        self.assertEqual(reputation_main.call_args.kwargs["max_workers"], 10)
        self.assertEqual(infraction_main.call_args.kwargs["max_workers"], 10)
        self.assertEqual(reputation_main.call_args.kwargs["stagger_min_seconds"], 5)
        self.assertEqual(reputation_main.call_args.kwargs["stagger_max_seconds"], 10)
        self.assertEqual(result["errors"], {})
        process_lock.release.assert_called_once_with()

    def test_scheduler_caps_explicit_high_worker_settings(self):
        with mock.patch.dict(
            "os.environ",
            {
                "BIT_REPUTATION_MAX_WORKERS": "12",
                "BIT_INFRACTION_MAX_WORKERS": "9",
                "BIT_MAIN_BROWSER_WORKER_LIMIT": "2",
            },
            clear=True,
        ):
            reputation_options = bit_main._collection_options("REPUTATION")
            infraction_options = bit_main._collection_options("INFRACTION")

        self.assertEqual(reputation_options["max_workers"], 2)
        self.assertEqual(infraction_options["max_workers"], 2)

    def test_scheduler_never_exceeds_fifteen_workers(self):
        with mock.patch.dict(
            "os.environ",
            {
                "BIT_REPUTATION_MAX_WORKERS": "30",
                "BIT_INFRACTION_MAX_WORKERS": "30",
                "BIT_MAIN_BROWSER_WORKER_LIMIT": "30",
                "BIT_DAILY_MAX_WORKERS": "30",
            },
            clear=True,
        ):
            reputation_options = bit_main._collection_options("REPUTATION")
            infraction_options = bit_main._collection_options("INFRACTION")

        self.assertEqual(reputation_options["max_workers"], 15)
        self.assertEqual(infraction_options["max_workers"], 15)
        self.assertEqual(bit_main._main_browser_worker_limit(), 15)

    def test_reputation_and_appeal_still_run_when_infraction_fails(self):
        events = []
        process_lock = mock.Mock()
        process_lock.acquire.return_value = True

        with (
            mock.patch.object(bit_main, "InterProcessLock", return_value=process_lock),
            mock.patch.object(
                bit_main,
                "_run_order_print_if_due",
                return_value={"status": "skipped", "reason": "within_24_hours"},
            ),
            mock.patch.object(
                bit_main.bit_infractions_info,
                "main",
                side_effect=RuntimeError("侵权数据库写入失败"),
            ),
            mock.patch.object(
                bit_main.bit_reputation_info,
                "main",
                side_effect=lambda **_kwargs: events.append("reputation") or {"ok": True},
            ),
            mock.patch.object(bit_main, "_wait_between_collections"),
            mock.patch.object(
                bit_main,
                "_run_ai_appeal_loop",
                side_effect=lambda _started_at: events.append("appeal") or {"ok": True},
            ),
        ):
            result = bit_main.run_infraction_reputation_then_appeal()

        self.assertEqual(events, ["reputation", "appeal"])
        self.assertEqual(result["errors"], {"infraction": "侵权数据库写入失败"})
        self.assertEqual(result["reputation"], {"ok": True})
        self.assertEqual(result["ai_appeal"], {"ok": True})

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

    def test_reputation_retry_plan_contains_only_failed_site(self):
        row = ("id", "部分失败店", "", "墨西哥，巴西", "", "", "")
        outcomes = {
            control.row_key(row): (
                row,
                [],
                [
                    ("获取声誉信息", "部分失败店", "墨西哥", "成功", "now"),
                    ("获取声誉信息", "部分失败店", "巴西", "失败：页面元素等待超时", "now"),
                ],
            )
        }
        with mock.patch.object(reputation, "list_config_rows", return_value=[row]):
            retry_plan = reputation._prepare_reputation_retry_rows(outcomes)
        self.assertEqual(
            retry_plan,
            [(control.row_key(row), ("id", "部分失败店", "", "巴西", "", "", ""))],
        )

    def test_only_deterministic_login_failure_is_not_retried(self):
        row = ("id", "登录失效店", "", "墨西哥", "", "", "mail@example.com")
        outcomes = {
            control.row_key(row): (
                row,
                [],
                [("获取声誉信息", "登录失效店", "墨西哥", "失败：登录失效", "now")],
            )
        }
        permanent_failure = {
            "ok": False,
            "status": "浏览器未保存默认密码",
            "message": "需要人工处理",
        }
        transient_failure = {
            "ok": False,
            "status": "登录失败",
            "message": "邮箱页暂时未继续",
        }
        cases = (
            (reputation, reputation._prepare_reputation_retry_rows),
            (infractions, infractions._prepare_infraction_retry_rows),
        )
        for module, prepare in cases:
            with self.subTest(module=module.__name__):
                permanent = set()
                with (
                    mock.patch.object(module, "list_config_rows", return_value=[row]),
                    mock.patch(
                        "bit.bit_mercado_login.login_one_database_shop",
                        side_effect=[permanent_failure],
                    ) as login,
                ):
                    self.assertEqual(prepare(outcomes, permanent), [])
                    self.assertEqual(prepare(outcomes, permanent), [])

                login.assert_called_once()
                self.assertEqual(permanent, {"登录失效店"})

                with (
                    mock.patch.object(module, "list_config_rows", return_value=[row]),
                    mock.patch(
                        "bit.bit_mercado_login.login_one_database_shop",
                        return_value=transient_failure,
                    ) as login,
                ):
                    self.assertEqual(prepare(outcomes, set()), [])
                    self.assertEqual(prepare(outcomes, set()), [])
                self.assertEqual(login.call_count, 2)

    def test_final_infraction_rate_limit_attempt_trips_whole_batch(self):
        response = {
            "success": False,
            "msg": "Hubo un error accediendo a esta página",
        }
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

    def test_infraction_batch_opens_browser_once_per_shop(self):
        row = ("window-id", "多站点店", "", "墨西哥，巴西", "", "", "")
        context = mock.MagicMock()
        playwright = object()
        context.__enter__.return_value = playwright
        page = mock.Mock()
        with (
            mock.patch.object(
                infractions,
                "_open_bitbrowser",
                return_value={"data": {"http": "127.0.0.1:12345"}},
            ) as open_browser,
            mock.patch.object(
                infractions,
                "_load_playwright_sync_api",
                return_value=(mock.Mock(return_value=context), None),
            ),
            mock.patch.object(
                infractions,
                "_connect_bitbrowser_with_playwright",
                return_value=(object(), page),
            ),
            mock.patch.object(
                infractions,
                "_collect_site_infractions",
                side_effect=[[], []],
            ) as collect_site,
            mock.patch.object(infractions, "closeBrowser"),
            mock.patch.object(infractions, "wait_for_batch_resume"),
        ):
            _data, results = infractions._run_infractions_for_browser_locked(row)

        open_browser.assert_called_once()
        self.assertEqual(collect_site.call_count, 2)
        self.assertEqual([item[2] for item in results], ["墨西哥", "巴西"])
        self.assertTrue(all(item[3] == "成功" for item in results))

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

    def test_infraction_login_repair_records_human_verification_source(self):
        from bit import bit_mercado_login

        row = ("window-captcha", "人机验证店铺", "", "墨西哥", "", "", "")
        outcomes = {
            control.row_key(row): (
                row,
                [],
                [("获取侵权信息", "人机验证店铺", "墨西哥", "失败：登录失效", "now")],
            )
        }
        recorded = []
        with (
            mock.patch.object(infractions, "list_config_rows", return_value=[row]),
            mock.patch.object(
                bit_mercado_login,
                "login_one_database_shop",
                return_value={
                    "ok": False,
                    "status": bit_mercado_login.LOGIN_CAPTCHA_REQUIRED,
                    "login_stage": "captcha",
                    "message": "检测到人机验证，需要人工处理",
                },
            ),
            mock.patch.object(
                infractions,
                "record_human_verification_anomaly",
                side_effect=lambda *args, **kwargs: recorded.append((args, kwargs)),
            ),
        ):
            retry_plan = infractions._prepare_infraction_retry_rows(outcomes)

        self.assertEqual(retry_plan, [])
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0][0][1:3], ("window-captcha", "人机验证店铺"))
        self.assertEqual(recorded[0][1]["site"], "墨西哥")
        self.assertEqual(recorded[0][1]["source"], "侵权采集")


if __name__ == "__main__":
    unittest.main()
