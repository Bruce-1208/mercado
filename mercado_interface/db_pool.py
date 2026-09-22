"""Legacy API entry point; use the lazy process-wide MySQL budget."""

if not __package__:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bit.db_pool import get_db_connection

__all__ = ("get_db_connection",)
