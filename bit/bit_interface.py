import time
from flask import Flask, Response, request, render_template

app = Flask(__name__)


# 模拟你的申诉逻辑
def shensu(name, site, form, message):
    yield f"--- 任务启动：{name} ---<br>"

    # 模拟驱动初始化
    time.sleep(1)
    yield "【系统】正在初始化浏览器驱动...<br>"

    # 模拟站点访问
    time.sleep(2)
    yield f"【进度】成功访问站点：{site}<br>"

    # 模拟表单填写
    time.sleep(1.5)
    yield f"【操作】正在填写表单 {form} 并发送信息...<br>"

    # 模拟结果
    time.sleep(1)
    yield "【结果】✅ 申诉已提交成功！<br>"


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/shensu')
def api_shensu():
    # 获取前端传来的参数
    name = request.args.get('name')
    site = request.args.get('site')
    form = request.args.get('form')
    message = request.args.get('message')

    # 返回流式响应
    return Response(shensu(name, site, form, message), mimetype='text/html')


if __name__ == '__main__':
    app.run(debug=True, port=5000)