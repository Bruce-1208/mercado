
"""Mercado Libre Global Selling 订单与 Listing 数据同步包。

包顶层导出最常用的 API 客户端、SQLite 数据库和同步服务，便于其他模块直接
组合使用；命令行用法见 ``python -m mercado_api --help``。
"""

from .client import MercadoLibreClient
from .database import MercadoDatabase
from .service import MercadoSyncService



__all__ = ["MercadoAPIError", "SyncResult", "sync_listings"]


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(name)
    from .mercado_api_listings import MercadoAPIError, SyncResult, sync_listings

    return {
        "MercadoAPIError": MercadoAPIError,
        "SyncResult": SyncResult,
        "sync_listings": sync_listings,
    }[name]
