import time
from flask import Flask, Response, request, render_template

from bit_appeal import *

app = Flask(__name__)


# 1. 核心逻辑方法：改造成生成器
def shensu_logic(name, site, form, message):
    yield f"--- 任务启动：{name} ---<br>"
    shensu(name, site, form, message)
    # 模拟自动化操作步骤
    time.sleep(1)
    yield f"【1/4】正在连接站点：{site}...<br>"

    time.sleep(2)
    yield f"【2/4】正在定位表单：{form}<br>"

    time.sleep(1.5)
    yield f"【3/4】提交申诉内容：{message[:10]}...<br>"

    # 这里可以放真实的 driver 操作代码
    # driver.get(site) ...

    time.sleep(1)
    yield "【4/4】✅ 申诉执行完毕！<br>"


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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)