"""Official catalog size-chart discovery and submit-once creation adapter."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests

from mercado_api.client import MercadoAPIError
from mercado_api.request_budget import admission


class SizeChartWriteError(MercadoAPIError):
    def __init__(self, message: str, *, unknown: bool, details: Any = None):
        super().__init__(message)
        self.unknown = unknown
        self.details = details


def bare_domain(value: Any) -> str:
    domain = str(value or "").strip().upper()
    if "-" in domain and domain.split("-", 1)[0] in {"CBT", "MLM", "MLB", "MCO", "MLC", "MLA", "MLU", "MPE", "MEC"}:
        domain = domain.split("-", 1)[1]
    if not domain or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for char in domain):
        raise ValueError("请选择官方支持的商品域")
    return domain


def _tags(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(key) for key, enabled in value.items() if enabled]
    return [str(tag) for tag in (value or [])]


def extract_fields(specs: Any) -> dict[str, Any]:
    """Preserve official attribute metadata while separating filters from grid rows."""
    template_fields, row_fields, max_rows = {}, {}, None

    def walk(node, in_grid=False):
        nonlocal max_rows
        if isinstance(node, list):
            for child in node:
                walk(child, in_grid)
            return
        if not isinstance(node, dict):
            return
        kind = str(node.get("type") or node.get("component") or node.get("id") or "").lower()
        grid = in_grid or kind in {"grid", "grids", "size_chart", "size_chart_grid"}
        config = node.get("ui_config") or {}
        if kind == "grid" and config.get("max_allowed") is not None:
            try:
                max_rows = int(config["max_allowed"])
            except (ValueError, TypeError):
                pass
        for raw in node.get("attributes") or []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            field = dict(raw)
            tags = _tags(raw.get("tags"))
            field["tags"] = tags
            lowered_tags = {tag.lower() for tag in tags}
            field["required"] = bool(raw.get("required") or "required" in lowered_tags or "grid_template_required" in lowered_tags)
            field["allowed_units"] = list(raw.get("allowed_units") or raw.get("units") or [])
            field["values"] = list(raw.get("values") or [])
            field["name"] = str(raw.get("name") or raw["id"])
            # Filters in a grid component still describe the chart itself.
            target = template_fields if ("grid_template_required" in lowered_tags or "grid_filter" in lowered_tags) else row_fields if grid else template_fields
            # Output sections repeat IDs without input validation metadata.
            # Keep the input's tags/choices when traversing those mirrors.
            previous = target.get(str(raw["id"]), {})
            field["tags"] = list(dict.fromkeys([*previous.get("tags", []), *tags]))
            field["required"] = bool(previous.get("required") or field["required"])
            if not field["values"] and previous.get("values"):
                field["values"] = previous["values"]
            if not field["allowed_units"] and previous.get("allowed_units"):
                field["allowed_units"] = previous["allowed_units"]
            target[str(raw["id"])] = {**previous, **field}
        for key, value in node.items():
            if key not in {"attributes", "values", "allowed_units", "units", "tags"}:
                walk(value, grid)

    walk(specs)
    return {"template_fields": list(template_fields.values()), "row_fields": list(row_fields.values()),
            "main_attributes": [field for field in row_fields.values() if "main_attribute_candidate" in {tag.lower() for tag in field["tags"]}],
            "max_rows": max_rows}


class MercadoSizeChartsClient:
    def __init__(self, client):
        self.client = client

    def identity(self) -> dict:
        profile = self.client.request("GET", "/users/me")
        if not isinstance(profile, dict) or not profile.get("id") or not profile.get("site_id"):
            raise ValueError("官方未返回有效的店铺身份，请重新授权")
        site = str(profile["site_id"]).upper()
        sites = [site]
        if site == "CBT":
            marketplace = self.client.request("GET", f"/marketplace/users/{int(profile['id'])}")
            sites = sorted({str(row.get("site_id") or "").upper() for row in marketplace.get("marketplaces") or []
                            if row.get("site_id") and row.get("user_id")})
            if not sites:
                raise ValueError("官方未返回可用销售站点，请检查店铺的 Global Selling 授权")
        return {"seller_id": int(profile["id"]), "site_id": site, "sites": sites}

    def active_domains(self, site_id: str) -> list[dict]:
        raw = self.client.request("GET", f"/catalog/charts/{quote(site_id, safe='')}/configurations/active_domains")
        source = raw.get("domains", []) if isinstance(raw, dict) else raw
        domains = []
        for row in source or []:
            original = str(row.get("domain_id") or row.get("id") or "") if isinstance(row, dict) else str(row)
            if original:
                domain = bare_domain(original)
                domains.append({"id": domain, "domain_id": domain, "full_domain_id": original,
                                "name": str(row.get("name") or domain) if isinstance(row, dict) else domain})
        return domains

    def technical_specs(self, site_id: str, domain_id: str, attributes=None, *, grids=False) -> dict:
        path = f"/domains/{quote(site_id + '-' + bare_domain(domain_id), safe='')}/technical_specs"
        if not grids:
            return self.client.request("GET", path)
        # technical_specs uses item-style values, unlike chart creation's
        # values[] format. Sending values[] silently returns an empty object.
        filters = []
        for row in attributes or []:
            if "values" not in row:
                filters.append(dict(row))
                continue
            values = row.get("values") or []
            if len(values) != 1:
                raise ValueError(f"规格筛选属性 {row.get('id')} 必须包含一个值")
            value = values[0]
            filters.append({"id": row["id"], **({"value_id": str(value["id"])}
                           if value.get("id") else {"value_name": value.get("name", "")})})
        return self.client.request("POST", path, params={"section": "grids"}, json_body={"attributes": filters})

    def get_chart(self, chart_id: str) -> dict:
        return self.client.request("GET", f"/catalog/charts/{quote(str(chart_id), safe='')}")

    def create_chart(self, payload: dict) -> dict:
        """Never replay an ambiguous create, including HTTP 5xx or invalid JSON."""
        # Ownership is derived by the official API from the bearer token. The
        # frozen local payload keeps seller_id for audit and reconciliation;
        # the published create schema does not specify this request field.
        official_payload = {key: value for key, value in payload.items() if key != "seller_id"}
        try:
            with admission(self.client.access_token):
                response = self.client.session.request(
                    "POST", f"{self.client.BASE_URL}/catalog/charts",
                    headers={"Authorization": f"Bearer {self.client.access_token}", "Content-Type": "application/json"},
                    json=official_payload, timeout=self.client.timeout)
        except requests.RequestException as exc:
            raise SizeChartWriteError("上传连接中断，结果未知；请核对官方尺码表，禁止直接重试", unknown=True) from exc
        try:
            data = response.json()
        except (ValueError, TypeError) as exc:
            raise SizeChartWriteError("官方返回无法识别，上传结果未知；请核对官方尺码表", unknown=True) from exc
        if not response.ok:
            uncertain = response.status_code >= 500 or response.status_code in {408, 409}
            detail = str(data)[:2000]
            raise SizeChartWriteError(f"官方拒绝上传 ({response.status_code}): {detail}", unknown=uncertain, details=data)
        if not isinstance(data, dict) or not data.get("id"):
            raise SizeChartWriteError("官方未返回尺码表编号，上传结果未知；请核对官方尺码表", unknown=True, details=data)
        return data
