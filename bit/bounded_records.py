"""Bound materialized risk results while retaining exact streaming summaries."""

import heapq


MAX_MATERIALIZED_RECORDS = 50000


def collect_bounded(records):
    rows = []
    try:
        for row in records:
            if len(rows) >= MAX_MATERIALIZED_RECORDS:
                raise ValueError("当前范围超过 50000 件，请缩小筛选范围后分批检测或导出")
            rows.append(row)
        return rows
    finally:
        if hasattr(records, "close"):
            records.close()


def select_bounded(records, *, limit, key, reverse):
    limit = max(0, int(limit or 0))
    if not limit:
        return sorted(collect_bounded(records), key=key, reverse=reverse)
    if limit > MAX_MATERIALIZED_RECORDS:
        raise ValueError("单次最多返回 50000 件，请缩小筛选范围")
    select = heapq.nlargest if reverse else heapq.nsmallest
    return select(limit, records, key=key)
