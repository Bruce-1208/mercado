import ftplib
import base64
import io
import json
import subprocess
import sys
import zipfile
from datetime import date

import pytest

from bit import wipo_madrid


def _archive(xml, name="daily.xml"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, xml)
        archive.writestr("123.gif", b"image ignored")
    return stream.getvalue()


def _xml(*registrations):
    # This structure follows the actual ST.66 daily feed: ApplicationNumber
    # identifies the IR, while nested basic registration numbers are unrelated.
    records = []
    for number, brand in registrations:
        text = f"<WordMarkSpecification><MarkVerbalElementText>{brand}</MarkVerbalElementText></WordMarkSpecification>" if brand else ""
        records.append(f"""<TradeMark><RegistrationOfficeCode>WO</RegistrationOfficeCode>
          <ApplicationNumber>{number}</ApplicationNumber>
          <BasicRegistrationApplicationDetails><BasicRegistrationNumber>unrelated</BasicRegistrationNumber></BasicRegistrationApplicationDetails>
          {text}<GoodsServicesDetails><GoodsServices><ClassDescriptionDetails><ClassDescription><ClassNumber>09</ClassNumber></ClassDescription></ClassDescriptionDetails></GoodsServices></GoodsServicesDetails>
          <ApplicantDetails><Applicant><Name>Holder is not a brand</Name></Applicant></ApplicantDetails>
          </TradeMark>""")
    return ("""<?xml version="1.0" encoding="utf-8"?><Transaction xmlns="http://www.wipo.int/standards/XMLSchema/trademarks"><TradeMarkTransactionBody><TransactionContentDetails><TransactionData><TradeMarkDetails>"""
            + "".join(records) + "</TradeMarkDetails></TransactionData></TransactionContentDetails></TradeMarkTransactionBody></Transaction>").encode()


class FakeFTP:
    def __init__(self, packages):
        self.packages = packages
        self.commands, self.directories = [], []

    def cwd(self, directory):
        self.directories.append(directory)

    def nlst(self):
        return list(reversed(self.packages)) + ["ST96", "not-a-date.zip"]

    def retrbinary(self, command, callback, blocksize):
        self.commands.append(command)
        content = self.packages[command.removeprefix("RETR ")]
        if isinstance(content, Exception):
            raise content
        for index in range(0, len(content), blocksize):
            callback(content[index:index + blocksize])


class CachedFTP(FakeFTP):
    def __init__(self, packages, *, interrupt_after=None, chunk_size=None):
        super().__init__(packages)
        self.offsets = []
        self.interrupt_after, self.chunk_size = interrupt_after, chunk_size

    def voidcmd(self, command):
        assert command == "TYPE I"
        return "200 OK"

    def size(self, name):
        return len(self.packages[name])

    def retrbinary(self, command, callback, blocksize, rest=None):
        self.commands.append(command)
        self.offsets.append(rest)
        content = self.packages[command.removeprefix("RETR ")][rest or 0:]
        if self.interrupt_after is not None:
            callback(content[:self.interrupt_after])
            raise OSError("connection interrupted")
        width = self.chunk_size or blocksize
        for index in range(0, len(content), width):
            callback(content[index:index + width])


def test_official_xml_uses_current_mark_text_and_ir_not_holder_or_basic_number():
    rows = wipo_madrid.parse_madrid_archive(_archive(_xml(("123456", "Example Brand"), ("654321", ""))))
    assert [(row["brand_name"], row["registration_number"]) for row in rows] == [
        ("Example Brand", "123456"), ("", "654321")]
    assert rows[0]["country"] == "WO"
    assert rows[0]["nice_classes"] == "09"


def test_chronological_packages_keep_latest_ir_and_ignore_unpublished_weekend(monkeypatch):
    monkeypatch.setattr(wipo_madrid, "_today", lambda: date(2026, 9, 30))
    ftp = FakeFTP({"20260925.zip": _archive(_xml(("1", "Old Name"), ("2", "Other Brand"))),
                   "20260928.zip": _archive(_xml(("1", "New Name"), ("3", ""))),
                   "20261001.zip": _archive(_xml(("4", "Future")))})
    rows, summary = wipo_madrid.fetch_madrid_updates({"madrid_from_date": "2026-09-25"}, session=ftp)
    assert ftp.commands == ["RETR 20260925.zip", "RETR 20260928.zip"]
    assert {row["brand_name"] for row in rows} == {"New Name", "Other Brand"}
    assert all(row["list_type"] == "blacklist" and row["evidence_count"] == 0 for row in rows)
    detail = json.loads(next(row for row in rows if row["brand_name"] == "New Name")["source_detail"])
    assert detail["source"] == "WIPO Madrid Monitor"
    assert detail["registration"] == "1" and detail["update_date"] == "2026-09-28"
    assert summary["madrid_through_date"] == "2026-09-28"
    assert (summary["fetched"], summary["unique"], summary["skipped"], summary["packages"]) == (4, 2, 1, 2)


def test_no_new_package_returns_empty_without_advancing_cursor(monkeypatch):
    monkeypatch.setattr(wipo_madrid, "_today", lambda: date(2026, 9, 30))
    rows, summary = wipo_madrid.fetch_madrid_updates({"madrid_from_date": "2026-09-29"},
                                                    session=FakeFTP({"20260928.zip": b"unused"}))
    assert rows == [] and summary["packages"] == 0 and summary["madrid_through_date"] == ""


def test_service_batches_one_date_package_for_durable_backlog_progress(monkeypatch):
    monkeypatch.setattr(wipo_madrid, "_today", lambda: date(2026, 9, 30))
    ftp = FakeFTP({"20260925.zip": _archive(_xml(("1", "First"))),
                   "20260928.zip": _archive(_xml(("2", "Second")))})
    rows, summary = wipo_madrid.fetch_madrid_updates(
        {"madrid_from_date": "2026-09-25", "madrid_batch_packages": 1}, session=ftp)
    assert [row["brand_name"] for row in rows] == ["First"]
    assert summary["madrid_through_date"] == "2026-09-25" and summary["remaining_packages"] == 1


def test_same_brand_registration_group_uses_latest_update_date(monkeypatch):
    monkeypatch.setattr(wipo_madrid, "_today", lambda: date(2026, 9, 30))
    ftp = FakeFTP({"20260925.zip": _archive(_xml(("1", "Same Brand"), ("2", "Same Brand"))),
                   "20260928.zip": _archive(_xml(("1", "Same Brand")))})
    rows, _ = wipo_madrid.fetch_madrid_updates({"madrid_from_date": "2026-09-25"}, session=ftp)
    detail = json.loads(rows[0]["source_detail"])
    assert detail["registration"] == ["1", "2"]
    assert detail["update_date"] == "2026-09-28" and detail["update_date_from"] == "2026-09-25"


def test_byte_budget_stops_after_complete_package_with_resumable_cursor(monkeypatch):
    monkeypatch.setattr(wipo_madrid, "_today", lambda: date(2026, 9, 30))
    first = _archive(_xml(("1", "First Brand")))
    second = _archive(_xml(("2", "Second Brand")))
    ftp = FakeFTP({"20260925.zip": first, "20260928.zip": second})
    ftp.size = lambda name: len(ftp.packages[name])
    ftp.voidcmd = lambda command: "200 OK"
    monkeypatch.setattr(wipo_madrid, "MAX_DOWNLOAD_BYTES", len(first) + len(second) - 1)
    rows, summary = wipo_madrid.fetch_madrid_updates({"madrid_from_date": "2026-09-25"}, session=ftp)
    assert [row["brand_name"] for row in rows] == ["First Brand"]
    assert summary["madrid_through_date"] == "2026-09-25" and summary["packages"] == 1
    assert summary["has_more"] is True
    assert summary["remaining_packages"] == 1 and summary["has_more_packages"] is True


def test_figurative_only_updates_do_not_promote_holder_names(monkeypatch):
    monkeypatch.setattr(wipo_madrid, "_today", lambda: date(2026, 9, 30))
    rows, summary = wipo_madrid.fetch_madrid_updates({"madrid_from_date": "2026-09-28"},
        session=FakeFTP({"20260928.zip": _archive(_xml(("1", "")))}))
    assert rows == [] and summary["skipped"] == 1 and summary["madrid_through_date"] == "2026-09-28"


def test_retention_gap_and_disappearing_listed_archive_fail_without_rows(monkeypatch):
    monkeypatch.setattr(wipo_madrid, "_today", lambda: date(2026, 9, 30))
    with pytest.raises(ValueError, match="保留范围"):
        wipo_madrid.fetch_madrid_updates({"madrid_from_date": "2026-09-20"},
                                        session=FakeFTP({"20260928.zip": b"unused"}))
    ftp = FakeFTP({"20260925.zip": _archive(_xml(("1", "Example"))),
                   "20260928.zip": ftplib.error_perm("550 unavailable")})
    with pytest.raises(ValueError, match="日期包下载失败"):
        wipo_madrid.fetch_madrid_updates({"madrid_from_date": "2026-09-25"}, session=ftp)


@pytest.mark.parametrize("payload", [b'<!DOCTYPE Transaction [<!ENTITY x "bad">]><Transaction>&x;</Transaction>',
                                     '<!DOCTYPE Transaction><Transaction/>'.encode("utf-16")])
def test_xml_declarations_are_rejected_before_parser(payload):
    with pytest.raises(ValueError, match="DOCTYPE"):
        wipo_madrid.parse_madrid_archive(_archive(payload))


def test_archive_paths_and_size_bounds_are_enforced_without_extraction(monkeypatch):
    with pytest.raises(ValueError, match="非法文件路径"):
        wipo_madrid.parse_madrid_archive(_archive(_xml(("1", "Example")), "../outside.xml"))
    with pytest.raises(ValueError, match="有效的 ZIP"):
        wipo_madrid.parse_madrid_archive(b"not zip")
    monkeypatch.setattr(wipo_madrid, "MAX_XML_BYTES", 16)
    with pytest.raises(ValueError, match="大小上限"):
        wipo_madrid.parse_madrid_archive(_archive(_xml(("1", "Example"))))


def test_download_limit_is_checked_as_bytes_arrive(monkeypatch):
    monkeypatch.setattr(wipo_madrid, "_today", lambda: date(2026, 9, 30))
    monkeypatch.setattr(wipo_madrid, "MAX_ARCHIVE_BYTES", 5)
    with pytest.raises(ValueError, match="下载上限"):
        wipo_madrid.fetch_madrid_updates({"madrid_from_date": "2026-09-28"},
            session=FakeFTP({"20260928.zip": b"more than five bytes"}))


def test_archive_download_resumes_saved_prefix_and_reuses_completed_file(monkeypatch, tmp_path):
    monkeypatch.setattr(wipo_madrid, "_today", lambda: date(2026, 9, 30))
    content = _archive(_xml(("1", "Resumable Brand")))
    settings = {"madrid_from_date": "2026-09-28", "madrid_cache_dir": str(tmp_path)}
    with pytest.raises(ValueError, match="日期包下载失败"):
        wipo_madrid.fetch_madrid_updates(settings,
            session=CachedFTP({"20260928.zip": content}, interrupt_after=100))
    partial = tmp_path / "monitor-ST66/20260928.zip.part"
    assert partial.read_bytes() == content[:100]

    resumed = CachedFTP({"20260928.zip": content})
    rows, summary = wipo_madrid.fetch_madrid_updates(settings, session=resumed)
    assert resumed.offsets == [100]
    assert [row["brand_name"] for row in rows] == ["Resumable Brand"]
    assert summary["download_bytes"] == len(content) - 100
    assert summary["madrid_through_date"] == "2026-09-28"
    assert not partial.exists()
    assert partial.with_suffix("").read_bytes() == content

    retry = CachedFTP({"20260928.zip": content})
    _, summary = wipo_madrid.fetch_madrid_updates(settings, session=retry)
    assert retry.commands == [] and summary["download_bytes"] == 0


def test_process_exit_during_download_preserves_rest_offset(monkeypatch, tmp_path):
    monkeypatch.setattr(wipo_madrid, "_today", lambda: date(2026, 9, 30))
    content = _archive(_xml(("1", "Restart Brand")))
    cache = tmp_path / "monitor-ST66"
    cache.mkdir()
    script = """
import base64, os, sys, time
from pathlib import Path
from bit.wipo_madrid import _fetch_cached_archive
content = base64.b64decode(sys.argv[2])
class ExitingFTP:
    def retrbinary(self, command, callback, blocksize):
        callback(content[:100])
        os._exit(73)
_fetch_cached_archive(ExitingFTP(), '20260928.zip', len(content),
                     Path(sys.argv[1]), time.monotonic()+1800, len(content))
"""
    result = subprocess.run([sys.executable, "-c", script, str(cache),
                             base64.b64encode(content).decode()], check=False, timeout=10)
    assert result.returncode == 73
    assert (cache / "20260928.zip.part").read_bytes() == content[:100]
    resumed = CachedFTP({"20260928.zip": content})
    rows, _ = wipo_madrid.fetch_madrid_updates(
        {"madrid_from_date": "2026-09-28", "madrid_cache_dir": str(tmp_path)}, session=resumed)
    assert resumed.offsets == [100] and rows[0]["brand_name"] == "Restart Brand"


def test_deadline_keeps_partial_archive_and_exposes_resume_progress(monkeypatch, tmp_path):
    monkeypatch.setattr(wipo_madrid, "_today", lambda: date(2026, 9, 30))
    clock = iter((0, 1801))
    monkeypatch.setattr(wipo_madrid.time, "monotonic", lambda: next(clock))
    content = _archive(_xml(("1", "Slow Brand")))
    settings = {"madrid_from_date": "2026-09-28", "madrid_cache_dir": str(tmp_path)}
    with pytest.raises(wipo_madrid.MadridDownloadPending) as pending:
        wipo_madrid.fetch_madrid_updates(settings,
            session=CachedFTP({"20260928.zip": content}, chunk_size=100))
    assert pending.value.summary == {"download_bytes": 100, "cached_download_bytes": 100,
        "archive_bytes": len(content), "resume_pending": True, "source_mode": "madrid"}
    assert (tmp_path / "monitor-ST66/20260928.zip.part").read_bytes() == content[:100]
    assert "madrid_through_date" not in pending.value.summary
    assert str(tmp_path) not in str(pending.value)

    monkeypatch.setattr(wipo_madrid.time, "monotonic", lambda: 0)
    resumed = CachedFTP({"20260928.zip": content})
    _, summary = wipo_madrid.fetch_madrid_updates(settings, session=resumed)
    assert resumed.offsets == [100] and summary["madrid_through_date"] == "2026-09-28"


def test_cache_resume_falls_back_when_ftp_rejects_rest(monkeypatch, tmp_path):
    monkeypatch.setattr(wipo_madrid, "_today", lambda: date(2026, 9, 30))
    content = _archive(_xml(("1", "Fallback Brand")))
    cache = tmp_path / "monitor-ST66"
    cache.mkdir()
    (cache / "20260928.zip.part").write_bytes(content[:100])
    class NoRestFTP(CachedFTP):
        def retrbinary(self, command, callback, blocksize, rest=None):
            if rest:
                self.offsets.append(rest)
                raise ftplib.error_perm("502 REST unsupported")
            return super().retrbinary(command, callback, blocksize, rest)
    ftp = NoRestFTP({"20260928.zip": content})
    rows, summary = wipo_madrid.fetch_madrid_updates(
        {"madrid_from_date": "2026-09-28", "madrid_cache_dir": str(tmp_path)}, session=ftp)
    assert ftp.offsets == [100, None]
    assert rows[0]["brand_name"] == "Fallback Brand"
    assert summary["download_bytes"] == len(content)
    assert (cache / "20260928.zip").read_bytes() == content


def test_download_cache_removes_only_dates_older_than_success_cursor(monkeypatch, tmp_path):
    monkeypatch.setattr(wipo_madrid, "_today", lambda: date(2026, 9, 30))
    cache = tmp_path / "monitor-ST66"
    cache.mkdir()
    for name in ("20260925.zip", "20260928.zip.part", "20260929.zip.part", "unrelated.txt"):
        (cache / name).write_bytes(b"saved")
    rows, _ = wipo_madrid.fetch_madrid_updates(
        {"madrid_from_date": "2026-09-29", "madrid_cache_dir": str(tmp_path)},
        session=CachedFTP({"20260928.zip": b"not fetched"}))
    assert rows == []
    assert sorted(path.name for path in cache.iterdir()) == ["20260929.zip.part", "unrelated.txt"]


def test_completed_cache_size_mismatch_is_redownloaded(monkeypatch, tmp_path):
    monkeypatch.setattr(wipo_madrid, "_today", lambda: date(2026, 9, 30))
    content = _archive(_xml(("1", "Verified Brand")))
    cache = tmp_path / "monitor-ST66"
    cache.mkdir()
    (cache / "20260928.zip").write_bytes(content[:100])
    ftp = CachedFTP({"20260928.zip": content})
    rows, summary = wipo_madrid.fetch_madrid_updates(
        {"madrid_from_date": "2026-09-28", "madrid_cache_dir": str(tmp_path)}, session=ftp)
    assert ftp.offsets == [None] and summary["download_bytes"] == len(content)
    assert rows[0]["brand_name"] == "Verified Brand"


def test_download_progress_reports_received_file_bytes_and_completed_reuse(monkeypatch, tmp_path):
    monkeypatch.setattr(wipo_madrid, "_today", lambda: date(2026, 9, 30))
    content = _archive(_xml(("1", "Progress Brand")))
    progress = []
    settings = {"madrid_from_date": "2026-09-28", "madrid_cache_dir": str(tmp_path),
                "madrid_download_progress": lambda saved, expected: progress.append((saved, expected))}
    wipo_madrid.fetch_madrid_updates(settings,
        session=CachedFTP({"20260928.zip": content}, chunk_size=100))
    assert progress[0] == (100, len(content)) and progress[-1] == (len(content), len(content))
    assert all(saved <= len(content) and expected == len(content) for saved, expected in progress)
    progress.clear()
    wipo_madrid.fetch_madrid_updates(settings, session=CachedFTP({"20260928.zip": content}))
    assert progress == [(len(content), len(content))]


def test_fixed_official_connection_and_directory_fallback(monkeypatch):
    monkeypatch.setattr(wipo_madrid, "_today", lambda: date(2026, 9, 30))
    ftp = FakeFTP({"20260928.zip": _archive(_xml(("1", "Example")))})
    calls = []
    ftp.connect = lambda host, timeout: calls.append(("connect", host))
    ftp.login = lambda user, password: calls.append(("login", user, password))
    ftp.close = lambda: calls.append(("close",))
    def cwd(directory):
        ftp.directories.append(directory)
        if directory == wipo_madrid.FTP_DIRECTORIES[0]:
            raise ftplib.error_perm("550 missing")
    ftp.cwd = cwd
    monkeypatch.setattr(wipo_madrid.ftplib, "FTP", lambda timeout: ftp)
    rows, _ = wipo_madrid.fetch_madrid_updates({"madrid_from_date": "2026-09-28", "feed_url": "ftp://unsafe.invalid"})
    assert len(rows) == 1
    assert calls == [("connect", "ftpird.wipo.int"), ("login", "anonymous", "guest"), ("close",)]
    assert ftp.directories == list(wipo_madrid.FTP_DIRECTORIES)
