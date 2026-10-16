"""Only a disposable local synthetic database; no credentials or copied records."""
import os,sys,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
os.environ.update(MYSQL_HOST='127.0.0.1',MYSQL_PORT='13326',MYSQL_USER='root',MYSQL_PASSWORD='',MYSQL_DATABASE='stress_test',BIT_BACKGROUND_SERVICES_DISABLED='1')
import pymysql
from erp import mercadolibre_collection_store as products, mercadolibre_store_link_store as links, mercadolibre_prohibited_store as prohibited, mercadolibre_infraction_store as infractions
from bit import bit_inventory as inventory,bit_mysql as db
c=pymysql.connect(host='127.0.0.1',port=13326,user='root',database='stress_test',charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor)
counts={}
def insert(table, rowfn, count):
 with c.cursor() as q:
  q.execute(f'SELECT COUNT(*) AS n FROM `{table}`')
  existing=q.fetchone()['n']
  if existing==count:
   counts[table]=count;return
  if existing:raise RuntimeError('Refusing partial fixture table '+table)
  keys=list(rowfn(0));sql=f'INSERT INTO `{table}` ('+','.join('`'+k+'`' for k in keys)+') VALUES ('+','.join(['%s']*len(keys))+')'
  for start in range(0,count,500):
   q.executemany(sql,[tuple(rowfn(i)[k] for k in keys) for i in range(start,min(start+500,count))]);c.commit()
 counts[table]=count;print(table,count,flush=True)
from datetime import datetime
now=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
with c.cursor() as q:
 for f in (products.ensure_collection_tables,links.ensure_store_link_table,prohibited.ensure_prohibited_tables,infractions.ensure_infraction_tables,inventory.ensure_inventory_tables,db._ensure_mercado_action_center_tables,db._ensure_appeal_phrases_table,db._ensure_infringement_knowledge_table,db._ensure_weight_dimensions_record_table):f(q)
c.commit()
insert(products.TASK_TABLE,lambda i:dict(id=i+1,requested_count=20000,source_url='https://example.invalid',created_by='Synthetic',status='completed'),1)
insert(products.COLLECTION_TABLE,lambda i:dict(task_id=1,source_item_id=f'fixture{i:08}',source_url='https://example.invalid',title=f'Synthetic product {i}',price=20,weight_g=300,collected_at=now,source_json=json.dumps({'padding':'x'*2000}),organization_key='synthetic'),20000)
insert(products.PRODUCT_TABLE,lambda i:dict(collection_item_id=i+1,source_item_id=f'fixture{i:08}',source_url='https://example.invalid',title=f'Synthetic product {i}',price=20,weight_g=300,category_id='CBT123',source_type='ai_original' if i%2 else 'collected',review_status='approved',last_publish_token_id=i%20+1,added_at=now,organization_key='synthetic',source_snapshot_json=json.dumps({'ai_original':{'status':'completed','generated_by':f'Person{i%10}'},'padding':'x'*2000})),20000)
insert(products.PUBLISH_RECORD_TABLE,lambda i:dict(batch_id=f'batch{i//50}',product_item_id=i+1,source_item_id=f'fixture{i:08}',token_id=i%20+1,store_name=f'Test {i%20+1}',site_id='MLM',status='success',title=f'Synthetic product {i}'),20000)
insert(links.STORE_LINK_TABLE,lambda i:dict(token_id=i%20+1,store_name=f'Test {i%20+1}',seller_id='synthetic',site_id='MLM',item_id=f'fixture{i:08}',title=f'Synthetic product {i}',status='active',sold_quantity=i%100,last_synced_at=now,remote_json=json.dumps({'padding':'x'*2000})),20000)
insert(prohibited.PROHIBITED_TABLE,lambda i:dict(token_id=i%20+1,store_name=f'Test {i%20+1}',seller_id='synthetic',site_id='MLM',item_id=f'fixture{i:08}',title=f'Synthetic product {i}',infraction_reason='Synthetic prohibited item',sync_marker='test',last_checked_at=now,infraction_date=now),10000)
insert(infractions.INFRACTION_TABLE,lambda i:dict(token_id=i%20+1,store_name=f'Test {i%20+1}',site_id='MLM',source_type='detection',source_id=str(i),item_id=f'fixture{i:08}',title=f'Synthetic product {i}',last_checked_at=now,occurred_at=now,reason='Synthetic reason',status='active'),10000)
insert(inventory.SHELF_TABLE,lambda i:dict(code=f'S{i}',name=f'Shelf {i}',created_at=now,updated_at=now),20)
insert(inventory.STOCK_TABLE,lambda i:dict(shelf_id=i%20+1,order_id=f'test{i:08}',product_id=f'fixture{i:08}',product_name=f'Synthetic product {i}',quantity=i%10+1,unit_cost=10,first_inbound_at=now,last_inbound_at=now,created_at=now,updated_at=now),10000)
insert(inventory.MOVEMENT_TABLE,lambda i:dict(stock_id=i+1,shelf_id=i%20+1,movement_type='inbound',order_id=f'test{i:08}',product_id=f'fixture{i:08}',product_name=f'Synthetic product {i}',occurred_at=now,quantity=1,before_quantity=0,after_quantity=1,created_at=now),10000)
c.close();Path(__file__).with_name('fixture-counts.json').write_text(json.dumps(counts,indent=2))
