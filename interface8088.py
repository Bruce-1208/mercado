from flask import Flask

app = Flask(__name__)


@app.route('/brucezhang')
def hello2():
    return 'Hello, World!'
@app.route('/')
def hello():
    return 'Hello, World!'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8088)  # 监听所有接口的8000端口from flask import Flask

app = Flask(__name__)




