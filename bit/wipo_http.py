"""HTTPS transport which connects only to already validated public addresses."""
from __future__ import annotations

import ipaddress
import socket
import sys

from requests.adapters import HTTPAdapter
from requests.exceptions import ProxyError
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError
from urllib3.util import connection


class _PublicHTTPSConnection(HTTPSConnection):
    def _new_conn(self):
        # A proxy would perform a separate, unvalidated origin lookup. The
        # WIPO transport uses direct connections, with trust_env disabled.
        if getattr(self, "_tunnel_host", None):
            raise NewConnectionError(self, "WIPO 数据源不支持代理连接")
        try:
            resolved = socket.getaddrinfo(
                getattr(self, "_dns_host", self.host), self.port,
                connection.allowed_gai_family(), socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise NewConnectionError(self, "无法解析 WIPO 数据源域名") from exc
        addresses = []
        for item in resolved:
            try:
                address = ipaddress.ip_address(item[4][0])
            except (ValueError, IndexError, TypeError) as exc:
                raise NewConnectionError(self, "WIPO 数据源解析地址无效") from exc
            if not address.is_global:
                raise NewConnectionError(self, "WIPO 数据源不能指向本机或内网地址")
            literal = str(address)
            if literal not in addresses:
                addresses.append(literal)
        if not addresses:
            raise NewConnectionError(self, "WIPO 数据源域名未返回有效地址")

        last_error = None
        for literal in addresses:
            try:
                # Only numeric addresses reach the connection helper, so its
                # getaddrinfo call cannot re-resolve/rebind the original name.
                sock = connection.create_connection(
                    (literal, self.port), self.timeout,
                    source_address=self.source_address, socket_options=self.socket_options,
                )
            except OSError as exc:
                last_error = exc
                continue
            # self.host remains the origin hostname: HTTPSConnection.connect
            # uses it for SNI/certificate verification, and HTTP uses it for Host.
            sys.audit("http.client.connect", self, self.host, self.port)
            return sock
        if isinstance(last_error, socket.timeout):
            raise ConnectTimeoutError(self, "WIPO 数据源连接超时") from last_error
        raise NewConnectionError(self, "无法连接 WIPO 数据源") from last_error


class PublicHTTPSAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

        class PublicHTTPSPool(HTTPSConnectionPool):
            ConnectionCls = _PublicHTTPSConnection

        # urllib3's default map is shared; copy it before changing this adapter.
        self.poolmanager.pool_classes_by_scheme = dict(self.poolmanager.pool_classes_by_scheme)
        self.poolmanager.pool_classes_by_scheme["https"] = PublicHTTPSPool

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        raise ProxyError("WIPO 数据源不支持代理连接")
