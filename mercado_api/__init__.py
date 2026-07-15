"""Mercado Libre API helpers."""

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
