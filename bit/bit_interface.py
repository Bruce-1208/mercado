import time
from flask import Flask, Response, request, render_template

from bit_appeal import *
from bit_utils import *
from bit_api import *

app = Flask(__name__)


# 1. 核心逻辑方法：改造成生成器
def shensu_logic(name, site, form, message):
    i = 0
    while (i < 10):
        i = i + 1
        try:
            yield f"{get_now_time()}--- 任务启动第{i}次：{name}{site} ---<br>"
            shensu(name, site, form, message)
            # 模拟自动化操作步骤
            yield f"{get_now_time()}✅ {name}{site}申诉执行完毕,！<br>"
        except Exception as e:
            yield e
        finally:
            yield f"{get_now_time()}{name}{site}关闭浏览器等待十分钟，进行下一次申诉"
            window_id = getWindowidByName(name)
            time.sleep(600)


# 2. 接口路由
@app.route('/api/run_shensu', methods=['GET'])
def api_run_shensu():
    # 获取前端传入的参数
    name = request.args.get('name', '')
    site = request.args.get('site', '')
    form = request.args.get('form', '')
    message = request.args.get('message', '')

    # 返回流式响应，mimetype 设为 text/html 或 text/event-stream
    return Response(shensu_logic(name, site, form, message), mimetype='text/html')


@app.route('/')
def index():
    return render_template('index.html')
# 定义路由和返回内容
@app.route('/zs')
def hello_whzs():
    return "武汉泽顺"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
