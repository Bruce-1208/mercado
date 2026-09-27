"""One persistent publishing concurrency setting shared by all accounts."""


def publish_workers(value=None):
    from bit.bit_mysql import config, pymysql

    if value is not None:
        if isinstance(value, bool) or str(value) != str(int(value)) or int(value) < 1:
            raise ValueError("上架并发必须是大于 0 的整数")
        value = int(value)
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS workbench_publish_concurrency (
                id TINYINT PRIMARY KEY, workers INT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            if value is not None:
                cursor.execute("""INSERT INTO workbench_publish_concurrency (id, workers)
                    VALUES (1, %s) ON DUPLICATE KEY UPDATE workers=VALUES(workers)""", (value,))
                connection.commit()
            cursor.execute("SELECT workers FROM workbench_publish_concurrency WHERE id=1")
            row = cursor.fetchone()
            return int(row['workers']) if row else 16
    finally:
        connection.close()
