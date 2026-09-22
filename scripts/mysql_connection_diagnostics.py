"""Read-only MySQL connection snapshot: python -m scripts.mysql_connection_diagnostics."""

import json

from bit.db_pool import close_all_pools, get_db_connection


def snapshot():
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SHOW GLOBAL STATUS WHERE Variable_name IN "
                "('Uptime', 'Threads_connected', 'Threads_running', 'Max_used_connections', "
                "'Connection_errors_max_connections', 'Aborted_clients', 'Aborted_connects')"
            )
            status = {row["Variable_name"]: row["Value"] for row in cursor.fetchall()}
            cursor.execute(
                "SHOW GLOBAL VARIABLES WHERE Variable_name IN "
                "('max_connections', 'max_user_connections', 'wait_timeout')"
            )
            variables = {row["Variable_name"]: row["Value"] for row in cursor.fetchall()}
            cursor.execute(
                "SELECT USER AS user, SUBSTRING_INDEX(HOST, ':', 1) AS client_host, "
                "COMMAND AS command, COUNT(*) AS connections, MAX(TIME) AS max_seconds "
                "FROM information_schema.PROCESSLIST "
                "GROUP BY USER, SUBSTRING_INDEX(HOST, ':', 1), COMMAND "
                "ORDER BY connections DESC"
            )
            return {
                "status_since_server_start": status,
                "variables": variables,
                "sessions": cursor.fetchall(),
                "note": "无 PROCESS 权限时仅能看到当前账号的会话；诊断连接也计入数量。",
            }
    finally:
        connection.close()


def main():
    try:
        print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
    finally:
        close_all_pools()


if __name__ == "__main__":
    main()
