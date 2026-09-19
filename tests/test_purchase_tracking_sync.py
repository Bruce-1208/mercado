import time

from bit.purchase_tracking_sync import (
    PurchaseTrackingSyncManager,
    extract_tracking,
    supported_platforms,
)


def test_supported_purchase_platforms_include_requested_sites():
    assert {row["id"] for row in supported_platforms()} == {
        "1688", "taobao", "pdd", "xianyu",
    }


def test_extract_tracking_number_and_company_from_platform_text():
    result = extract_tracking("物流公司：顺丰速运  运单号：SF123456789CN 已揽收")

    assert result == {
        "tracking_number": "SF123456789CN",
        "logistics_company": "shunfeng",
    }


def test_sync_manager_updates_each_order_without_exposing_password():
    opened = []
    updated = []

    class FakeAdapter:
        def __init__(self, platform, account, password, state_callback):
            opened.append((platform, account, password))
            self.state_callback = state_callback

        def open(self):
            self.state_callback(phase="syncing", message="已登录")

        def fetch_tracking(self, purchase_order):
            if purchase_order == "PO-2":
                return {"status": "pending", "message": "尚未发货"}
            return {
                "status": "synced",
                "tracking_number": "SF123456789CN",
                "logistics_company": "shunfeng",
            }

        def close(self):
            pass

    manager = PurchaseTrackingSyncManager(adapter_factory=FakeAdapter)
    manager.start(
        platform="1688",
        account="buyer@example.com",
        password="secret-value",
        orders=[
            {"order_id": "20001", "purchase_order": "PO-1"},
            {"order_id": "20002", "purchase_order": "PO-2"},
        ],
        update_order=lambda *args: updated.append(args),
    )
    deadline = time.monotonic() + 2
    while manager.status()["running"] and time.monotonic() < deadline:
        time.sleep(0.01)

    state = manager.status()
    assert opened == [("1688", "buyer@example.com", "secret-value")]
    assert updated == [("20001", "SF123456789CN", "shunfeng")]
    assert state["phase"] == "completed"
    assert state["synced"] == 1
    assert state["pending"] == 1
    assert "secret-value" not in repr(state)


def test_sync_manager_rejects_orders_without_purchase_number():
    manager = PurchaseTrackingSyncManager()

    try:
        manager.start(
            platform="taobao",
            account="buyer",
            password="",
            orders=[{"order_id": "20001", "purchase_order": ""}],
            update_order=lambda *_args: None,
        )
    except ValueError as exc:
        assert "采购订单号" in str(exc)
    else:
        raise AssertionError("expected ValueError")

