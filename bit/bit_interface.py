import queue
import sys
import threading
import time
import traceback
from flask import Flask, Response, request, render_template, jsonify

from bit.bit_appeal import *
from bit.bit_utils import *
from bit.bit_api import *
from bit.bit_mysql import insert_chat_info

# 引入数据库入库需要的模块
import logging
from decimal import Decimal
from datetime import datetime
from db_pool import get_db_connection  # 确保你的连接池文件在这个目录下
from decimal import Decimal, InvalidOperation

app = Flask(__name__)

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# 1. 核心逻辑方法：改造成生成器
_original_stdout = sys.stdout
_original_stderr = sys.stderr
_thread_log_queues = {}
_thread_log_lock = threading.Lock()


class ThreadLogStream:
    def __init__(self, original_stream):
        self.original_stream = original_stream

    def write(self, text):
        if text:
            if isinstance(text, bytes):
                encoding = getattr(self.original_stream, "encoding", None) or "utf-8"
                text = text.decode(encoding, errors="replace")
            with _thread_log_lock:
                output_queue = _thread_log_queues.get(threading.get_ident())
            if output_queue:
                output_queue.put(text)
            else:
                self.original_stream.write(text)

    def flush(self):
        self.original_stream.flush()

    def isatty(self):
        return self.original_stream.isatty()

    @property
    def encoding(self):
        return getattr(self.original_stream, "encoding", "utf-8")

    def __getattr__(self, name):
        return getattr(self.original_stream, name)


sys.stdout = ThreadLogStream(_original_stdout)
sys.stderr = ThreadLogStream(_original_stderr)


def register_thread_log_queue(output_queue):
    with _thread_log_lock:
        _thread_log_queues[threading.get_ident()] = output_queue


def unregister_thread_log_queue():
    with _thread_log_lock:
        _thread_log_queues.pop(threading.get_ident(), None)


def format_log_text(text):
    return str(text).replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")


def shensu_logic_old(name, site, form, message):
    i = 0
    while (i < 10):
        i = i + 1
        try:
            # yield f"{get_now_time()}--- 任务启动第{i}次：{name}{site} ---<br>"
            shensu(name, site, form, message)
            # 模拟自动化操作步骤
            # yield f"{get_now_time()}✅ {name}{site}申诉执行完毕,！<br>"
        except Exception as e:
            print(e)
        finally:
            print(f"{get_now_time()}{name}{site}关闭浏览器等待十分钟，进行下一次申诉")
            # window_id = getWindowidByName(name)

            time.sleep(600)


# 2. 接口路由
def shensu_logic_previous(name, site, form, message):
    for i in range(1, 11):
        output_queue = queue.Queue()

        def run_task():
            writer = StreamWriter(output_queue)
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    print(f"{get_now_time()} --- 任务启动第 {i} 次：{name} {site}")
                    shensu(name, site, form, message)
                    print(f"{get_now_time()} {name} {site} 申诉执行完毕")
                except Exception as e:
                    print(f"{get_now_time()} 发生错误: {str(e)}")
                    traceback.print_exc()
                finally:
                    output_queue.put(None)

        task_thread = threading.Thread(target=run_task, daemon=True)
        task_thread.start()

        while True:
            text = output_queue.get()
            if text is None:
                break
            yield format_log_text(text)
            sys.stdout.flush()

        yield f"{get_now_time()} {name} {site} 本轮结束，等待十分钟后进入下一轮\n"
        getWindowidByName(name)
        time.sleep(600)


def shensu_logic(name, site, form, message):
    for i in range(1, 11):
        output_queue = queue.Queue()

        def run_task():
            register_thread_log_queue(output_queue)
            try:
                print(f"{get_now_time()} --- 任务启动第 {i} 次：{name} {site}")
                shensu(name, site, form, message)
                print(f"{get_now_time()} {name} {site} 申诉执行完毕")
            except Exception as e:
                print(f"{get_now_time()} 发生错误: {str(e)}")
                traceback.print_exc()
            finally:
                unregister_thread_log_queue()
                output_queue.put(None)

        task_thread = threading.Thread(target=run_task, daemon=True)
        task_thread.start()

        while True:
            text = output_queue.get()
            if text is None:
                break
            yield format_log_text(text)
            sys.stdout.flush()

        yield f"{get_now_time()} {name} {site} 本轮结束，等待十分钟后进入下一轮\n"
        getWindowidByName(name)
        time.sleep(600)


@app.route('/api/run_shensu', methods=['GET'])
def api_run_shensu():
    # 获取前端传入的参数
    name = request.args.get('name', '')
    site = request.args.get('site', '')
    form = request.args.get('form', '')
    message = request.args.get('message', '')

    # 返回流式响应，mimetype 设为 text/html 或 text/event-stream
    response = Response(shensu_logic(name, site, form, message), mimetype='text/plain; charset=utf-8')
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.route('/')
def index():
    return render_template('index.html')


# 定义路由和返回内容
@app.route('/zs')
def hello_whzs():
    return "武汉泽顺"


from decimal import Decimal, InvalidOperation


def safe_decimal(value, default="0.00"):
    """安全地将输入转换为 Decimal，若为空、None、或非法格式则返回默认值"""
    if value is None:
        return Decimal(default)

    # 转为字符串并去掉两端空格
    clean_str = str(value).strip()

    # 拦截常见的空值或无效字符串
    if clean_str in ('', 'None', 'null', 'NaN', 'undefined'):
        return Decimal(default)

    try:
        return Decimal(clean_str)
    except InvalidOperation:
        # 如果还是解析失败（比如传入了 "abc"），则安全返回默认值
        return Decimal(default)

# --- 新增：1688大模型找货数据插入接口 ---
@app.route('/api/v1/chat', methods=['POST'])
def api_insert_chat_info():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error", "message": "Missing JSON payload"}), 400

    required_fields = ["name", "site", "message", "chat", "response", "time"]
    missing_fields = [field for field in required_fields if data.get(field) in (None, "")]
    if missing_fields:
        return jsonify({
            "status": "error",
            "message": "Missing required fields",
            "fields": missing_fields
        }), 422

    try:
        chat_id = insert_chat_info(
            data["name"],
            data["site"],
            data["message"],
            data["chat"],
            data["response"],
            data["time"]
        )
        return jsonify({
            "status": "success",
            "message": "Chat info inserted successfully",
            "id": chat_id
        }), 201
    except Exception as e:
        logging.error(f"Chat info insert failed: {str(e)}")
        return jsonify({"status": "error", "message": f"Database error: {str(e)}"}), 500


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
    identified_weight = int(data.get('identified_weight') or 0)
    pre_modified_weight = int(data.get('pre_modified_weight') or 0)
    post_modified_weight = int(data.get('post_modified_weight') or 0)

    # 金额与置信度转换为 Decimal 类型，防止精度丢失
    # pre_modified_cost_usd = Decimal(str(data.get('pre_modified_cost_usd', '0.0000')))
    # post_modified_cost_usd = Decimal(str(data.get('post_modified_cost_usd', '0.0000')))
    # max_sku_price_cny = Decimal(str(data.get('max_sku_price_cny', '0.00')))
    # model_confidence = Decimal(str(data.get('model_confidence', '0.00')))
    # pre_modified_cost_usd 和 post_modified_cost_usd 默认 4 位小数
    pre_modified_cost_usd = safe_decimal(data.get('pre_modified_cost_usd'), default='0.0000')
    post_modified_cost_usd = safe_decimal(data.get('post_modified_cost_usd'), default='0.0000')

    # max_sku_price_cny 和 model_confidence 默认 2 位小数
    max_sku_price_cny = safe_decimal(data.get('max_sku_price_cny'), default='0.00')
    model_confidence = safe_decimal(data.get('model_confidence'), default='0.00')

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
