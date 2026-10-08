"""Transport checks use an isolated Flask application and no business services."""
import gzip

import pytest
from flask import Flask, Response

from bit.http_performance import install_http_performance


@pytest.fixture
def transport_app(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "test.js").write_text("// console module\n" * 300, encoding="utf-8")
    (static / "test.png").write_bytes(b"PNG" * 1000)
    app = Flask(__name__, static_folder=str(static), static_url_path="/static")
    app.config.update(TESTING=True, SEND_FILE_MAX_AGE_DEFAULT=0)

    @app.get("/")
    def index():
        return Response("<p>private user page</p>" * 300, mimetype="text/html",
                        headers={"Cache-Control": "no-store", "Vary": "Cookie"})

    @app.get("/stream")
    def stream():
        return Response(iter([b"x" * 2048]), mimetype="text/plain")

    @app.get("/encoded")
    def encoded():
        return Response(b"x" * 2048, mimetype="application/json",
                        headers={"Content-Encoding": "br"})

    @app.get("/no-transform")
    def no_transform():
        return Response(b"x" * 2048, mimetype="text/plain",
                        headers={"Cache-Control": "private, no-transform"})

    install_http_performance(app)
    install_http_performance(app)
    return app


def test_gzip_preserves_private_html_cache_policy_and_vary(transport_app):
    reply = transport_app.test_client().get("/", headers={"Accept-Encoding": "gzip"})
    assert reply.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(reply.data) == b"<p>private user page</p>" * 300
    assert reply.headers["Cache-Control"] == "no-store"
    assert set(reply.vary) == {"Cookie", "Accept-Encoding"}
    assert int(reply.headers["Content-Length"]) == len(reply.data)
    assert len(transport_app.after_request_funcs[None]) == 1


@pytest.mark.parametrize("accept", ["", "identity", "gzip;q=0, br", "gzip;q=0, *;q=1"])
def test_compression_obeys_negotiation(transport_app, accept):
    reply = transport_app.test_client().get("/", headers={"Accept-Encoding": accept})
    assert "Content-Encoding" not in reply.headers
    assert reply.data.startswith(b"<p>")
    assert "Accept-Encoding" in reply.vary


def test_versioned_static_caches_and_compresses_with_conditional_validation(transport_app):
    client = transport_app.test_client()
    identity = client.get("/static/test.js?v=release-1")
    compressed = client.get("/static/test.js?v=release-1", headers={"Accept-Encoding": "gzip"})
    assert compressed.headers["Cache-Control"] == "public, max-age=86400"
    assert gzip.decompress(compressed.data) == identity.data
    assert compressed.headers["ETag"].startswith("W/")
    conditional = client.get("/static/test.js?v=release-1", headers={
        "Accept-Encoding": "gzip", "If-None-Match": compressed.headers["ETag"],
    })
    assert conditional.status_code == 304
    assert not conditional.data
    assert conditional.headers["Cache-Control"] == "public, max-age=86400"
    assert "Accept-Encoding" in conditional.vary


def test_unversioned_assets_keep_existing_cache_policy_and_binary_not_compressed(transport_app):
    client = transport_app.test_client()
    unversioned = client.get("/static/test.js")
    assert unversioned.headers["Cache-Control"] == "no-cache, max-age=0"
    binary = client.get("/static/test.png?v=1", headers={"Accept-Encoding": "gzip"})
    assert "Content-Encoding" not in binary.headers
    assert binary.headers["Cache-Control"] == "public, max-age=86400"


@pytest.mark.parametrize("path", ["/stream", "/encoded", "/no-transform"])
def test_transport_leaves_stream_encoded_and_no_transform_responses(transport_app, path):
    reply = transport_app.test_client().get(path, headers={"Accept-Encoding": "gzip"})
    assert reply.headers.get("Content-Encoding") != "gzip"
    assert reply.data == b"x" * 2048


def test_range_and_head_do_not_compress(transport_app):
    client = transport_app.test_client()
    partial = client.get("/static/test.js?v=1", headers={"Range": "bytes=0-1023", "Accept-Encoding": "gzip"})
    assert partial.status_code == 206
    assert len(partial.data) == 1024
    assert "Content-Encoding" not in partial.headers
    head = client.head("/", headers={"Accept-Encoding": "gzip"})
    assert not head.data
    assert "Content-Encoding" not in head.headers
