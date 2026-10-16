import ast,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
import pymysql
c=pymysql.connect(host='127.0.0.1',port=13326,user='root',database='stress_test',charset='utf8mb4')
# Use the exact CREATE TABLE literal from current service, without importing startup jobs.
tree=ast.parse((ROOT/'bit/bit_interface.py').read_text())
sql=next(n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str) and 'CREATE TABLE IF NOT EXISTS `workbench_users`' in n.value)
with c.cursor() as q:
 q.execute(sql)
 q.execute("UPDATE erp_mercadolibre_products SET source_type='ai_original' WHERE source_type='1688'")
c.commit();c.close()
