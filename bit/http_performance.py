"""Transport optimizations that leave authenticated HTML/API cache policy intact."""
import gzip

from flask import request


_TEXT_TYPES = frozenset((
    "text/html", "text/css", "text/plain", "text/javascript", "application/javascript",
    "application/json", "application/xml", "text/xml", "image/svg+xml",
))
_PUBLIC_STATIC_SUFFIXES = frozenset((
    "css", "js", "png", "jpg", "jpeg", "webp", "gif", "svg", "ico", "woff", "woff2",
))


def install_http_performance(app):
    """Install once; only public static assets gain a browser freshness lifetime.

    Text compression is bounded to 4 MiB and never consumes an application stream.
    Versioned assets receive one day of freshness, with validators retained, so a
    accidentally unchanged version does not make an asset stale indefinitely.
    """
    if app.extensions.get("zeshun_http_performance"):
        return
    app.extensions["zeshun_http_performance"] = True

    @app.after_request
    def improve_transport(response):
        static = request.endpoint == "static"
        if static and response.status_code in (200, 304):
            filename = str((request.view_args or {}).get("filename") or "")
            suffix = filename.rsplit(".", 1)[-1].lower()
            if request.args.get("v") and suffix in _PUBLIC_STATIC_SUFFIXES:
                response.headers["Cache-Control"] = "public, max-age=86400"
            if response.status_code == 304 and response.mimetype in _TEXT_TYPES:
                response.vary.add("Accept-Encoding")

        if (request.method != "GET" or response.status_code != 200
                or response.mimetype not in _TEXT_TYPES
                or response.headers.get("Content-Encoding")
                or response.headers.get("Content-Range") or request.headers.get("Range")
                or response.cache_control.no_transform):
            return response
        # Flask's send_file wrapper is a stream too. It is safe to materialize
        # only bounded static text files; API/event/download streams stay intact.
        if response.is_streamed and not static:
            return response
        length = response.content_length
        if length is not None and not 1024 <= length <= 4 * 1024 * 1024:
            return response
        if static and (length is None or length > 4 * 1024 * 1024):
            return response
        response.vary.add("Accept-Encoding")
        if request.accept_encodings["gzip"] <= 0:
            return response
        response.direct_passthrough = False
        original = response.get_data()
        if not 1024 <= len(original) <= 4 * 1024 * 1024:
            return response
        compressed = gzip.compress(original, compresslevel=5, mtime=0)
        if len(compressed) >= len(original):
            return response
        response.set_data(compressed)
        response.headers["Content-Encoding"] = "gzip"
        etag, _weak = response.get_etag()
        if etag:
            response.set_etag(etag, weak=True)
        return response
