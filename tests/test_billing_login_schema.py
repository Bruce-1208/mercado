"""Login subscription checks must not contend with billing DDL locks."""
import pytest
from pymysql.err import ProgrammingError, OperationalError

from bit import account_billing as billing


class SchemaCursor:
    def __init__(self, existing=(), failure=None):
        self.tables = set(existing)
        self.failure = failure
        self.ddl = []

    def execute(self, sql):
        if sql.lstrip().startswith("SELECT"):
            if self.failure:
                raise self.failure
            if self.tables != {"workbench_subscriptions", "workbench_billing_records"}:
                raise ProgrammingError(1146, "Table doesn't exist")
        else:
            self.ddl.append(sql)
            self.tables.add(sql.split()[5])


def test_existing_schema_never_requests_ddl_on_repeated_login_checks():
    cursor = SchemaCursor({"workbench_subscriptions", "workbench_billing_records"})
    for _ in range(20):
        billing.ensure_tables(cursor)
    assert cursor.ddl == []


@pytest.mark.parametrize("existing", [(), ("workbench_subscriptions",),
                                      ("workbench_billing_records",)])
def test_new_or_partial_install_initializes_then_uses_read_only_probe(existing):
    cursor = SchemaCursor(existing)
    billing.ensure_tables(cursor)
    assert len(cursor.ddl) == 2
    billing.ensure_tables(cursor)
    assert len(cursor.ddl) == 2


@pytest.mark.parametrize("failure", [ProgrammingError(1142, "SELECT denied"),
                                     OperationalError(2013, "Lost connection")])
def test_other_database_failures_propagate_without_attempting_ddl(failure):
    cursor = SchemaCursor(failure=failure)
    with pytest.raises(type(failure)) as caught:
        billing.ensure_tables(cursor)
    assert caught.value is failure
    assert cursor.ddl == []
