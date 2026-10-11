"""Fail closed with a recoverable HTTP response when MySQL is unavailable."""
import logging

import pymysql
from flask import g, jsonify, request

from bit.db_pool import PoolAcquireTimeout


def unavailable_response(error):
    unavailable = isinstance(error, PoolAcquireTimeout) or (
        isinstance(error, pymysql.InterfaceError) and error.args and error.args[0] == 0
    ) or (
        isinstance(error, pymysql.OperationalError) and error.args
        and error.args[0] in {1040, 2002, 2003, 2006, 2013, 2055}
    )
    if not unavailable:
        return None
    # Response hooks must not try to reload authorization from the failed DB.
    # Keep the signed session: temporary outages must not log everyone out.
    g.workbench_database_unavailable = True
    response = jsonify(status="error", code="database_unavailable",
                       message="数据库暂时不可用，请稍后重试")
    response.status_code = 503
    response.headers["Cache-Control"] = "no-store"
    if request.method in {"GET", "HEAD"}:
        response.headers["Retry-After"] = "2"
    return response


def install_database_error_handlers(app):
    def handle(error):
        response = unavailable_response(error)
        logging.error("Database request failed: %s", type(error).__name__, exc_info=True)
        if response is not None:
            return response
        # SQL/programming failures must remain visible as server errors.
        return jsonify(status="error", message="数据库请求失败，请联系管理员"), 500

    for error_type in (PoolAcquireTimeout, pymysql.OperationalError, pymysql.InterfaceError):
        app.register_error_handler(error_type, handle)
