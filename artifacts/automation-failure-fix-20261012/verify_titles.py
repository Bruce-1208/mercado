"""Read production candidates and test matching in an isolated disposable table."""
import json
import time
import uuid
from pathlib import Path
from bit import bit_mysql
from erp import product_title_index as index, rights_holder_policy as policy

c = bit_mysql.pymysql.connect(**{**bit_mysql.config, 'read_timeout': 15})
output = {}
test_table = 'automation_title_regression_' + uuid.uuid4().hex
try:
    with c.cursor() as q:
        q.execute(f'CREATE TABLE `{test_table}` ('
                  'id BIGINT PRIMARY KEY, organization_key VARCHAR(64), title TEXT, '
                  'source_snapshot_json LONGTEXT, review_status VARCHAR(32), '
                  "rights_title_hash BINARY(16) AS (UNHEX(MD5(TRIM(title)))) VIRTUAL, "
                  "rights_title_es_hash BINARY(16) AS (UNHEX(MD5(TRIM(JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(source_snapshot_json),source_snapshot_json,'{}'),'$.ai_original.title_es')))))) VIRTUAL, "
                  "rights_title_pt_hash BINARY(16) AS (UNHEX(MD5(TRIM(JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(source_snapshot_json),source_snapshot_json,'{}'),'$.ai_original.title_pt')))))) VIRTUAL, "
                  + ','.join(f'INDEX `{name}` (organization_key, `{column}`)' for name,column in zip(index.INDEXES,index.COLUMNS)) + ') CHARACTER SET utf8mb4')
        q.executemany(f'INSERT INTO `{test_table}` (id,organization_key,title,source_snapshot_json) VALUES (%s,%s,%s,%s)', [
            (1,'a',' 商品 Á ',None), (2,'a','other',json.dumps({'ai_original':{'title_es':'商品 Á'}})),
            (3,'a','other',json.dumps({'ai_original':{'title_pt':'商品 Á'}})),
            (4,'b','商品 Á',None), (5,'a','商品 á',None), (6,'a','商品 A','invalid'),
            (7,'a','other','invalid'), (8,'a','商品 Á',json.dumps({'ai_original':{'title_es':'商品 Á'}})),
        ])
        original_table, original_available = policy.products.PRODUCT_TABLE, index.available
        try:
            policy.products.PRODUCT_TABLE = test_table
            index.available = lambda _: True
            rows = policy.lock_matching_products(q, 'a', '商品 Á', 'id')
            assert [r['id'] for r in rows] == [1,2,3,8], rows
            assert policy.lock_matching_products(q, 'a', 'missing', 'id') == []
            output['isolated_table_exact_matching'] = 'passed: original/es/pt, whitespace, case, accents, organization, duplicate candidates, invalid JSON, absent title'
        finally:
            policy.products.PRODUCT_TABLE, index.available = original_table, original_available
    c.rollback()
    with c.cursor() as q:
        output['product_indexes_ready'] = index.available(q)
        output['link_index_ready'] = index.links_available(q)
        if output['product_indexes_ready']:
            branches = [f'SELECT id FROM `{policy.products.PRODUCT_TABLE}` FORCE INDEX (`{name}`) WHERE organization_key=%s AND `{column}`=UNHEX(MD5(%s))' for name,column in zip(index.INDEXES,index.COLUMNS)]
            title = 'Mexico National Team Jersey Set'
            query, args = ' UNION ALL '.join(branches), ('wuhan-zeshun',title)*3
            start = time.monotonic()
            q.execute(query,args)
            output['production_candidate_count'] = len(q.fetchall())
            output['production_candidate_seconds'] = round(time.monotonic()-start,4)
            q.execute('EXPLAIN '+query,args)
            output['production_plan'] = [{k:r[k] for k in ('type','key','rows','Extra')} for r in q.fetchall()]
    c.rollback()
    if output['link_index_ready']:
        start = time.monotonic()
        rows = policy.same_title_links('Mexico National Team Jersey Set','wuhan-zeshun')
        output['production_link_seconds'] = round(time.monotonic()-start,4)
        output['production_link_count'] = len(rows)
finally:
    with c.cursor() as q:
        q.execute(f'DROP TABLE IF EXISTS `{test_table}`')
    c.close()
print(json.dumps(output,ensure_ascii=False,indent=2))
Path('artifacts/automation-failure-fix-20261012/verification.json').write_text(json.dumps(output,ensure_ascii=False,indent=2))
