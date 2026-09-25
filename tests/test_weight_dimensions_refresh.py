from unittest.mock import patch

from bit import weight_dimensions_records as records


def test_refresh_without_upload_reuses_running_task():
    with patch.object(records, "_tasks", {}), patch.object(records, "_latest_task_by_owner", {}), patch.object(records.threading, "Thread") as thread:
        first = records.start_changed_refresh("user", {"store_ids": [1]}, [], {1})
        second = records.start_changed_refresh("user", {"store_ids": [1]}, [], {1})
        assert first["task_id"] == second["task_id"]
        assert thread.call_count == 1
        records._tasks[first["task_id"]]["status"] = "ready"
        third = records.start_changed_refresh("user", {"store_ids": [1]}, [], {1})
        assert third["task_id"] != first["task_id"]


def test_refresh_does_not_reuse_other_filters_owner_or_permissions():
    with patch.object(records, "_tasks", {}), patch.object(records, "_latest_task_by_owner", {}), patch.object(records.threading, "Thread"):
        calls = [
            ("user", {"store_ids": [1]}, {1}),
            ("other", {"store_ids": [1]}, {1}),
            ("user", {"store_ids": [1], "region": "MX"}, {1}),
            ("user", {"store_ids": [1]}, {1, 2}),
        ]
        ids = {records.start_changed_refresh(owner, filters, [], allowed)["task_id"]
               for owner, filters, allowed in calls}
        assert len(ids) == len(calls)
