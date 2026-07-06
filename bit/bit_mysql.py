from datetime import datetime, timedelta

import pymysql

# 1. 配置数据库连接信息
config = {
    'host': '192.168.1.11',
    'user': 'mercado',
    'password': 'mercado',
    'database': 'mercado',
    'charset': 'utf8mb4',
    'port': 3306,
    'cursorclass': pymysql.cursors.DictCursor  # 让查询结果以字典形式返回
}
# config = {
#     'host': 'c766667e.natappfree.cc',
#     'user': 'root',
#     'password': 'zzw@951208',
#     'database': 'mercado',
#     'charset': 'utf8mb4',
#     'port': 39181,
#     'cursorclass': pymysql.cursors.DictCursor  # 让查询结果以字典形式返回
# }
# yuming=c766667e.natappfree.cc:39181


def _ensure_column(cursor, table_name, column_name, column_definition):
    cursor.execute(f"SHOW COLUMNS FROM `{table_name}` LIKE %s", (column_name,))
    if cursor.fetchone():
        return
    cursor.execute(f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` {column_definition}")


def _parse_number(value):
    text = str(value or "").replace(",", "").strip()
    number_text = "".join(ch for ch in text if ch.isdigit() or ch in ".-")
    try:
        return float(number_text) if number_text else 0
    except ValueError:
        return 0


def mysql_demo():
    # 建立连接
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            # --- 增 (Create) ---
            sql_insert = "INSERT INTO `users` (`username`, `email`) VALUES (%s, %s)"
            cursor.execute(sql_insert, ("Gemini", "gemini@example.com"))
            print(f"新增记录ID: {cursor.lastrowid}")

            # --- 查 (Read) ---
            sql_select = "SELECT * FROM `users` WHERE `username` = %s"
            cursor.execute(sql_select, ("Gemini",))
            result = cursor.fetchone()
            print(f"查询结果: {result}")

            # --- 改 (Update) ---
            sql_update = "UPDATE `users` SET `email` = %s WHERE `username` = %s"
            cursor.execute(sql_update, ("new_gemini@example.com", "Gemini"))
            print(f"修改行数: {cursor.rowcount}")

            # --- 删 (Delete) ---
            sql_delete = "DELETE FROM `users` WHERE `username` = %s"
            cursor.execute(sql_delete, ("Gemini",))
            print(f"删除行数: {cursor.rowcount}")

        # 核心：涉及写操作（增删改）必须提交事务
        connection.commit()
        print("事务已提交")

    except Exception as e:
        # 发生错误则回滚
        connection.rollback()
        print(f"操作失败，已回滚: {e}")
    finally:
        # 关闭连接
        connection.close()


def insert_task_record(record_list):
    # 建立连接
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            # --- 增 (Create) ---
            sql_insert = "insert into record (type,name,site,isSuccess,datetime) VALUES (%s,%s,%s,%s,%s)"
            cursor.executemany(sql_insert, record_list)
            print("执行sql成功", sql_insert)

        # 核心：涉及写操作（增删改）必须提交事务
        connection.commit()

    except Exception as e:
        # 发生错误则回滚
        connection.rollback()
        print(f"操作失败，已回滚: {e}")
    finally:
        # 关闭连接
        connection.close()


def inset_reputation_info(reputation_list):
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            _ensure_column(cursor, "reputation", "一周流量趋势", "TEXT NULL")
            submit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            normalized_list = []
            for row in reputation_list:
                row = list(row)
                if len(row) == 12:
                    row = row[:6] + row[7:11] + [row[11]]
                elif len(row) == 6:
                    color, orders, complain, delay, name, site = row
                    row = [name, site, color, orders, complain, delay, "", "", "", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ""]
                elif len(row) == 10:
                    row.append("")
                elif len(row) > 11:
                    row = row[:11]
                if len(row) == 11:
                    row.append(submit_time)
                elif len(row) == 12:
                    row[11] = submit_time
                normalized_list.append(row)
            print(f"准备插入声誉记录 {len(normalized_list)} 条，提交时间 {submit_time}")

            # --- 增 (Create) ---
            sql_insert = """
    INSERT INTO reputation (
         店铺名, 站点, 声誉颜色, 总单量, 
        投诉率, 延误率, 增加或减少, 近七天变化率, 
        系统告警, 更新时间, 一周流量趋势, 提交时间
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
            cursor.executemany(sql_insert, normalized_list)
            print("执行sql成功", sql_insert)

        # 核心：涉及写操作（增删改）必须提交事务
        connection.commit()

    except Exception as e:
        # 发生错误则回滚
        connection.rollback()
        print(f"操作失败，已回滚: {e}")
    finally:
        # 关闭连接
        connection.close()


def inset_infraction_info(infraction_list):
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            normalized_list = []
            submit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for row in infraction_list:
                row = list(row)
                if len(row) == 6:
                    row.insert(5, "")
                    row.append("侵权")
                elif len(row) == 7:
                    row.append("侵权")
                if len(row) >= 8:
                    row[5] = submit_time
                normalized_list.append(row)
            submit_time_count = sum(1 for row in normalized_list if len(row) > 5 and row[5])
            print(f"准备插入侵权记录 {len(normalized_list)} 条，提交时间 {submit_time}，非空 {submit_time_count} 条")

            # --- 增 (Create) ---
            sql_insert = """
    INSERT INTO infraction (
         店铺名,站点,编号,标题,侵权时间,提交时间,执行时间,类型

    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """
            cursor.executemany(sql_insert, normalized_list)
            print("执行sql成功", sql_insert)

        # 核心：涉及写操作（增删改）必须提交事务
        connection.commit()

    except Exception as e:
        # 发生错误则回滚
        connection.rollback()
        print(f"操作失败，已回滚: {e}")
    finally:
        # 关闭连接
        connection.close()


def _parse_infraction_date(value):
    text = str(value or "").strip()
    if not text:
        return None

    formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%m/%d/%y",
        "%m/%d/%Y",
        "%d/%m/%y",
        "%d/%m/%Y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%d %B %Y",
    ]
    candidates = []
    for fmt in formats:
        try:
            candidates.append(datetime.strptime(text, fmt))
        except ValueError:
            pass

    if not candidates:
        return None

    now = datetime.now()
    not_future = [item for item in candidates if item <= now]
    return max(not_future or candidates)


def get_latest_infraction_info(recent_days=30):
    try:
        recent_days = int(recent_days)
    except (TypeError, ValueError):
        recent_days = 30
    if recent_days not in (7, 30, 100, 365):
        recent_days = 30

    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            _ensure_column(cursor, "reputation", "一周流量趋势", "TEXT NULL")
            cursor.execute(
                """
                SELECT MAX(`提交时间`) AS latest_submit_time
                FROM infraction
                WHERE `提交时间` IS NOT NULL AND `提交时间` <> ''
                """
            )
            latest = cursor.fetchone() or {}
            latest_submit_time = latest.get("latest_submit_time")
            if not latest_submit_time:
                return {"latest_submit_time": "", "total": 0, "rows": []}

            cursor.execute(
                """
                SELECT
                    `店铺名`, `站点`, `编号`, `标题`, `侵权时间`,
                    `提交时间`, `执行时间`, `类型`
                FROM infraction
                WHERE `提交时间` = %s
                """,
                (latest_submit_time,),
            )
            rows = cursor.fetchall()
            cutoff = datetime.now() - timedelta(days=recent_days)
            recent_rows = []
            for row in rows:
                infraction_date = _parse_infraction_date(row.get("侵权时间"))
                if infraction_date and infraction_date >= cutoff:
                    row["_侵权时间排序"] = infraction_date
                    recent_rows.append(row)
            recent_rows.sort(
                key=lambda row: row.get("_侵权时间排序") or datetime.min,
                reverse=True,
            )
            for row in recent_rows:
                row.pop("_侵权时间排序", None)

            summary_map = {}
            for row in recent_rows:
                key = (row.get("店铺名") or "", row.get("站点") or "")
                item = summary_map.setdefault(
                    key,
                    {
                        "店铺名": key[0],
                        "站点": key[1],
                        "总数": 0,
                        "侵权": 0,
                        "权利人": 0,
                    },
                )
                item["总数"] += 1
                infraction_type = row.get("类型") or "侵权"
                if infraction_type == "权利人":
                    item["权利人"] += 1
                else:
                    item["侵权"] += 1

            summary = sorted(
                summary_map.values(),
                key=lambda item: (item["总数"], item["侵权"], item["权利人"]),
                reverse=True,
            )

            return {
                "latest_submit_time": str(latest_submit_time),
                "latest_total": len(rows),
                "recent_days": recent_days,
                "total": len(recent_rows),
                "summary": summary,
                "rows": recent_rows,
            }
    except Exception as e:
        print(f"查询最新侵权数据失败: {e}")
        raise
    finally:
        connection.close()


def get_latest_reputation_info():
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            _ensure_column(cursor, "reputation", "一周流量趋势", "TEXT NULL")
            cursor.execute(
                """
                SELECT MAX(`提交时间`) AS latest_submit_time
                FROM reputation
                WHERE `提交时间` IS NOT NULL
                """
            )
            latest = cursor.fetchone() or {}
            latest_submit_time = latest.get("latest_submit_time")
            if not latest_submit_time:
                return {"latest_submit_time": "", "total": 0, "rows": []}

            cursor.execute(
                """
                SELECT
                    `店铺名`, `站点`, `声誉颜色`, `总单量`, `投诉率`,
                    `延误率`, `增加或减少`, `近七天变化率`,
                    `系统告警`, `更新时间`, `一周流量趋势`, `提交时间`
                FROM reputation
                WHERE `提交时间` = %s
                """,
                (latest_submit_time,),
            )
            rows = cursor.fetchall()
            for row in rows:
                for key in ("更新时间", "提交时间"):
                    if row.get(key) is not None:
                        row[key] = str(row[key])
            rows.sort(
                key=lambda row: (
                    -_parse_number(row.get("总单量")),
                    str(row.get("店铺名") or ""),
                    str(row.get("站点") or ""),
                )
            )
            return {
                "latest_submit_time": str(latest_submit_time),
                "total": len(rows),
                "rows": rows,
            }
    except Exception as e:
        print(f"查询最新声誉数据失败: {e}")
        raise
    finally:
        connection.close()


def insert_orders(line):
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            # --- 增 (Create) ---
            sql_insert = """
            INSERT INTO orders (
                `id`, `编号`, `时间`, `业务员`, `来源`, `状态`, 
                `金额`, `费用`, `退款`, `人民币收入`, `采购成本`, `采购单号`, 
                `采购追踪`, `利润`, `产品id`, `产品分类`, `标题`, 
                `图片`, `数量`, `订单运费`,`订单备注`, `地区`, `买家姓名`
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            cursor.executemany(sql_insert, line)
            print("执行sql成功", sql_insert)

        # 核心：涉及写操作（增删改）必须提交事务
        connection.commit()

    except Exception as e:
        # 发生错误则回滚
        connection.rollback()
        print(f"操作失败，已回滚: {e}")
    finally:
        # 关闭连接
        connection.close()


def inset_delay_info(delay_list):
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            # --- 增 (Create) ---
            sql_insert = """
            INSERT INTO `delay` (
    `店铺`, 
    `站点`, 
    `延误率`, 
    `下单时间`, 
    `销售单号`, 
    `订单标题`, 
    `截止延误时间`, 
    `实际揽收时间`, 
    `更新时间`, 
    `文件路径`
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
);    
    """
            cursor.executemany(sql_insert, delay_list)
            print("执行sql成功", sql_insert)

        # 核心：涉及写操作（增删改）必须提交事务
        connection.commit()

    except Exception as e:
        # 发生错误则回滚
        connection.rollback()
        print(f"操作失败，已回滚: {e}")
    finally:
        # 关闭连接
        connection.close()


def insert_chat_info(name, site, message, chat, response, time):
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            # --- 增 (Create) ---
            sql_insert = """
                INSERT INTO `record_chat` (
        `店铺`, 
        `站点`, 
        `话术`, 
        `客服消息`, 
        `回复`, 
        `时间`
    ) VALUES (
        %s, %s, %s, %s, %s, %s
    );    
        """
            cursor.execute(sql_insert, (name, site, message, chat, response, time))
            chat_id = cursor.lastrowid
            print("执行sql成功", sql_insert)
        connection.commit()
        return chat_id

    except Exception as e:
        # 发生错误则回滚
        connection.rollback()
        print(f"操作失败，已回滚: {e}")
    finally:
        # 关闭连接
        connection.close()


if __name__ == "__main__":
    mysql_demo()
