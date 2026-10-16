import os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
os.environ.update(MYSQL_HOST='127.0.0.1',MYSQL_PORT='13317',MYSQL_USER='root',MYSQL_PASSWORD='',MYSQL_DATABASE='stress_test',BIT_BACKGROUND_SERVICES_DISABLED='1')
import pymysql
from bit import bit_mysql as db,yandex_order_sync
from erp import mercadolibre_profitability_cache as rates,mercadolibre_store_link_store as links
c=pymysql.connect(host='127.0.0.1',port=13317,user='root',charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor)
with c.cursor() as q:
 q.execute('CREATE DATABASE stress_test');q.execute('USE stress_test')
 for f in [db._ensure_mercado_store_tokens_table,db._ensure_mercado_store_site_settings_table,db._ensure_mercado_synced_orders_table,yandex_order_sync.ensure_tables,rates.ensure_profitability_cache_tables,links.ensure_store_link_table]:f(q)
c.commit();c.close();print('Synthetic schema created from current source; no production connection')
