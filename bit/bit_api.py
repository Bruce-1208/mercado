import requests
import json
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
_BROWSER_OPEN_TIMEOUT = int(os.environ.get("BIT_BROWSER_OPEN_TIMEOUT", "60"))
_BROWSER_CLOSE_TIMEOUT = int(os.environ.get("BIT_BROWSER_CLOSE_TIMEOUT", "30"))


def _post_browser_mutation(endpoint, browser_id, request_timeout):
    """串行调用 BitBrowser 窗口接口，避免本地服务被并发打开请求阻塞。"""
    api_lock = InterProcessLock(
        _BROWSER_API_MUTATION_LOCK_KEY,
        owner=f"bit_api.{endpoint}",
        metadata={"endpoint": endpoint, "window_id": str(browser_id or "")},
    )
    if not api_lock.acquire(timeout=max(1, _BROWSER_API_LOCK_TIMEOUT)):
        raise TimeoutError(
            f"等待 BitBrowser {endpoint} 接口锁超时：{_BROWSER_API_LOCK_TIMEOUT} 秒"
        )
    try:
        return requests.post(
            f"{url}/browser/{endpoint}",
            data=json.dumps({"id": f"{browser_id}"}),
            headers=headers,
            timeout=max(1, int(request_timeout)),
        ).json()
    finally:
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


def openBrowser(id):  # 直接指定ID打开窗口，也可以使用 createBrowser 方法返回的ID
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
        res = _post_browser_mutation("open", id, _BROWSER_OPEN_TIMEOUT)
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


def closeBrowser(id, lease=None):  # 关闭窗口
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
        return _post_browser_mutation("close", id, _BROWSER_CLOSE_TIMEOUT)
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
