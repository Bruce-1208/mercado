"""500 synthetic database accounts; no production credentials or app startup."""
import ast,json,sys
from pathlib import Path
import pymysql
ROOT=Path(__file__).resolve().parents[2]
source=ast.parse((ROOT/'bit/bit_interface.py').read_text())
connection=pymysql.connect(host='127.0.0.1',port=13327,user='root',database='stress_test',charset='utf8mb4')
with connection.cursor() as cursor:
 for table in ['workbench_roles','workbench_users']:
  sql=next(n.value for n in ast.walk(source) if isinstance(n,ast.Constant) and isinstance(n.value,str) and n.value.strip().startswith('CREATE TABLE IF NOT EXISTS `'+table+'`'))
  cursor.execute(sql)
 for role in ['member','super_admin']:
  cursor.execute("INSERT INTO workbench_roles (organization_key,role_key,role_name,permissions_json,own_store_only,is_system,created_at,updated_at) VALUES ('capacity-test',%s,%s,'[\"orders\"]',0,0,NOW(),NOW()) ON DUPLICATE KEY UPDATE role_name=VALUES(role_name)",(role,role))
 for uid in range(1,501):
  cursor.execute("INSERT INTO workbench_users (id,username,password_hash,organization_key,role_key,is_active,created_at,updated_at) VALUES (%s,%s,'disabled-synthetic-password','capacity-test',%s,1,NOW(),NOW()) ON DUPLICATE KEY UPDATE organization_key=VALUES(organization_key),role_key=VALUES(role_key)",(uid,f'capacity-{uid}', 'super_admin' if uid%10==0 else 'member'))
 cursor.execute("SELECT COUNT(*) FROM workbench_users WHERE organization_key='capacity-test'");print(json.dumps({'synthetic_database_accounts':cursor.fetchone()[0]}))
connection.commit();connection.close()
