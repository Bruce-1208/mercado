import requests
import json
import hashlib
import os
import threading
import time

from bit.bit_runtime_lock import (
    InterProcessLock,
    create_window_lease,
    current_thread_window_lease,
    get_lock_owner,
    window_lock_key,
)

# 官方文档地址
# https://doc2.bitbrowser.cn/jiekou/ben-di-fu-wu-zhi-nan.html

# 此demo仅作为参考使用，以下使用的指纹参数仅是部分参数，完整参数请参考文档

url = "http://127.0.0.1:54345"
headers = {"Content-Type": "application/json"}
_AUTO_LEASES = {}
_AUTO_LEASES_GUARD = threading.Lock()
_BROWSER_API_MUTATION_LOCK_KEY = "bit_browser_api_mutation"
_BROWSER_API_LOCK_TIMEOUT = int(os.environ.get("BIT_BROWSER_API_LOCK_TIMEOUT", "180"))
_BROWSER_API_MUTATION_CONCURRENCY = max(
    1,
    min(int(os.environ.get("BIT_BROWSER_API_MUTATION_CONCURRENCY", "1")), 8),
)
_BROWSER_OPEN_TIMEOUT = int(os.environ.get("BIT_BROWSER_OPEN_TIMEOUT", "60"))
_BROWSER_CLOSE_TIMEOUT = int(os.environ.get("BIT_BROWSER_CLOSE_TIMEOUT", "30"))
try:
    _BROWSER_OPEN_COOLDOWN_SECONDS = max(
        0.0,
        float(os.environ.get("BIT_BROWSER_OPEN_COOLDOWN_SECONDS", "2")),
    )
except (TypeError, ValueError):
    _BROWSER_OPEN_COOLDOWN_SECONDS = 2.0


def _browser_api_slot_order(browser_id, slot_count=None):
    slot_count = max(
        1,
        int(slot_count or _BROWSER_API_MUTATION_CONCURRENCY),
    )
    digest = hashlib.sha256(str(browser_id or "").encode("utf-8")).digest()
    start_index = int.from_bytes(digest[:4], "big") % slot_count
    return tuple((start_index + offset) % slot_count for offset in range(slot_count))


def _acquire_browser_api_mutation_slot(endpoint, browser_id, timeout):
    """限制跨进程窗口操作；默认串行，避免 BitBrowser 并发启动超时。"""
    timeout = max(1, float(timeout))
    deadline = time.monotonic() + timeout
    slot_order = _browser_api_slot_order(browser_id)
    while True:
        for slot_index in slot_order:
            slot_lock = InterProcessLock(
                f"{_BROWSER_API_MUTATION_LOCK_KEY}_slot_{slot_index}",
                owner=f"bit_api.{endpoint}",
                metadata={
                    "endpoint": endpoint,
                    "window_id": str(browser_id or ""),
                    "slot": slot_index,
                },
            )
            if slot_lock.acquire(timeout=0):
                return slot_lock
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"等待 BitBrowser {endpoint} 接口并发槽位超时：{int(timeout)} 秒"
            )
        time.sleep(0.1)


def _post_browser_mutation(
    endpoint, browser_id, request_timeout, *, api_lock_timeout=None
):
    """限流调用 BitBrowser 窗口接口；窗口连接后的业务仍可多进程并行。"""
    api_lock = _acquire_browser_api_mutation_slot(
        endpoint,
        browser_id,
        (
            _BROWSER_API_LOCK_TIMEOUT
            if api_lock_timeout is None
            else max(1, float(api_lock_timeout))
        ),
    )
    try:
        return requests.post(
            f"{url}/browser/{endpoint}",
            data=json.dumps({"id": f"{browser_id}"}),
            headers=headers,
            timeout=max(1, int(request_timeout)),
        ).json()
    finally:
        # BitBrowser's local API rejects back-to-back window starts even when the
        # HTTP calls are serialized. Keep a small cross-process gap; once opened,
        # all browser business work still runs concurrently.
        if endpoint == "open" and _BROWSER_OPEN_COOLDOWN_SECONDS:
            time.sleep(_BROWSER_OPEN_COOLDOWN_SECONDS)
        api_lock.release()


def createBrowser():  # 创建或者更新窗口，指纹参数 browserFingerPrint 如没有特定需求，只需要指定下内核即可，如果需要更详细的参数，请参考文档
    json_data = {
        "name": "google",  # 窗口名称
        "remark": "",  # 备注
        "proxyMethod": 2,  # 代理方式 2自定义 3 提取IP
        # 代理类型  ['noproxy', 'http', 'https', 'socks5', 'ssh']
        "proxyType": "noproxy",
        "host": "",  # 代理主机
        "port": "",  # 代理端口
        "proxyUserName": "",  # 代理账号
        "browserFingerPrint": {  # 指纹对象
            "coreVersion": "124"  # 内核版本，注意，win7/win8/winserver 2012 已经不支持112及以上内核了，无法打开
        },
    }

    res = requests.post(
        f"{url}/browser/update", data=json.dumps(json_data), headers=headers
    ).json()
    browserId = res["data"]["id"]
    print(browserId)
    return browserId


def updateBrowser():  # 更新窗口，支持批量更新和按需更新，ids 传入数组，单独更新只传一个id即可，只传入需要修改的字段即可，比如修改备注，具体字段请参考文档，browserFingerPrint指纹对象不修改，则无需传入
    json_data = {
        "ids": ["93672cf112a044f08b653cab691216f0"],
        "remark": "我是一个备注",
        "browserFingerPrint": {},
    }
    res = requests.post(
        f"{url}/browser/update/partial", data=json.dumps(json_data), headers=headers
    ).json()
    print(res)


def listBrowsers(page_size=100, max_pages=100):
    """读取 BitBrowser 窗口列表，供授权店铺按名称实时匹配窗口。"""
    browsers = []
    for page in range(max(1, int(max_pages))):
        response = requests.post(
            f"{url}/browser/list",
            data=json.dumps({"page": page, "pageSize": max(1, int(page_size))}),
            headers=headers,
            timeout=max(1, _BROWSER_OPEN_TIMEOUT),
        ).json()
        if not isinstance(response, dict) or response.get("success") is False:
            message = response.get("msg") if isinstance(response, dict) else response
            raise RuntimeError(f"读取比特浏览器窗口列表失败：{message or response}")
        page_rows = (response.get("data") or {}).get("list") or []
        if not isinstance(page_rows, list):
            raise RuntimeError(f"比特浏览器窗口列表返回格式异常：{response}")
        browsers.extend(row for row in page_rows if isinstance(row, dict))
        if len(page_rows) < max(1, int(page_size)):
            break
    return browsers


def getBrowserIdByName(name, page_size=100, max_pages=100, browsers=None):
    """按 BitBrowser 窗口名称查找 ID，优先且要求唯一的精确匹配。"""
    wanted_name = str(name or "").strip()
    if not wanted_name:
        raise ValueError("请输入比特浏览器窗口名称")

    if browsers is None:
        browsers = listBrowsers(page_size=page_size, max_pages=max_pages)

    exact_matches = [
        row for row in browsers
        if str(row.get("name") or "").strip() == wanted_name
    ]
    if not exact_matches:
        folded_name = wanted_name.casefold()
        exact_matches = [
            row for row in browsers
            if str(row.get("name") or "").strip().casefold() == folded_name
        ]
    if not exact_matches:
        # 授权名称常写成“蒋学斌2”，而 BitBrowser 窗口可能是“蒋学斌 2”。
        # 仅忽略空白字符，并且仍要求唯一，避免模糊匹配到错误店铺。
        compact_name = "".join(wanted_name.split()).casefold()
        exact_matches = [
            row
            for row in browsers
            if "".join(str(row.get("name") or "").split()).casefold()
            == compact_name
        ]
    if not exact_matches:
        raise RuntimeError(f"未找到名称为“{wanted_name}”的比特浏览器窗口")
    if len(exact_matches) > 1:
        raise RuntimeError(
            f"存在 {len(exact_matches)} 个同名比特浏览器窗口“{wanted_name}”，"
            "请先在比特浏览器中将窗口名称改为唯一名称"
        )
    browser_id = str(exact_matches[0].get("id") or "").strip()
    if not browser_id:
        raise RuntimeError(f"比特浏览器窗口“{wanted_name}”缺少窗口 ID")
    return browser_id


def openBrowser(
    id, *, api_lock_timeout=None, request_timeout=None
):  # 直接指定ID打开窗口，也可以使用 createBrowser 方法返回的ID
    auto_lease = None
    if current_thread_window_lease(id) is None:
        auto_lease = create_window_lease(
            id,
            owner="bit_api.openBrowser",
            task_type="legacy_browser_task",
        )
        if not auto_lease.acquire(timeout=0):
            return {
                "success": False,
                "msg": "窗口正在被其他任务占用",
                "lockOwner": get_lock_owner(window_lock_key(id)),
            }
        with _AUTO_LEASES_GUARD:
            _AUTO_LEASES[(threading.get_ident(), str(id))] = auto_lease
    try:
        res = _post_browser_mutation(
            "open",
            id,
            _BROWSER_OPEN_TIMEOUT if request_timeout is None else request_timeout,
            api_lock_timeout=api_lock_timeout,
        )
        if auto_lease is not None and isinstance(res, dict) and res.get("success") is False:
            with _AUTO_LEASES_GUARD:
                _AUTO_LEASES.pop((threading.get_ident(), str(id)), None)
            auto_lease.release()
        return res
    except Exception:
        if auto_lease is not None:
            with _AUTO_LEASES_GUARD:
                _AUTO_LEASES.pop((threading.get_ident(), str(id)), None)
            auto_lease.release()
        raise


def releaseBrowserLease(id):
    """释放 openBrowser 自动获取的任务锁，但保持浏览器窗口打开。"""
    auto_lease_key = (threading.get_ident(), str(id))
    with _AUTO_LEASES_GUARD:
        auto_lease = _AUTO_LEASES.pop(auto_lease_key, None)
    if auto_lease is not None:
        auto_lease.release()


def closeBrowser(
    id,
    lease=None,
    force=False,
    request_timeout=None,
    api_lock_timeout=None,
):  # 关闭窗口
    close_timeout = (
        _BROWSER_CLOSE_TIMEOUT
        if request_timeout is None
        else max(1, int(request_timeout))
    )
    if force:
        return _post_browser_mutation(
            "close",
            id,
            close_timeout,
            api_lock_timeout=api_lock_timeout,
        )
    active_lease = lease or current_thread_window_lease(id)
    auto_lease_key = (threading.get_ident(), str(id))
    with _AUTO_LEASES_GUARD:
        auto_lease = _AUTO_LEASES.get(auto_lease_key)
    temporary_lease = None
    if active_lease is None or not active_lease.acquired:
        temporary_lease = create_window_lease(
            id,
            owner="bit_api.closeBrowser",
            task_type="close_only",
        )
        if not temporary_lease.acquire(timeout=0):
            return {
                "success": False,
                "skipped": True,
                "msg": "窗口正在被其他任务使用，已跳过关闭",
                "lockOwner": get_lock_owner(window_lock_key(id)),
            }
    try:
        return _post_browser_mutation(
            "close",
            id,
            close_timeout,
            api_lock_timeout=api_lock_timeout,
        )
    finally:
        if temporary_lease is not None:
            temporary_lease.release()
        if auto_lease is not None and active_lease is auto_lease:
            with _AUTO_LEASES_GUARD:
                _AUTO_LEASES.pop(auto_lease_key, None)
            auto_lease.release()


def deleteBrowser(id):  # 删除窗口
    json_data = {"id": f"{id}"}
    print(
        requests.post(
            f"{url}/browser/delete", data=json.dumps(json_data), headers=headers
        ).json()
    )


if __name__ == "__main__":
    browser_id = "fd3f4b699b4447b2a0eb2db1bf66b1aa"
    # browser_id = createBrowser()
    openBrowser(browser_id)

    time.sleep(10)  # 等待10秒自动关闭窗口

    closeBrowser(browser_id)

    time.sleep(10)  # 等待10秒自动删掉窗口

    deleteBrowser(browser_id)
