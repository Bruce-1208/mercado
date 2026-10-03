import io
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3 import PoolManager
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

from bit import wipo_http
from bit.wipo_http import PublicHTTPSAdapter


def _address(literal):
    family = socket.AF_INET6 if ":" in literal else socket.AF_INET
    return (family, socket.SOCK_STREAM, 6, "", (literal, 443))


def _connection(adapter=None):
    adapter = adapter or PublicHTTPSAdapter()
    return adapter.poolmanager.connection_from_host("feed.example", 443, "https")._new_conn()


def test_public_transport_pins_literal_address_and_preserves_connection_options(monkeypatch):
    resolve = MagicMock(return_value=[_address("8.8.8.8")])
    sock = MagicMock()
    connect = MagicMock(return_value=sock)
    monkeypatch.setattr(wipo_http.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(wipo_http.connection, "create_connection", connect)
    conn = _connection()
    conn.timeout = 10
    conn.source_address = ("0.0.0.0", 0)
    assert conn._new_conn() is sock
    resolve.assert_called_once_with("feed.example", 443,
                                    wipo_http.connection.allowed_gai_family(), socket.SOCK_STREAM)
    connect.assert_called_once_with(("8.8.8.8", 443), 10,
                                   source_address=("0.0.0.0", 0), socket_options=conn.socket_options)
    assert conn.host == "feed.example"


@pytest.mark.parametrize("addresses", [
    ["127.0.0.1"], ["10.0.0.1"], ["::1"], ["169.254.169.254"],
    ["8.8.8.8", "192.168.0.1"], [],
])
def test_every_dns_result_is_checked_before_any_socket_connection(monkeypatch, addresses):
    monkeypatch.setattr(wipo_http.socket, "getaddrinfo",
                        lambda *_args, **_kwargs: [_address(address) for address in addresses])
    connect = MagicMock()
    monkeypatch.setattr(wipo_http.connection, "create_connection", connect)
    with pytest.raises(NewConnectionError):
        _connection()._new_conn()
    connect.assert_not_called()


def test_public_ipv6_and_fallback_never_re_resolve_origin(monkeypatch):
    resolve = MagicMock(return_value=[_address("8.8.8.8"), _address("2606:4700:4700::1111")])
    connect = MagicMock(side_effect=[OSError("first address unavailable"), MagicMock()])
    monkeypatch.setattr(wipo_http.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(wipo_http.connection, "create_connection", connect)
    _connection()._new_conn()
    resolve.assert_called_once()
    assert [call.args[0][0] for call in connect.call_args_list] == ["8.8.8.8", "2606:4700:4700::1111"]


def test_connect_timeout_preserves_urllib3_error_contract(monkeypatch):
    monkeypatch.setattr(wipo_http.socket, "getaddrinfo", lambda *_: [_address("8.8.8.8")])
    monkeypatch.setattr(wipo_http.connection, "create_connection",
                        MagicMock(side_effect=socket.timeout("timed out")))
    with pytest.raises(ConnectTimeoutError):
        _connection()._new_conn()


def test_adapter_does_not_mutate_other_pool_managers():
    ordinary = PoolManager()
    original = ordinary.pool_classes_by_scheme["https"]
    adapter = PublicHTTPSAdapter()
    other = PublicHTTPSAdapter()
    assert ordinary.pool_classes_by_scheme["https"] is original
    assert HTTPAdapter().poolmanager.pool_classes_by_scheme["https"] is original
    assert adapter.poolmanager.pool_classes_by_scheme["https"] is not original
    assert adapter.poolmanager.pool_classes_by_scheme["https"] is not other.poolmanager.pool_classes_by_scheme["https"]


class ResponseSocket:
    def __init__(self):
        self.sent = []

    def settimeout(self, timeout):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def makefile(self, mode, buffering=None):
        return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n[]")

    def close(self):
        pass


def test_real_requests_flow_keeps_tls_sni_and_host_while_connecting_literal(monkeypatch):
    sock = ResponseSocket()
    resolve = MagicMock(return_value=[_address("8.8.8.8")])
    connect = MagicMock(return_value=sock)
    tls = MagicMock(return_value=SimpleNamespace(socket=sock, is_verified=True))
    monkeypatch.setattr(wipo_http.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(wipo_http.connection, "create_connection", connect)
    monkeypatch.setattr("urllib3.connection._ssl_wrap_socket_and_match_hostname", tls)
    with requests.Session() as session:
        session.trust_env = False
        session.mount("https://", PublicHTTPSAdapter())
        response = session.get("https://feed.example/data", headers={"Authorization": "Bearer test-secret"})
        assert response.status_code == 200 and response.text == "[]"
    assert connect.call_args.args[0] == ("8.8.8.8", 443)
    assert tls.call_args.kwargs["server_hostname"] == "feed.example"
    assert tls.call_args.kwargs["cert_reqs"] != "CERT_NONE"
    wire = b"".join(sock.sent)
    assert b"Host: feed.example\r\n" in wire
    assert b"Authorization: Bearer test-secret\r\n" in wire
    resolve.assert_called_once()


def test_private_rebinding_result_blocks_real_request_before_credentials(monkeypatch):
    # A caller may already have validated a public answer. The adapter performs
    # its own lookup at connect time and refuses a changed/private answer.
    resolve = MagicMock(return_value=[_address("127.0.0.1")])
    connect = MagicMock()
    monkeypatch.setattr(wipo_http.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(wipo_http.connection, "create_connection", connect)
    with requests.Session() as session:
        session.trust_env = False
        session.mount("https://", PublicHTTPSAdapter())
        with pytest.raises(requests.ConnectionError):
            session.get("https://feed.example/data", headers={"Authorization": "Bearer test-secret"})
    connect.assert_not_called()
    resolve.assert_called_once()


def test_proxy_cannot_bypass_origin_address_validation(monkeypatch):
    connect = MagicMock()
    monkeypatch.setattr(wipo_http.connection, "create_connection", connect)
    with requests.Session() as session:
        session.trust_env = False
        session.mount("https://", PublicHTTPSAdapter())
        with pytest.raises(requests.exceptions.ProxyError):
            session.get("https://feed.example/data", proxies={"https": "http://proxy.example:8080"})
    connect.assert_not_called()
