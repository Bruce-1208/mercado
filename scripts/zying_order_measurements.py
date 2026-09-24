"""读取智赢跨境订单详情的官方重量尺寸，输出三列 Excel。

订单号来自智赢自带的订单导出文件。请在桌面端先选中“运费跳高档”筛选。
本脚本只读取订单详情，不打开产品、不修改任何智赢数据。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


STATUS = "运费跳高档"
ID_HEADERS = ("id", "订单号", "订单id")


@dataclass(frozen=True)
class Measurements:
    weight_g: str
    dimensions_cm: str


def positive_number(value):
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"不是有效数值：{value!r}") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"重量或尺寸不是正数：{value!r}")
    return format(number.normalize(), "f")


def parse_official(text):
    """解析订单里的“官方重量：435克 19x15x8厘米”。"""
    pattern = (r"(?<![\d.\-])(?:官方重量\s*[:：]\s*)?"
               r"(\d+(?:\.\d+)?)\s*(千克|kg|克|g)\s*"
               r"(\d+(?:\.\d+)?)\s*[xX×*]\s*"
               r"(\d+(?:\.\d+)?)\s*[xX×*]\s*"
               r"(\d+(?:\.\d+)?)\s*(厘米|cm|毫米|mm)")
    matches = list(re.finditer(pattern, str(text), re.I))
    if len(matches) != 1:
        raise ValueError(f"官方重量尺寸缺失或不唯一：{text!r}")
    weight, weight_unit, *rest = matches[0].groups()
    a, b, c, size_unit = rest
    weight_g = Decimal(weight) * (1000 if weight_unit.lower() in ("千克", "kg") else 1)
    factor = 10 if size_unit.lower() in ("毫米", "mm") else 1
    sizes = [positive_number(Decimal(part) / factor) for part in (a, b, c)]
    return Measurements(positive_number(weight_g), "×".join(sizes))


def normalize_order_id(value):
    if value is None:
        return None
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text if re.fullmatch(r"\d{7,12}", text) else None


def load_order_ids(path, max_count):
    if path.suffix.lower() == ".xlsx":
        from openpyxl import load_workbook
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            rows = workbook.active.values
            header = next(rows, None)
            if header is None:
                raise ValueError("订单导出文件为空")
            ids = _ids_from_rows(header, rows, max_count)
        finally:
            workbook.close()
        return ids
    if path.suffix.lower() == ".csv":
        # utf-8-sig handles UTF-8 exports; gb18030 handles common Windows CSV exports.
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                with path.open("r", encoding=encoding, newline="") as stream:
                    reader = csv.reader(stream)
                    header = next(reader, None)
                    if header is None:
                        raise ValueError("订单导出文件为空")
                    return _ids_from_rows(header, reader, max_count)
            except UnicodeError:
                continue
        raise ValueError("无法识别 CSV 编码")
    raise ValueError("--input 只支持 .xlsx 或 .csv")


def _ids_from_rows(header, rows, max_count):
    normalized = [str(cell or "").strip().casefold() for cell in header]
    columns = [i for i, name in enumerate(normalized) if name in ID_HEADERS]
    if len(columns) != 1:
        raise ValueError(f"找不到唯一的智赢内部订单号列；表头为 {normalized!r}")
    column = columns[0]
    found = []
    seen = set()
    for row in rows:
        order_id = normalize_order_id(row[column] if column < len(row) else None)
        if order_id and order_id not in seen:
            seen.add(order_id)
            found.append(order_id)
            if len(found) >= max_count:
                break
    if not found:
        raise ValueError("导出文件中没有有效的智赢内部订单号")
    return found


class OrderNotFound(Exception):
    pass


class DesktopReader:
    def __init__(self, pause=1.0, row_y_offset=55):
        import ctypes
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        import psutil
        import pyautogui
        from pywinauto import Desktop

        self.mouse = pyautogui
        self.mouse.FAILSAFE = True
        self.mouse.PAUSE = .15
        self.pause = pause
        self.row_y_offset = row_y_offset
        desktop = Desktop(backend="win32")
        matches = []
        for window in desktop.windows():
            try:
                if (psutil.Process(window.process_id()).name().lower() == "zying.exe"
                        and window.window_text() == "分销系统"):
                    matches.append(window)
            except (psutil.Error, OSError):
                continue
        if len(matches) != 1:
            raise RuntimeError("请打开并登录一个智赢跨境窗口，进入订单列表")
        self.window = matches[0]
        self.window.set_focus()

    @staticmethod
    def aid(control):
        return str(getattr(control.element_info, "automation_id", ""))

    def find(self, aid, root=None):
        root = root if root is not None else self.window
        return [control for control in root.descendants()
                if self.aid(control) == aid and control.is_visible()]

    def one(self, aid, root=None):
        matches = self.find(aid, root)
        if len(matches) != 1:
            raise RuntimeError(f"控件 {aid} 应唯一，实际找到 {len(matches)} 个")
        return matches[0]

    def order_list(self):
        return self.one("List订单列表")

    def check_status(self):
        selected = self.one("PType", self.order_list()).window_text().strip()
        if selected != STATUS:
            raise RuntimeError(f"请先在智赢订单页选择“{STATUS}”；当前为 {selected!r}")

    def search(self, order_id):
        self.check_status()
        key = self.one("PKey", self.order_list())
        self.window.set_focus()
        key.click_input()
        self.mouse.hotkey("ctrl", "a")
        self.mouse.write(order_id, interval=.04)
        self.mouse.press("enter")
        time.sleep(max(1.0, self.pause))
        deadline = time.monotonic() + 15
        total = ""
        while time.monotonic() < deadline:
            total = self.one("lblTotal", self.order_list()).window_text().strip()
            if total == "共 1 订单":
                break
            if re.search(r"共\s*0\s*订单", total):
                raise OrderNotFound(f"订单 {order_id} 不在当前筛选结果中")
            time.sleep(.4)
        else:
            raise RuntimeError(f"查询 {order_id} 后订单数不是1：{total!r}")

        # 订单表格自绘，筛选为唯一结果后只需点击首行，无需 OCR 定位或翻页。
        grid = self.one("tbl", self.order_list())
        rect = grid.rectangle()
        if rect.height() < self.row_y_offset + 10:
            raise RuntimeError("订单列表首行不可见")
        self.mouse.click(int(rect.left + min(70, rect.width() // 5)),
                         int(rect.top + self.row_y_offset))

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            matching = self._details_for(order_id)
            if len(matching) == 1:
                return matching[0]
            if len(matching) > 1:
                raise RuntimeError(f"订单 {order_id} 出现多个详情窗口")
            time.sleep(.4)
        raise RuntimeError(f"没有打开订单 {order_id} 的详情，可能需要调整 --row-y-offset")

    def _details_for(self, order_id):
        candidates = []
        for panel in self.find("right", self.order_list()):
            if not self.find("lb官方尺寸", panel):
                continue
            if any(label.window_text().strip() == order_id for label in self.find("lbl", panel)):
                candidates.append(panel)
        # WinForms 有时把同一 HWND 暴露两次；以句柄去重。
        return list({panel.handle: panel for panel in candidates}.values())

    def read(self, order_id):
        detail = self.search(order_id)
        official = [label.window_text().strip() for label in self.find("lb官方尺寸", detail)]
        official = sorted(set(text for text in official if text))
        if len(official) != 1:
            raise ValueError(f"订单 {order_id} 有 {len(official)} 组不同的官方重量尺寸")
        measurement = parse_official(official[0])
        return {"order_id": order_id, "weight_g": measurement.weight_g,
                "dimensions_cm": measurement.dimensions_cm}


def write_excel(records, path):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "官方重量尺寸"
    sheet.append(["订单号", "重量（克）", "尺寸（厘米）"])
    for record in records:
        weight = Decimal(record["weight_g"])
        sheet.append([record["order_id"], int(weight) if weight == int(weight) else float(weight),
                      record["dimensions_cm"]])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.column_dimensions["A"].width = 18
    sheet.column_dimensions["B"].width = 16
    sheet.column_dimensions["C"].width = 22
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    for cell in sheet["A"][1:]:
        cell.number_format = "@"
    for cell in sheet["B"][1:]:
        cell.number_format = "0.##"
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="智赢自带导出的 .xlsx 或 .csv 订单文件")
    source.add_argument("--orders", nargs="+", help="直接指定智赢内部订单号")
    parser.add_argument("--max-count", type=int, default=100, help="最多读取的订单数，默认100")
    parser.add_argument("--row-y-offset", type=int, default=55,
                        help="订单列表首行相对表格顶端的像素位置，默认55")
    parser.add_argument("--pause", type=float, default=1.0)
    parser.add_argument("--output", type=Path,
                        default=Path(f"output/zying_order_measurements_{datetime.now():%Y%m%d_%H%M%S}.xlsx"))
    args = parser.parse_args()
    if args.max_count < 1 or args.row_y_offset < 10 or args.pause < .2:
        parser.error("max-count、row-y-offset 必须为正，pause 不得小于0.2秒")
    if args.output.suffix.lower() != ".xlsx":
        parser.error("output 必须是 .xlsx 文件")
    if args.input:
        orders = load_order_ids(args.input, args.max_count)
    else:
        orders = []
        for raw in args.orders:
            order_id = normalize_order_id(raw)
            if not order_id:
                parser.error(f"无效的智赢内部订单号：{raw!r}")
            if order_id not in orders:
                orders.append(order_id)
        orders = orders[:args.max_count]

    log_path = args.output.with_name(
        f"{args.output.stem}_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(status, **fields):
        event = {"time": datetime.now().isoformat(), "status": status, **fields}
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(json.dumps(event, ensure_ascii=False), flush=True)

    reader = DesktopReader(args.pause, args.row_y_offset)
    reader.check_status()
    log("started", requested=len(orders))
    records = []
    for order_id in orders:
        try:
            record = reader.read(order_id)
            records.append(record)
            log("read", **record)
        except (OrderNotFound, ValueError) as exc:
            log("skipped", order_id=order_id, reason=str(exc))
        except Exception as exc:
            log("stopped", order_id=order_id, reason=str(exc))
            raise
    write_excel(records, args.output)
    log("finished", exported=len(records), excel=str(args.output))


if __name__ == "__main__":
    main()
