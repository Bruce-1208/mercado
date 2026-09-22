import pytest

import DataAnalysis_db as module


@pytest.mark.parametrize("method,args", [
    ("getCbtlistDb", ("store",)),
    ("get_visit_db", ("store", "2026-01-01", "2026-01-02")),
    ("updateListings", ([], "store")),
    ("updateListings_visit", ([], "store")),
])
@pytest.mark.parametrize("fail", [False, True])
def test_analysis_always_returns_connection(monkeypatch, method, args, fail):
    class Connection:
        closed = False

        def cursor(self):
            if fail:
                raise RuntimeError("cursor failed")
            return self

        def execute(self, *args):
            pass

        def fetchall(self):
            return []

        def commit(self):
            pass

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(module.pymysql, "connect", lambda **config: connection)
    call = getattr(module.DataAnalysis_db(), method)
    if fail:
        with pytest.raises(RuntimeError, match="cursor failed"):
            call(*args)
    else:
        call(*args)
    assert connection.closed
