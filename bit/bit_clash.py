import json
import os
import random
import re
import subprocess
from pathlib import Path

import requests

from bit import bit_runtime_lock

# 配置信息

CLASH_API_URL = os.environ.get("CLASH_API_URL", "http://127.0.0.1:9090")
CLASH_SECRET = os.environ.get("CLASH_SECRET", "12345678")

TARGET_GROUP = "🔰 选择节点"  # 确保这个名字和你之前 list_proxies 打印出来的一致

HONGKONG_IP_SWITCH_LOCK_KEY = "clash_hongkong_ip_switch"


def _parse_clash_controller_config(config_path):
    """只读取 Clash 的控制地址和密钥，不依赖额外的 YAML 包。"""
    try:
        content = Path(config_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError, TypeError):
        return None

    controller_match = re.search(
        r"(?m)^\s*external-controller\s*:\s*['\"]?([^'\"#\r\n]+)",
        content,
    )
    if not controller_match:
        return None
    controller = controller_match.group(1).strip()
    if not controller:
        return None
    if not controller.startswith(("http://", "https://")):
        controller = f"http://{controller}"

    secret_match = re.search(
        r"(?m)^\s*secret\s*:\s*['\"]?([^'\"#\r\n]*)",
        content,
    )
    secret = secret_match.group(1).strip() if secret_match else ""
    return controller.rstrip("/"), secret


def _running_clash_config_paths():
    """定位 Clash for Windows 当前核心的 -d 数据目录。"""
    if os.name != "nt":
        return []
    try:
        completed = subprocess.run(
            [
                "wmic",
                "process",
                "where",
                "name='clash-win64.exe'",
                "get",
                "CommandLine",
                "/value",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    paths = []
    for command_line in re.findall(r"(?m)^CommandLine=(.+)$", completed.stdout or ""):
        match = re.search(r"(?:^|\s)-d\s+(?:\"([^\"]+)\"|(\S+))", command_line)
        if match:
            paths.append(Path(match.group(1) or match.group(2)) / "config.yaml")
    return paths


def _resolve_clash_api_settings():
    """优先读取运行中 Clash 的实际控制端口，避免重启后随机端口失效。"""
    if os.environ.get("CLASH_API_URL"):
        return CLASH_API_URL.rstrip("/"), os.environ.get(
            "CLASH_SECRET", CLASH_SECRET
        )

    config_paths = []
    configured_path = os.environ.get("CLASH_CONFIG_PATH")
    if configured_path:
        config_paths.append(Path(configured_path))
    config_paths.extend(_running_clash_config_paths())
    config_paths.extend(
        (
            Path.home() / ".config" / "clash" / "config.yaml",
            Path(os.environ.get("APPDATA", "")) / "clash_win" / "config.yaml",
        )
    )
    seen = set()
    for config_path in config_paths:
        normalized = str(config_path)
        if normalized in seen:
            continue
        seen.add(normalized)
        parsed = _parse_clash_controller_config(config_path)
        if parsed:
            return parsed
    return CLASH_API_URL.rstrip("/"), CLASH_SECRET


def _clash_headers(secret):
    return {"Authorization": f"Bearer {secret}"} if secret else {}


def switch_random_hongkong_node():
    """切换随机香港节点；跨进程串行执行，但不再限制切换间隔。"""
    switch_lock = bit_runtime_lock.InterProcessLock(
        HONGKONG_IP_SWITCH_LOCK_KEY,
        owner="clash_hongkong_ip_switch",
        metadata={"cooldown_seconds": 0},
        stale_seconds=300,
    )
    if not switch_lock.acquire(timeout=30):
        print("⏳ 另一个进程正在切换香港 IP，本次调用跳过")
        return {
            "switched": False,
            "reason": "switch_in_progress",
            "remaining_seconds": 0,
        }

    clash_api_url, clash_secret = _resolve_clash_api_settings()
    headers = _clash_headers(clash_secret)
    proxies_setting = {"http": None, "https": None}

    try:
        # 1. 获取该组当前状态
        url = f"{clash_api_url}/proxies/{TARGET_GROUP}"
        resp = requests.get(
            url,
            headers=headers,
            proxies=proxies_setting,
            timeout=10,
        )

        if resp.status_code != 200:
            print(f"❌ 无法连接到 Clash API，状态码: {resp.status_code}")
            return {
                "switched": False,
                "reason": "clash_api_error",
                "status_code": resp.status_code,
            }

        group_data = resp.json()
        current_node = group_data.get("now")
        all_nodes = group_data.get("all", [])

        # 2. 筛选并排除当前节点
        hk_nodes = [n for n in all_nodes if "香港" in n and n != current_node]

        if not hk_nodes:
            print(f"⚠️ 库里没有多余的香港节点了。当前已在: {current_node}")
            return {
                "switched": False,
                "reason": "no_alternative_hongkong_node",
                "current_node": current_node,
            }

        # 3. 随机选一个
        new_node = random.choice(hk_nodes)
        print(f"🔄 正在从 {current_node} 切换至 -> {new_node}")

        # 4. 发出切换。跨进程锁只负责避免并发写入，不再设置时间冷却。
        put_resp = requests.put(
            url,
            data=json.dumps({"name": new_node}),
            headers=headers,
            proxies=proxies_setting,
            timeout=10,
        )

        if put_resp.status_code not in (200, 204):
            print(f"❌ 香港 IP 切换失败，状态码: {put_resp.status_code}")
            return {
                "switched": False,
                "reason": "switch_failed",
                "status_code": put_resp.status_code,
            }

        print("✅ 香港 IP 切换成功；当前未设置时间冷却")
        return {
            "switched": True,
            "reason": "switched",
            "current_node": current_node,
            "new_node": new_node,
            "remaining_seconds": 0,
        }

    except Exception as e:
        print(f"❌ 运行时报错: {e}")
        return {
            "switched": False,
            "reason": "exception",
            "error": str(e),
        }
    finally:
        switch_lock.release()


def get_public_ip():
    urls = [
        "https://api.ipify.org?format=json",
        "https://myip.ipip.net",
        "http://ip-api.com/json?lang=zh-CN",
    ]

    print("--- 正在检测公网 IP ---")
    try:
        # 使用 ip-api.com 可以看到地理位置信息
        response = requests.get("http://ip-api.com/json?lang=zh-CN", timeout=5)
        data = response.json()

        if data["status"] == "success":
            print(f"当前 IP: {data['query']}")
            print(f"所在地: {data['country']} {data['regionName']} {data['city']}")
            print(f"运营商: {data['isp']}")
        else:
            print("无法获取详细地理位置信息")

    except Exception as e:
        print(f"获取失败: {e}")


def switch_node(group_name, target_node_keyword):
    """
    group_name: 策略组全名，例如 "🔰 选择节点"
    target_node_keyword: 你想切到的节点关键词，例如 "日本Z01"
    """
    clash_api_url, clash_secret = _resolve_clash_api_settings()
    base_url = f"{clash_api_url}/proxies"
    headers = {**_clash_headers(clash_secret), "Content-Type": "application/json"}

    # 1. 先获取该组下所有可选节点的准确名称
    try:
        resp = requests.get(
            f"{base_url}/{group_name}",
            headers=headers,
            proxies={"http": None, "https": None},
        )
        if resp.status_code != 200:
            print(f"❌ 找不到策略组: {group_name}")
            return

        all_nodes = resp.json().get("all", [])

        # 2. 匹配关键词（因为节点名通常包含表情和特殊符号，全匹配很麻烦）
        matched_node = None
        for node in all_nodes:
            if target_node_keyword in node:
                matched_node = node
                break

        if not matched_node:
            print(f"❓ 在组中没找到包含 '{target_node_keyword}' 的节点")
            return

        # 3. 执行切换
        payload = {"name": matched_node}
        put_resp = requests.put(
            f"{base_url}/{group_name}",
            data=json.dumps(payload),
            headers=headers,
            proxies={"http": None, "https": None},
        )

        if put_resp.status_code == 204:
            print(f"✅ 已成功将 [{group_name}] 切换至: {matched_node}")
        else:
            print(f"❌ 切换失败: {put_resp.text}")

    except Exception as e:
        print(f"⚠️ 发生错误: {e}")


def list_proxies():
    clash_api_url, clash_secret = _resolve_clash_api_settings()
    url = f"{clash_api_url}/proxies"
    headers = {**_clash_headers(clash_secret), "Content-Type": "application/json"}

    try:
        # 记得禁用系统代理，防止请求发往 Clash 导致死循环
        response = requests.get(
            url, headers=headers, proxies={"http": None, "https": None}, timeout=5
        )

        if response.status_code != 200:
            print(f"❌ 获取失败，状态码: {response.status_code}")
            return

        data = response.json()
        proxies = data.get("proxies", {})

        print("=" * 50)
        print(f"{'策略组名称':<20} | {'当前选择节点':<20}")
        print("-" * 50)

        # 遍历所有对象
        for name, info in proxies.items():
            # 我们只关心策略组（Selector）或 自动测速组（URLTest）
            if info.get("type") in ["Selector", "URLTest"]:
                current_node = info.get("now", "Unknown")
                print(f"{name:<20} | {current_node:<20}")

                # 列出该组下前 5 个可选节点（避免刷屏）
                all_nodes = info.get("all", [])
                if all_nodes:
                    node_preview = ", ".join(all_nodes[:20])
                    print(f"  └─ 可选节点({len(all_nodes)}个): {node_preview} ...")
                print("-" * 50)

    except Exception as e:
        print(f"⚠️ 运行时出错: {e}")


# --- 使用示例 ---
# 注意：名称必须与 Clash 界面中看到的完全一致（区分大小写）
if __name__ == "__main__":
    # list_proxies()
    switch_random_hongkong_node()
    # switch_node("🔰 选择节点", "香港Z05")
    get_public_ip()
