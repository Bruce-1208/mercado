"""Offline checks for preserving working downloads during Windows packaging."""
import hashlib
import io
import json
import struct
import zipfile

import pytest

from scripts import build_windows_agent as build


def pe_bytes(*, machine=0x8664, subsystem=2):
    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 128)
    data[128:132] = b"PE\0\0"
    struct.pack_into("<H", data, 132, machine)
    struct.pack_into("<H", data, 128 + 24 + 68, subsystem)
    return bytes(data)


def test_validate_windows_gui_executable(tmp_path):
    path = tmp_path / "agent.exe"
    path.write_bytes(pe_bytes())
    build.validate_pe(path)
    for data in [b"macOS", pe_bytes(machine=0xAA64), pe_bytes(subsystem=3)]:
        path.write_bytes(data)
        with pytest.raises(RuntimeError):
            build.validate_pe(path)


def test_wine_exit_zero_requires_actual_runtime_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "TOOLS", tmp_path)
    (tmp_path / "runtime-verified.json").write_text('{"ok": true}')
    monkeypatch.setattr(build, "run", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="运行依赖校验未通过"):
        build.verify_runtime()


def test_update_zip_preserves_registration_configuration(tmp_path, monkeypatch):
    executable = tmp_path / "agent.exe"
    executable.write_bytes(pe_bytes())
    (tmp_path / "install-agent-forever.ps1").write_text("# supervisor")
    monkeypatch.setattr(build, "ROOT", tmp_path)
    monkeypatch.setattr(build, "agent_version", lambda: "1.2.8")
    with zipfile.ZipFile(io.BytesIO(build.update_archive(executable))) as archive:
        assert archive.testzip() is None
        assert "local-agent.json" not in archive.namelist()
        assert archive.read("MercadoLocalAgent.exe") == executable.read_bytes()
        assert "start-agent.bat" in archive.namelist()
        assert "uninstall-agent.bat" in archive.namelist()
        assert "1.2.8" in archive.read("README-update.txt").decode()


def prepare_build(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "MercadoLocalAgent.exe").write_bytes(b"previous executable")
    (dist / "Zeshun-MercadoLocalAgent-update.zip").write_bytes(b"previous zip")
    monkeypatch.setattr(build, "ROOT", tmp_path)
    monkeypatch.setattr(build, "TOOLS", tools)
    monkeypatch.setattr(build, "STATE", tools / "state.json")
    monkeypatch.setattr(build, "setup", lambda: None)
    monkeypatch.setattr(build, "agent_version", lambda: "1.2.8")
    monkeypatch.setattr(build, "build_fingerprint", lambda: "new-fingerprint")
    def mock_compile(args, **kwargs):
        output = build.Path(str(args[args.index("--distpath") + 1])[2:].replace("\\", "/"))
        output.mkdir()
        (output / "MercadoLocalAgent.exe").write_bytes(pe_bytes())
    monkeypatch.setattr(build, "run", mock_compile)
    return dist


def test_failed_smoke_test_keeps_previous_downloads(tmp_path, monkeypatch):
    dist = prepare_build(tmp_path, monkeypatch)
    def fail(_):
        raise RuntimeError("smoke test failed")
    monkeypatch.setattr(build, "smoke_test", fail)
    with pytest.raises(RuntimeError, match="smoke test failed"):
        build.build()
    assert (dist / "MercadoLocalAgent.exe").read_bytes() == b"previous executable"
    assert (dist / "Zeshun-MercadoLocalAgent-update.zip").read_bytes() == b"previous zip"
    assert not build.STATE.exists()


def test_source_change_during_build_keeps_previous_downloads(tmp_path, monkeypatch):
    dist = prepare_build(tmp_path, monkeypatch)
    fingerprints = iter(["started", "changed"])
    monkeypatch.setattr(build, "build_fingerprint", lambda: next(fingerprints))
    monkeypatch.setattr(build, "smoke_test", lambda _: None)
    monkeypatch.setattr(build, "update_archive", lambda _: b"new zip")
    with pytest.raises(RuntimeError, match="源文件发生变化"):
        build.build()
    assert (dist / "MercadoLocalAgent.exe").read_bytes() == b"previous executable"
    assert not build.STATE.exists()


def test_business_change_does_not_rebuild_executable(tmp_path, monkeypatch):
    from bit import local_agent_bundle
    dist = prepare_build(tmp_path, monkeypatch)
    previous = {"fingerprint": "new-fingerprint",
                "exe_sha256": build.digest(dist / "MercadoLocalAgent.exe"),
                "zip_sha256": build.digest(dist / "Zeshun-MercadoLocalAgent-update.zip"),
                "business_version": "old-business"}
    build.STATE.write_text(json.dumps(previous))
    def unexpected():
        pytest.fail("business-only change must not rebuild the Windows runtime")
    monkeypatch.setattr(build, "setup", unexpected)
    monkeypatch.setattr(local_agent_bundle, "build_business_bundle", lambda _: {
        "version": "new-business", "sha256": hashlib.sha256(b"business").hexdigest(),
        "content": b"business"})
    build.build()
    assert (dist / "MercadoLocalAgent.exe").read_bytes() == b"previous executable"
    assert (dist / "Zeshun-Agent-business.zip").read_bytes() == b"business"
    assert json.loads(build.STATE.read_text())["business_version"] == "new-business"


@pytest.mark.parametrize('archive_state', ['valid', 'missing', 'corrupt'])
def test_unchanged_business_metadata_matches_published_zip(tmp_path, monkeypatch, archive_state):
    from bit import local_agent_bundle
    dist = prepare_build(tmp_path, monkeypatch)
    business_path = dist / 'Zeshun-Agent-business.zip'
    original = b'previous zip timestamps'
    if archive_state != 'missing':
        business_path.write_bytes(original if archive_state == 'valid' else b'corrupt')
    previous = {'fingerprint': 'new-fingerprint',
                'exe_sha256': build.digest(dist / 'MercadoLocalAgent.exe'),
                'zip_sha256': build.digest(dist / 'Zeshun-MercadoLocalAgent-update.zip'),
                'business_version': 'same-source',
                'business_sha256': hashlib.sha256(original).hexdigest()}
    build.STATE.write_text(json.dumps(previous))
    repacked = b'new zip timestamps'
    monkeypatch.setattr(local_agent_bundle, 'build_business_bundle', lambda _: {
        'version': 'same-source', 'sha256': hashlib.sha256(repacked).hexdigest(),
        'content': repacked})
    build.build()
    assert business_path.read_bytes() == (original if archive_state == 'valid' else repacked)
    state = json.loads(build.STATE.read_text())
    published = json.loads((dist / 'windows-agent-build.json').read_text())
    assert state['business_sha256'] == published['business_sha256'] == build.digest(business_path)
