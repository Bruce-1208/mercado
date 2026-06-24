import time
from flask import Flask, Response, request, render_template, jsonify

from bit.bit_appeal import *
from bit.bit_utils import *
from bit.bit_api import *

# 引入数据库入库需要的模块
import logging
from decimal import Decimal
from datetime import datetime
from db_pool import get_db_connection  # 确保你的连接池文件在这个目录下

app = Flask(__name__)

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# 1. 核心逻辑方法：改造成生成器
def shensu_logic(name, site, form, message):
    i = 0
    while i < 10:
        i = i + 1
        try:
            yield f"{get_now_time()}--- 任务启动第{i}次：{name}{site} ---<br>"
            shensu(name, site, form, message)
            # 模拟自动化操作步骤
            yield f"{get_now_time()}✅ {name}{site}申诉执行完毕,！<br>"
        except Exception as e:
            yield f"发生错误: {str(e)}<br>"
        finally:
            yield f"{get_now_time()}{name}{site}关闭浏览器等待十分钟，进行下一次申诉<br>"
            window_id = getWindowidByName(name)
            time.sleep(600)


# 2. 接口路由
@app.route("/api/run_shensu", methods=["GET"])
def api_run_shensu():
    # 获取前端传入的参数
    name = request.args.get("name", "")
    site = request.args.get("site", "")
    form = request.args.get("form", "")
    message = request.args.get("message", "")

    # 返回流式响应，mimetype 设为 text/html 或 text/event-stream
    return Response(shensu_logic(name, site, form, message), mimetype="text/html")


@app.route("/")
def index():
    return render_template('index.html')


# 定义路由和返回内容
@app.route("/zs")
def hello_whzs():
    return "武汉泽顺"


# --- 新增：1688大模型找货数据插入接口 ---
@app.route('/api/v1/records', methods=['POST'])
def insert_record():
    # 获取客户端发送的 JSON 数据
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "Missing JSON payload"}), 400

    # 安全提取 product_id（必填项项校验）
    product_id = data.get('product_id')
    if not product_id:
        return jsonify({"status": "error", "message": "Field 'product_id' is required"}), 422

    # 提取其余字段，并设置默认值（与你的数据表结构严格对应）
    zhiying_category = data.get('zhiying_category', None)
    original_img_url = data.get('original_img_url', None)
    is_same_style = int(data.get('is_same_style', 0))
    title = data.get('title', None)
    identified_weight = int(data.get('identified_weight', 0))
    pre_modified_weight = int(data.get('pre_modified_weight', 0))
    post_modified_weight = int(data.get('post_modified_weight', 0))

    # 金额与置信度转换为 Decimal 类型，防止精度丢失
    pre_modified_cost_usd = Decimal(str(data.get('pre_modified_cost_usd', '0.0000')))
    post_modified_cost_usd = Decimal(str(data.get('post_modified_cost_usd', '0.0000')))
    max_sku_price_cny = Decimal(str(data.get('max_sku_price_cny', '0.00')))
    model_confidence = Decimal(str(data.get('model_confidence', '0.00')))

    max_sku_spec = data.get('max_sku_spec', None)
    max_sku_id = data.get('max_sku_id', None)
    weight_issue = data.get('weight_issue', None)
    matched_1688_url = data.get('matched_1688_url', None)
    reason = data.get('reason', None)

    sql = """
        INSERT INTO product_mapping_records (
            crawl_time, zhiying_category, original_img_url, is_same_style, 
            product_id, title, identified_weight, pre_modified_weight, 
            post_modified_weight, pre_modified_cost_usd, post_modified_cost_usd, 
            max_sku_price_cny, max_sku_spec, max_sku_id, model_confidence, 
            weight_issue, matched_1688_url, reason
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
    """

    current_time = datetime.now()
    params = (
        current_time, zhiying_category, original_img_url, is_same_style,
        product_id, title, identified_weight, pre_modified_weight,
        post_modified_weight, pre_modified_cost_usd, post_modified_cost_usd,
        max_sku_price_cny, max_sku_spec, max_sku_id, model_confidence,
        weight_issue, matched_1688_url, reason
    )

    conn = None
    cursor = None
    try:
        # 从连接池中取得一条连接
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()

        logging.info(f"成功录入产品数据，Product ID: {product_id}")
        return jsonify({
            "status": "success",
            "message": "Record inserted successfully",
            "id": cursor.lastrowid
        }), 201

    except Exception as e:
        if conn:
            conn.rollback()
        logging.error(f"数据库写入失败: {str(e)}")
        return jsonify({"status": "error", "message": f"Database error: {str(e)}"}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()  # 将连接放回连接池


if __name__ == '__main__':
    # 保持 5000 端口，多线程模式开启以防流式阻塞
    app.run(host='0.0.0.0', port=5000, threaded=True)
