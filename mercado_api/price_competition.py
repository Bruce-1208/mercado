"""Read-only Global Selling pricing signals; benchmarks and Catalog stay distinct."""
from __future__ import annotations

import re

from mercado_api.client import MercadoAPIError


class PriceCompetitionClient:
    def __init__(self, client, *, caller_id="", client_id=""):
        self.client = client
        self.caller_id = str(caller_id or "").strip()
        self.client_id = str(client_id or "").strip()

    @staticmethod
    def item_id(value):
        value = str(value or "").strip().upper()
        if not re.fullmatch(r"ML[A-Z]\d+", value):
            raise ValueError("价格竞争仅支持本地站点商品编号")
        return value

    def reference(self, item_id):
        item_id = self.item_id(item_id)
        try:
            return self.client.request("GET", f"/marketplace/benchmarks/items/{item_id}/details")
        except MercadoAPIError as exc:
            # A missing benchmark is normal; authorization/network failures aren't.
            if "失败 (404):" in str(exc):
                return None
            raise

    def catalog(self, item_id):
        item_id = self.item_id(item_id)
        if not self.caller_id or not self.client_id:
            raise ValueError("Catalog 竞争需要店铺 caller.id 和应用 client.id")
        return self.client.request("GET", f"/marketplace/items/{item_id}/price_to_win",
            params={"caller.id": self.caller_id, "client.id": self.client_id})

    def inspect(self, item_id):
        item_id = self.item_id(item_id)
        item = self.client.get_marketplace_item(item_id)
        if item.get("id") != item_id:
            raise ValueError("官方商品编号与请求不一致")
        flag = item.get("catalog_listing")
        if flag is True:
            return {"kind": "catalog", "item": item, "signal": self.catalog(item_id)}
        if flag is False:
            return {"kind": "ordinary", "item": item, "signal": self.reference(item_id)}
        return {"kind": "unknown", "item": item, "signal": None}
