import json
from datetime import datetime,timedelta
import pymysql
c=pymysql.connect(host='127.0.0.1',port=13307,user='root',database='stress_test',charset='utf8mb4')
with c.cursor() as q:
 q.execute('SELECT COUNT(*) FROM mercado_synced_orders')
 if q.fetchone()[0]:raise SystemExit('Synthetic data already present')
 for i in range(1,21):
  q.execute("INSERT INTO mercado_store_tokens (id,display_name,access_token,created_at,updated_at) VALUES (%s,%s,'synthetic-not-a-token',NOW(),NOW())",(i,f'Test {i}'))
  q.execute("INSERT INTO mercado_store_site_settings (token_id,site_id,salesperson,group_name,created_at,updated_at) VALUES (%s,'MLM',%s,%s,NOW(),NOW())",(i,f'Person {i%3}',f'Group {i%2}'))
 q.execute("INSERT INTO unified_yandex_stores (id,name,active) VALUES (-1,'Synthetic Yandex',1)")
 q.execute("INSERT INTO erp_mercadolibre_exchange_rates (from_currency_id,to_currency_id,rate,refreshed_at,expires_at) VALUES ('MXN','USD',0.055,NOW(),NOW()),('USD','CNY',7,NOW(),NOW())")
 q.execute("INSERT INTO erp_mercadolibre_exchange_rate_daily (from_currency_id,to_currency_id,rate_date,rate,refreshed_at) VALUES ('MXN','USD','2026-01-01',0.054,NOW()),('USD','CNY','2026-01-01',6.9,NOW())")
 columns='order_id,token_id,shop_name,seller_id,raw_json,first_synced_at,synced_at,date_created,site_id,country,status,status_label,workflow_status,status_detail,shipping_id,freight_source,freight,freight_currency_id,quoted_freight_source,quoted_freight_local,quoted_freight_currency_id,total_amount,paid_amount,currency_id,amount_currency_id,sale_fee,purchase_cost,purchase_order,purchase_remark,transfer_warehouse,product_id,title,quantity'
 rows=[];costs=[]
 for i in range(80000):
  token=i%20+1; yandex=i%101==0
  raw={'pack_id':f'pack{i//2}','_shipment_status':'delivered' if i%7==0 else 'ready_to_ship','order_items':[{'item':{'id':f'product{i%500}','title':'Synthetic product'},'quantity':1}],'padding':'z'*2400}
  row=(f'test{i:08}',-1 if yandex else token,f'Test {token}','test',json.dumps(raw),datetime(2026,1,1),datetime(2026,1,1),datetime(2026,1,1)+timedelta(minutes=i),'YANDEX' if yandex else 'MLM','墨西哥','shipped' if i%5==0 else 'paid','已发' if i%5==0 else '找货','交付' if i%11==0 else None,json.dumps({'shipment_status':'delivered'}) if i%13==0 else 'normal',f'ship{i}','shipment_costs' if i%4==0 else None,5,'USD','order_shipping_cost',4,'USD',100+i%30,5+i%3,'USD','MXN',1,10+i%9 if i%6 else None,f'1688-{i}','remark','shahu' if i%2 else 'yiyun',f'product{i%500}','Synthetic product',1)
  rows.append(row)
  if i%4==0:costs.append((f'ship{i}',token,'delivered' if i%3 else 'ready_to_ship'))
  if len(rows)==1000:
   q.executemany('INSERT INTO mercado_synced_orders ('+columns+') VALUES ('+','.join(['%s']*len(row))+')',rows);rows=[]
   q.executemany('INSERT INTO mercado_shipment_costs (shipping_id,token_id,shipment_status,checked_at) VALUES (%s,%s,%s,NOW())',costs);costs=[]
   c.commit()
 print('Seeded 80000 synthetic orders')
c.close()
