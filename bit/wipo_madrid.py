"""Read WIPO's published Madrid daily XML archives, never its search service.

The official download page links ftpird.wipo.int/wipo/madrid/monitor; its
ST66 directory contains the same dated updates in WIPO's structured XML
format. Daily updates cover changed Madrid registrations, not all GBD data.
"""
from __future__ import annotations

import ftplib
import io
import json
import os
import re
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

FTP_HOST = "ftpird.wipo.int"
FTP_DIRECTORIES = ("/wipo/madrid/monitor/ST66", "/wipo/madrid/romarin/ST66")
DOWNLOAD_DOCUMENTATION = "https://www.wipo.int/en/web/madrid-system/monitor/download"
SOURCE = "WIPO Madrid Monitor"
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_XML_BYTES = 350 * 1024 * 1024
MAX_ENTRIES = 20_000
MAX_PACKAGES = 31
MAX_RECORDS = 250_000
LOCAL = timezone(timedelta(hours=8))
DATE_FILE = re.compile(r"^(\d{8})\.zip$")


class MadridDownloadPending(ValueError):
    """A bounded download paused with its bytes saved for the next run."""

    def __init__(self, received, cached, expected):
        super().__init__("Madrid 日期包下载进度已保存，自动同步将继续断点下载")
        self.summary = {"download_bytes": received, "cached_download_bytes": cached,
                        "archive_bytes": expected, "resume_pending": True,
                        "source_mode": "madrid"}


def _download_cache(settings, directory, first_date):
    root = settings.get("madrid_cache_dir")
    if not root:
        return None
    # Keep the official source directories separate in case WIPO falls back
    # from Monitor to ROMARIN. Filenames always come from DATE_FILE matches.
    cache = Path(root) / (PurePosixPath(directory).parts[-2] + "-ST66")
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in cache.iterdir():
        name = path.name.removesuffix(".part")
        match = DATE_FILE.fullmatch(name)
        if match and match[1] < first_date.strftime("%Y%m%d") and path.is_file():
            path.unlink()
    return cache


def _fetch_cached_archive(ftp, name, expected_size, cache, deadline, remaining_bytes, progress=None):
    """Resume public FTP bytes from disk; publish only a complete archive."""
    complete, partial = cache / name, cache / (name + ".part")
    if complete.exists():
        size = complete.stat().st_size
        if size <= MAX_ARCHIVE_BYTES and (expected_size is None or size == expected_size):
            if callable(progress):
                progress(size, expected_size)
            return complete.read_bytes(), 0
        complete.unlink()
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > MAX_ARCHIVE_BYTES or (expected_size is not None and offset > expected_size):
        partial.unlink()
        offset = 0
    received = 0
    # No buffered bytes can be lost if a service process is killed between
    # callbacks: the next FTP REST starts at the actual file size on disk.
    with partial.open("ab", buffering=0) as stream:
        os.chmod(partial, 0o600)
        def append(chunk):
            nonlocal received
            if stream.tell() + len(chunk) > MAX_ARCHIVE_BYTES or received + len(chunk) > remaining_bytes:
                raise ValueError("Madrid 更新包超过本次下载上限，请缩短同步日期范围")
            stream.write(chunk)
            received += len(chunk)
            if callable(progress):
                progress(stream.tell(), expected_size)
            if time.monotonic() > deadline:
                os.fsync(stream.fileno())
                raise MadridDownloadPending(received, stream.tell(), expected_size)
        if expected_size is None or offset < expected_size:
            try:
                if offset:
                    ftp.retrbinary("RETR " + name, append, blocksize=64 * 1024, rest=offset)
                else:
                    ftp.retrbinary("RETR " + name, append, blocksize=64 * 1024)
            except ftplib.error_perm:
                # Older FTP servers may reject REST. If no resumed bytes were
                # received, restart this archive without poisoning its prefix.
                if not offset or received:
                    raise
                stream.seek(0)
                stream.truncate()
                ftp.retrbinary("RETR " + name, append, blocksize=64 * 1024)
        actual = stream.tell()
        if expected_size is not None and actual != expected_size:
            if actual > expected_size:
                stream.seek(0)
                stream.truncate()
            raise ValueError("Madrid 日期包下载不完整，已保留进度待重试")
        os.fsync(stream.fileno())
    partial.replace(complete)
    return complete.read_bytes(), received


def _today():
    return datetime.now(LOCAL).date()


def _local_name(tag):
    return tag.rsplit("}", 1)[-1]


def _first_text(element, names):
    for child in element.iter():
        if _local_name(child.tag) in names:
            value = " ".join("".join(child.itertext()).split())
            if value:
                return value
    return ""


def _direct_text(element, names):
    for name in names:
        child = next((c for c in element if _local_name(c.tag) == name), None)
        if child is not None:
            value = " ".join("".join(child.itertext()).split())
            if value:
                return value
    return ""


class _CheckedXmlStream:
    def __init__(self, stream):
        self.stream, self.size, self.tail = stream, 0, b""

    def read(self, size=-1):
        content = self.stream.read(size)
        self.size += len(content)
        if self.size > MAX_XML_BYTES:
            raise ValueError("Madrid XML 文件超过大小上限")
        # A rolling buffer detects split declarations and UTF-16/32 encodings
        # before bytes reach the parser. No DTD or external entity is parsed.
        declaration = (self.tail + content.replace(b"\x00", b"")).upper()
        if b"<!DOCTYPE" in declaration or b"<!ENTITY" in declaration:
            raise ValueError("Madrid XML 不允许 DOCTYPE 或实体声明")
        self.tail = declaration[-32:]
        return content


def _record_row(record):
    tag = _local_name(record.tag)
    if tag == "MARKGR":
        # IMAGE/@TEXT is the current mark's verbal text in legacy ROMARIN.
        # NAME elements are holders/representatives, not trademark names.
        current = next((c for c in record if _local_name(c.tag) == "CURRENT"), record)
        image = next((c for c in current if _local_name(c.tag) == "IMAGE"), None)
        name = image.get("TEXT", "") if image is not None else ""
        number = record.get("INTREGN", "")
        country = record.get("OOCD", "")
        classes = sorted({c.get("NICCLAI", "") for c in current.iter()
                          if _local_name(c.tag) == "GSGR" and c.get("NICCLAI")})
        status = ""
    else:
        specification = next((c for c in record
                              if _local_name(c.tag) == "WordMarkSpecification"), None)
        name = _first_text(specification, {"MarkVerbalElementText"}) if specification is not None else ""
        number = _direct_text(record, ("InternationalRegistrationNumber", "RegistrationNumber", "ApplicationNumber"))
        country = _direct_text(record, ("RegistrationOfficeCode", "OfficeOfOriginCode"))
        current_goods = next((c for c in record if _local_name(c.tag) == "GoodsServicesDetails"), record)
        classes = sorted({" ".join("".join(c.itertext()).split()) for c in current_goods.iter()
                          if _local_name(c.tag) in {"ClassNumber", "NiceClassNumber"} and c.text})
        status = _first_text(record, {"MarkCurrentStatusCode", "MarkCurrentStatusCategory"})
    if not number:
        raise ValueError("Madrid XML 商标记录缺少国际注册号")
    return {"brand_name": name, "registration_number": number,
            "country": country, "nice_classes": ",".join(classes), "status": status}


def _xml_rows(stream):
    rows, stack = [], []
    try:
        for event, element in ElementTree.iterparse(_CheckedXmlStream(stream), events=("start", "end")):
            if event == "start":
                stack.append(element)
                continue
            if _local_name(element.tag) in {"Trademark", "TradeMark", "MARKGR"}:
                rows.append(_record_row(element))
                if len(rows) > MAX_RECORDS:
                    raise ValueError("Madrid 更新记录超过单次同步上限")
                element.clear()
                if len(stack) > 1:
                    stack[-2].remove(element)
            stack.pop()
    except ElementTree.ParseError as exc:
        raise ValueError("Madrid 更新包包含无效 XML") from exc
    if not rows:
        raise ValueError("Madrid XML 不包含可识别的商标注册记录")
    return rows


def parse_madrid_archive(content):
    """Parse bounded ZIP members in memory; image assets are not extracted."""
    if len(content) > MAX_ARCHIVE_BYTES:
        raise ValueError("Madrid 日期包超过 100 MB")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ENTRIES or sum(m.file_size for m in members) > MAX_EXPANDED_BYTES:
                raise ValueError("Madrid 日期包解压范围超过上限")
            rows, xml_count = [], 0
            for member in members:
                path = PurePosixPath(member.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in member.filename:
                    raise ValueError("Madrid 日期包包含非法文件路径")
                if member.is_dir() or path.suffix.lower() != ".xml":
                    continue
                xml_count += 1
                if member.flag_bits & 1 or member.file_size > MAX_XML_BYTES:
                    raise ValueError("Madrid XML 文件被加密或超过大小上限")
                with archive.open(member) as stream:
                    rows.extend(_xml_rows(stream))
                if len(rows) > MAX_RECORDS:
                    raise ValueError("Madrid 更新记录超过单次同步上限")
            if not xml_count:
                raise ValueError("Madrid 日期包没有 XML 文件")
            return rows
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError("Madrid 日期包不是有效的 ZIP 文件") from exc


def _select_directory(ftp):
    for directory in FTP_DIRECTORIES:
        try:
            ftp.cwd(directory)
            return directory
        except ftplib.error_perm as exc:
            if not str(exc).startswith("550"):
                raise
    raise ValueError("WIPO 官方 Madrid 更新目录不可用")


def fetch_madrid_updates(settings, token="", session=None):
    """Return normalized blacklist brands and a success-only date cursor.

    ``session`` may be an already logged-in ftplib-compatible object for tests.
    The available FTP file list is authoritative: weekends and holidays need
    not publish a file. Listed files that disappear during download fail the
    run; a cursor older than the retained collection fails instead of skipping.
    """
    from bit.wipo_sync import normalize_feed_rows

    value = settings.get("madrid_from_date") or (_today() - timedelta(days=7)).isoformat()
    try:
        first_date = date.fromisoformat(value)
        if first_date.isoformat() != value:
            raise ValueError()
    except (TypeError, ValueError) as exc:
        raise ValueError("Madrid 起始日期须为 YYYY-MM-DD") from exc
    today = _today()
    if first_date > today + timedelta(days=1):
        raise ValueError("Madrid 起始日期不能晚于明天")

    ftp = session or ftplib.FTP(timeout=60)
    deadline = time.monotonic() + 1800
    downloaded = fetched = 0
    latest_rows, last_date = {}, ""
    try:
        if session is None:
            ftp.connect(FTP_HOST, timeout=15)
            ftp.login("anonymous", "guest")
        directory = _select_directory(ftp)
        available = {}
        for entry in ftp.nlst():
            name = entry.rsplit("/", 1)[-1]
            match = DATE_FILE.fullmatch(name)
            if not match:
                continue
            try:
                file_date = datetime.strptime(match[1], "%Y%m%d").date()
            except ValueError:
                continue
            if file_date <= today:
                available[file_date] = name
        if not available:
            raise ValueError("WIPO 官方 Madrid 目录没有可用日期包")
        if first_date < min(available):
            error = ValueError(f"Madrid 起始日期早于官方保留范围（最早 {min(available).isoformat()}），请先导入历史授权数据或调整起始日期；不会自动跳过缺失历史")
            error.available_from = min(available).isoformat()
            raise error
        cache = _download_cache(settings, directory, first_date)
        pending = sorted(d for d in available if d >= first_date)
        pending_count = len(pending)
        package_limit = max(1, min(int(settings.get("madrid_batch_packages", MAX_PACKAGES)), MAX_PACKAGES))
        if len(pending) > package_limit:
            # Process a bounded chronological batch. The caller continues
            # from this batch's cursor in the following scheduled run.
            pending = pending[:package_limit]
        processed = 0
        for file_date in pending:
            name = available[file_date]
            size = None
            # A retained backlog can exceed a single bounded run. Stop at a
            # completed package boundary; the caller resumes after its cursor.
            if callable(getattr(ftp, "size", None)):
                ftp.voidcmd("TYPE I")
                size = ftp.size(name)
                if size is not None:
                    if size > MAX_ARCHIVE_BYTES:
                        raise ValueError("Madrid 日期包超过 100 MB")
                    if downloaded + size > MAX_DOWNLOAD_BYTES and processed:
                        break
            if cache is None:
                buffer = io.BytesIO()
                def append(chunk):
                    nonlocal downloaded
                    downloaded += len(chunk)
                    if buffer.tell() + len(chunk) > MAX_ARCHIVE_BYTES or downloaded > MAX_DOWNLOAD_BYTES:
                        raise ValueError("Madrid 更新包超过本次下载上限，请缩短同步日期范围")
                    if time.monotonic() > deadline:
                        raise ValueError("Madrid 更新下载超时，请稍后重试")
                    buffer.write(chunk)
                ftp.retrbinary("RETR " + name, append, blocksize=64 * 1024)
                content = buffer.getvalue()
            else:
                content, received = _fetch_cached_archive(
                    ftp, name, size, cache, deadline, MAX_DOWNLOAD_BYTES - downloaded,
                    progress=settings.get("madrid_download_progress"))
                downloaded += received
            try:
                snapshot_rows = parse_madrid_archive(content)
            except ValueError:
                if cache is not None:
                    # A corrupt package must be fetched again on retry rather
                    # than becoming a permanent failure from the local cache.
                    (cache / name).unlink(missing_ok=True)
                raise
            for row in snapshot_rows:
                fetched += 1
                if fetched > MAX_RECORDS:
                    raise ValueError("Madrid 更新记录超过单次同步上限")
                row["update_date"] = file_date.isoformat()
                latest_rows[row["registration_number"]] = row
            last_date = file_date.isoformat()
            processed += 1
        usable = [row for row in latest_rows.values() if isinstance(row["brand_name"], str) and row["brand_name"].strip()]
        normalized, _ = normalize_feed_rows(usable) if usable else ([], {})
        by_name = {}
        for registration in usable:
            key = " ".join(registration["brand_name"].split()).casefold()
            by_name.setdefault(key, []).append(registration)
        for row in normalized:
            registrations = by_name[row["brand_name"].casefold()]
            detail = json.loads(row["source_detail"])
            update_dates = sorted({r["update_date"] for r in registrations})
            detail.update(source=SOURCE, source_mode="madrid", update_date=update_dates[-1],
                          update_date_from=update_dates[0],
                          source_url=DOWNLOAD_DOCUMENTATION)
            row.update(notes="WIPO Madrid 国际商标每日更新，自动导入品牌风险黑名单。",
                       source_detail=json.dumps(detail, ensure_ascii=False), evidence_count=0)
        return normalized, {"fetched": fetched, "unique": len(normalized),
                            "skipped": len(latest_rows) - len(usable), "registrations": len(latest_rows),
                            "packages": processed, "download_bytes": downloaded,
                            "madrid_through_date": last_date, "source_mode": "madrid",
                            "source": SOURCE, "coverage": "Madrid 每日变更记录，非全球品牌数据库全量",
                            "available_through_date": max(available).isoformat(),
                            "remaining_packages": pending_count - processed,
                            "has_more_packages": processed < pending_count,
                            "has_more": processed < pending_count}
    except (ftplib.Error, OSError, EOFError) as exc:
        raise ValueError("WIPO Madrid FTP 连接或日期包下载失败，请检查网络后重试") from exc
    finally:
        if session is None:
            ftp.close()
