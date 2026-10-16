import sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import pymysql
from bit.db_pool import default_connection_config
from scripts.backfill_pulled_product_salespeople import run
c=pymysql.connect(**default_connection_config())
try:
 with c.cursor() as q:
  for name,cols in {
   'erp_mercadolibre_products':'id BIGINT PRIMARY KEY, source_item_id VARCHAR(64), source_type VARCHAR(32), organization_key VARCHAR(64),owner_name VARCHAR(128) COLLATE utf8mb4_0900_ai_ci,owner_user_id BIGINT',
   'erp_mercadolibre_store_links':'id BIGINT PRIMARY KEY,token_id BIGINT,item_id VARCHAR(64),site_id VARCHAR(16)',
   'mercado_store_tokens':'id BIGINT PRIMARY KEY,organization_key VARCHAR(64)',
   'mercado_store_site_settings':'token_id BIGINT,site_id VARCHAR(16),salesperson VARCHAR(128)'
  }.items():q.execute(f'CREATE TEMPORARY TABLE {name} ({cols}) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci')
  q.execute("INSERT INTO mercado_store_tokens VALUES (1,'a'),(2,'a'),(3,'b')")
  q.execute("INSERT INTO mercado_store_site_settings VALUES (1,'MLM','甲'),(1,'MLB','乙'),(2,'MLM','丙'),(3,'MLM','丁')")
  q.execute("INSERT INTO erp_mercadolibre_products VALUES (1,'X','pulled','a',NULL,NULL),(2,'Y','pulled','a','operator',99),(3,'Z','pulled','a',NULL,NULL),(4,'X','pulled','b',NULL,NULL),(5,'C','collected','a','采集员',11),(6,'N','pulled','a','operator',99)")
  q.execute("INSERT INTO erp_mercadolibre_store_links VALUES (1,1,'X','MLM'),(2,1,'Y','MLB'),(3,1,'Z','MLM'),(4,2,'Z','MLM'),(5,3,'X','MLM'),(6,1,'C','MLM'),(7,1,'N','MLC')")
 with tempfile.TemporaryDirectory() as d:
  result=run(c,apply=True,audit_path=Path(d)/'audit.gz',batch_size=2)
  assert result['changed']==4 and result['ambiguous']==1,result
 with c.cursor() as q:
  q.execute('SELECT id,owner_name,owner_user_id FROM erp_mercadolibre_products ORDER BY id')
  assert [(r['owner_name'],r['owner_user_id']) for r in q.fetchall()]==[('甲',None),('乙',None),(None,None),('丁',None),('采集员',11),('',None)]
 print('PASS: backfill site/company boundaries, conflict protection, unassigned, collector preservation')
finally:c.close()
