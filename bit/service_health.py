"""Read-only health evidence; no business jobs are started by probes."""
from datetime import datetime, timezone


def checked_at():
    return datetime.now(timezone.utc).isoformat()


def database_probe():
    from bit import bit_db_api
    if bit_db_api.DB_MODE != "mysql":
        result = bit_db_api.get_database_api_health().get("database_probe")
        if not isinstance(result, dict):
            return {"status": "unknown", "checked_at": None, "detail": "服务端未提供数据库查询检测"}
        return result
    from bit.bit_mysql import config
    import pymysql
    connection = pymysql.connect(**{**config, "connect_timeout": 3, "read_timeout": 3, "write_timeout": 3})
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 AS healthy")
            if cursor.fetchone() != {"healthy": 1}:
                raise RuntimeError("Unexpected database probe result")
    finally:
        connection.close()
    return {"status": "ok", "checked_at": checked_at(), "detail": "数据库连接及 SELECT 1 查询成功"}


def probe(name, operation, detail):
    try:
        result = operation()
        if isinstance(result, dict):
            return {"name": name, **result}
        return {"name": name, "status": "ok" if result else "error", "checked_at": checked_at(),
                "detail": detail if result else "健康检查未通过"}
    except Exception:
        # Connection errors may contain credentials or internal hostnames.
        return {"name": name, "status": "error", "checked_at": checked_at(), "detail": "检测失败：连接或查询未成功"}


def service_snapshot(yandex_probe):
    return {"services": [
        probe("工作台接口", lambda: True, "已收到已登录的健康检查请求"),
        probe("业务数据库", database_probe, ""),
        probe("Yandex 服务", yandex_probe, "健康接口响应及服务标识校验通过"),
    ]}
