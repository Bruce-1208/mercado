"""Fetch traceable global brand/IP names for the knowledge risk blacklist."""

import re
import requests

from bit.bit_infringement_knowledge_analysis import normalize_brand_name

ENDPOINT = "https://query.wikidata.org/sparql"
CATEGORIES = (("Q431289", "品牌"), ("Q196600", "媒体系列 IP"),
              ("Q95074", "虚构角色"))


def sync_global_ip(*, writer, log_callback=None, session=None, per_category=1000):
    """Bounded, paginated import; existing manual decisions are protected by writer."""
    client = session or requests
    log = log_callback or (lambda message: None)
    limit = max(1, min(int(per_category), 5000))
    seen = set()
    totals = {key: 0 for key in ("inserted", "updated", "skipped_manual", "skipped_blacklist")}
    candidates = 0
    for category, label in CATEGORIES:
        for offset in range(0, limit, 250):
            size = min(250, limit - offset)
            query = f'''SELECT ?item ?name WHERE {{
              {{ SELECT DISTINCT ?item WHERE {{ ?item wdt:P31/wdt:P279* wd:{category} }}
                 ORDER BY ?item LIMIT {size} OFFSET {offset} }}
              ?item rdfs:label ?name .
              FILTER(LANG(?name) IN ("en", "zh", "zh-hans", "es", "pt"))
            }} ORDER BY ?item ?name'''
            log(f"正在获取全球{label}：第 {offset // 250 + 1} 批")
            response = client.get(ENDPOINT, params={"query": query, "format": "json"},
                                  headers={"Accept": "application/sparql-results+json",
                                           "User-Agent": "MercadoKnowledge/1.0"}, timeout=(10, 90))
            response.raise_for_status()
            rows = response.json()["results"]["bindings"]
            if not isinstance(rows, list):
                raise ValueError("全球品牌/IP 数据源返回格式错误")
            records = []
            for row in rows:
                uri = row.get("item", {}).get("value", "")
                if not re.fullmatch(r"https?://www\.wikidata\.org/entity/Q[1-9][0-9]*", uri):
                    continue
                name = normalize_brand_name(row.get("name", {}).get("value"))
                if not name or name.casefold() in seen:
                    continue
                seen.add(name.casefold())
                source = "https://www.wikidata.org/wiki/" + uri.rsplit("/", 1)[-1]
                records.append({"brand_name": name, "list_type": "blacklist",
                                "notes": f"全球{label}风险拦截；不代表已确认侵权。来源：{source}",
                                "source_detail": f"global_ip:wikidata:{category} {source}",
                                "evidence_count": 0})
            for start in range(0, len(records), 500):
                result = writer(records[start:start + 500]) or {}
                for key in totals:
                    totals[key] += int(result.get(key) or 0)
            candidates += len(records)
            log(f"全球{label}已处理 {len(records)} 个名称；累计新增 {totals['inserted']} 个")
            if not rows:
                break
    return {"blacklist_candidates": candidates, "whitelist_candidates": 0,
            "write_result": totals, "source": "wikidata", "per_category_limit": limit}
