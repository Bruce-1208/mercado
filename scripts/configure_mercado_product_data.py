#!/usr/bin/env python3
"""Administrator-only import of an existing account's DevTools session capture.

The output is private JSON for ZESHUN_MERCADO_SESSION_FILE, never an extension asset.
"""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time
from contextlib import contextmanager


@contextmanager
def session_lock(target):
    lock = str(target) + ".lock"
    deadline = time.monotonic() + 3
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise ValueError("Session update is locked; retry after the current update completes")
            time.sleep(.025)
    try:
        yield
    finally:
        os.close(fd)
        os.unlink(lock)


def import_session(source, destination):
    data = json.loads(Path(source).read_text())
    entry = next((e for e in data.get("log", {}).get("entries", []) if
                  e.get("request", {}).get("url") == "https://meli.zying.net/api/MeliBest/QueryMeliSkuInfos"), None)
    if not entry:
        raise ValueError("Capture does not contain the product query")
    headers = {h["name"].lower(): h["value"] for h in entry["request"]["headers"]}
    if not headers.get("token"):
        raise ValueError("Capture does not contain an authenticated session")
    session = {key: headers[key] for key in ("token", "appclient", "version", "ext_version", "client_identify") if key in headers}
    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".session-")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(session, out)
        os.chmod(temp, 0o600)
        with session_lock(target):
            os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-har", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    import_session(args.session_har, args.output)
    print("Private session saved (0600); configure the server environment and restart its worker.")
