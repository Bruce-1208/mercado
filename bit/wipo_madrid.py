"""Read WIPO's published Madrid daily XML archives, never its search service.

The official download page links ftpird.wipo.int/wipo/madrid/monitor; its
ST66 directory contains the same dated updates in WIPO's structured XML
format. Daily updates cover changed Madrid registrations, not all GBD data.
"""
from __future__ import annotations

import ftplib
import io
import json
import re
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath
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
    """Return normalized review candidates and a success-only date cursor.

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
            raise ValueError("Madrid 起始日期早于官方保留范围，请先导入历史授权数据或调整起始日期")
        pending = sorted(d for d in available if d >= first_date)
        pending_count = len(pending)
        if len(pending) > MAX_PACKAGES:
            # Process a bounded chronological batch. The caller continues
            # from this batch's cursor in the following scheduled run.
            pending = pending[:MAX_PACKAGES]
        processed = 0
        for file_date in pending:
            name = available[file_date]
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
            snapshot_rows = parse_madrid_archive(buffer.getvalue())
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
            row.update(notes="WIPO Madrid 国际商标每日更新，仅作为待复核线索，需人工确认处置。",
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
