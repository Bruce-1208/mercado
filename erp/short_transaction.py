"""Bounded retries for local, idempotent database writes only."""
import time


def run(connection, operation):
    # Restore the session setting before returning a pooled connection.
    with connection.cursor() as cursor:
        cursor.execute("SELECT @@SESSION.innodb_lock_wait_timeout AS lock_timeout")
        timeout = int(cursor.fetchone()["lock_timeout"])
        cursor.execute("SET SESSION innodb_lock_wait_timeout = 5")
    failed = False
    try:
        for attempt in range(3):
            try:
                result = operation()
                connection.commit()
                return result
            except BaseException as exc:
                try:
                    connection.rollback()
                except Exception:
                    raise exc
                if not getattr(exc, "args", ()) or exc.args[0] not in (1205, 1213) or attempt == 2:
                    raise
                time.sleep(.2 * (2 ** attempt))
    except BaseException:
        failed = True
        raise
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION innodb_lock_wait_timeout = %s", (timeout,))
        except Exception:
            if not failed:
                raise
