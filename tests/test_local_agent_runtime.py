import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from local_agent import LocalAgent


class BundleResponse:
    def __init__(self, content, version, sha256):
        self.content = content
        self.headers = {
            "X-Business-Version": version,
            "X-Bundle-SHA256": sha256,
        }

    def raise_for_status(self):
        return None


class BundleSession:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return self.response


def make_bundle(version="business-v1", files=None):
    files = files or {"local_agent_worker.py": "print('worker')\n"}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        archive.writestr(
            "bundle-manifest.json",
            json.dumps({"version": version, "files": list(files)}),
        )
    content = buffer.getvalue()
    return content, hashlib.sha256(content).hexdigest()


def test_agent_downloads_verifies_and_atomically_activates_release(tmp_path):
    content, sha256 = make_bundle()
    config = SimpleNamespace(
        data_dir=tmp_path,
        server_url="https://workbench.example",
        agent_token="",
    )
    agent = LocalAgent(config)
    session = BundleSession(BundleResponse(content, "business-v1", sha256))
    agent.session = session

    release = agent.ensure_release({"version": "business-v1", "sha256": sha256})

    assert (release / "local_agent_worker.py").read_text(
        encoding="utf-8"
    ) == "print('worker')\n"
    current = json.loads((tmp_path / "current-release.json").read_text())
    assert current["version"] == "business-v1"
    assert agent.current_release == "business-v1"
    assert session.calls == 1

    assert agent.ensure_release({"version": "business-v1", "sha256": sha256}) == release
    assert session.calls == 1


def test_agent_rejects_bundle_when_hash_does_not_match(tmp_path):
    content, sha256 = make_bundle()
    config = SimpleNamespace(
        data_dir=tmp_path,
        server_url="https://workbench.example",
        agent_token="",
    )
    agent = LocalAgent(config)
    agent.session = BundleSession(BundleResponse(content + b"changed", "business-v1", sha256))

    with pytest.raises(RuntimeError, match="完整性校验失败"):
        agent.ensure_release({"version": "business-v1", "sha256": sha256})

    assert not (tmp_path / "current-release.json").exists()


def test_agent_rejects_zip_path_traversal(tmp_path):
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../outside.py", "bad")
    destination = tmp_path / "destination"
    destination.mkdir()
    config = SimpleNamespace(
        data_dir=tmp_path,
        server_url="https://workbench.example",
        agent_token="",
    )
    agent = LocalAgent(config)

    with pytest.raises(RuntimeError, match="不安全"):
        agent._safe_extract(archive_path, destination)

    assert not (tmp_path / "outside.py").exists()
