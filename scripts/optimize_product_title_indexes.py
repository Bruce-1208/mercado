"""Install online exact-title indexes: python -m scripts.optimize_product_title_indexes."""
from bit import bit_mysql
from erp.product_title_index import install, install_links


def main():
    connection = bit_mysql.pymysql.connect(**{**bit_mysql.config, 'read_timeout': 7200})
    try:
        with connection.cursor() as cursor:
            cursor.execute('SET SESSION lock_wait_timeout=15')
        install(connection)
        install_links(connection)
        print('Exact product title indexes ready.', flush=True)
    finally:
        connection.close()


if __name__ == '__main__':
    main()
