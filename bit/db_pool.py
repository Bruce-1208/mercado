# db_pool.py
import os

from dbutils.pooled_db import PooledDB
import pymysql

# 数据库连接池配置（根据你的实际 MySQL 服务器修改）
POOL = PooledDB(
    creator=pymysql,  # 使用 pymysql 连接数据库
    maxconnections=20, # 连接池允许的最大连接数
    mincached=5,       # 初始化时，连接池中至少创建的空闲连接数
    maxcached=10,      # 连接池中最多闲置的连接数
    blocking=True,     # 连接池中如果没有可用连接后，是否阻塞等待
    host=os.environ.get('MYSQL_HOST', os.environ.get('DB_HOST', '127.0.0.1')),
    port=int(os.environ.get('MYSQL_PORT', os.environ.get('DB_PORT', '3306'))),
    user=os.environ.get('MYSQL_USER', os.environ.get('DB_USER', 'mercado')),
    password=os.environ.get('MYSQL_PASSWORD', os.environ.get('DB_PASSWORD', 'mercado')),
    database=os.environ.get('MYSQL_DATABASE', os.environ.get('DB_NAME', 'mercado')),
    charset=os.environ.get('MYSQL_CHARSET', 'utf8mb4')
)

def get_db_connection():
    return POOL.connection()
