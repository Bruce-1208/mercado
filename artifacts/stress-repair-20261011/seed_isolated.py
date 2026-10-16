"""Clone table definitions only; generate entirely synthetic order data locally."""
import json, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import pymysql
from bit.db_pool import default_connection_config
TABLES=['mercado_synced_orders','mercado_shipment_costs','mercado_store_tokens','mercado_store_site_settings','unified_yandex_stores','erp_mercadolibre_exchange_rate_daily','erp_mercadolibre_exchange_rates','erp_mercadolibre_store_links']
source=pymysql.connect(**default_connection_config())
local=pymysql.connect(host='127.0.0.1',port=13307,user='root',charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor)
with local.cursor() as q:
 q.execute('CREATE DATABASE IF NOT EXISTS stress_test')
 q.execute('USE stress_test')
 with source.cursor() as r:
  for table in TABLES:
   r.execute(f'SHOW CREATE TABLE `{table}`')
   ddl=r.fetchone()['Create Table']
   q.execute(ddl.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ',1))
   q.execute(f'SHOW COLUMNS FROM `{table}`')
   required=[row['Field'] for row in q.fetchall() if row['Null']=='NO' and row['Default'] is None and 'auto_increment' not in row['Extra']]
   print(table,required)
source.close();local.close()
