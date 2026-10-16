import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import pymysql
from bit.db_pool import default_connection_config
from erp import mercadolibre_collection_store as s
c=pymysql.connect(**default_connection_config())
class Connection:
    def cursor(self): return c.cursor()
    def commit(self): c.commit()
    def rollback(self): c.rollback()
    def close(self): pass
try:
    with c.cursor() as q:
        q.execute('CREATE TEMPORARY TABLE test_pulled_owner_products LIKE erp_mercadolibre_products')
    s.PRODUCT_TABLE='test_pulled_owner_products'
    s.ensure_collection_tables=lambda q:None
    token={'id':7,'organization_key':'test-company','owner_name':'operator','owner_user_id':123,
           'site_settings':[{'site_id':'MLM','salesperson':'甲'},{'site_id':'MLB','salesperson':'乙'}]}
    items=[dict(id=site+'123',site_id=site,title='Test',permalink='https://example.com/'+site,
                thumbnail='https://example.com/p.jpg',price=10,currency_id='USD',category_id=site+'1')
           for site in ['MLM','MLB','MLC']]
    s.upsert_pulled_store_links_to_products(token,items,connection_factory=Connection)
    with c.cursor() as q:
        q.execute('SELECT source_item_id,owner_name,owner_user_id FROM test_pulled_owner_products ORDER BY source_item_id')
        assert [(r['source_item_id'],r['owner_name'],r['owner_user_id']) for r in q.fetchall()]==[('MLB123','乙',None),('MLC123','',None),('MLM123','甲',None)]
        q.execute("UPDATE test_pulled_owner_products SET source_type='collected',owner_name='采集者',owner_user_id=99 WHERE source_item_id='MLB123'")
    token['site_settings']=[{'site_id':'MLM','salesperson':'丙'},{'site_id':'MLB','salesperson':'丁'}]
    s.upsert_pulled_store_links_to_products(token,items,connection_factory=Connection)
    with c.cursor() as q:
        q.execute('SELECT source_item_id,owner_name,owner_user_id FROM test_pulled_owner_products ORDER BY source_item_id')
        assert [(r['source_item_id'],r['owner_name'],r['owner_user_id']) for r in q.fetchall()]==[('MLB123','采集者',99),('MLC123','',None),('MLM123','丙',None)]
    print('PASS: per-site owners, unassigned site, resync reassignment, preserve collected owner (isolated MySQL temporary table)')
finally: c.close()
