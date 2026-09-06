"""Standalone local console, without a MySQL dependency."""
import argparse
from pathlib import Path

from flask import Flask

from .config import data_dir
from .service import Service
from .web import create_blueprint


def main():
    parser = argparse.ArgumentParser(description="泽顺 AI核重核价 本地独立启动")
    parser.add_argument("--port", type=int, default=5018)
    parser.add_argument("--data-dir", type=Path, default=data_dir())
    parser.add_argument("--run", action="store_true", help="启动后恢复处理已有任务")
    args = parser.parse_args()
    app = Flask(__name__)
    service = Service(args.data_dir)
    app.register_blueprint(create_blueprint(service))
    app.add_url_rule("/", view_func=lambda: __import__("flask").redirect("/ai-weight-price"))
    if args.run:
        service.start()
    print(f"AI核重核价：http://127.0.0.1:{args.port}/ai-weight-price")
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
