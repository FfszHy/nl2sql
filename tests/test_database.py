import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.core.errors import AppError
from app.models.contracts import DataSourceConfig
from app.services.database import execute_select_sql


class FakeConnection:
    """Record transaction ordering without opening a database connection."""

    def __init__(self, rows=(), columns=(), fail_sql=None):
        self.events = []
        self._read_only = False
        self.cursor_mock = MagicMock()
        self.cursor_mock.__enter__.return_value = self.cursor_mock
        self.cursor_mock.description = [SimpleNamespace(name=name) for name in columns]
        self.cursor_mock.fetchall.return_value = rows

        def execute(sql):
            self.events.append(("execute", sql))
            if not self.read_only:
                raise AssertionError("read_only must be enabled before SQL execution")
            if sql == fail_sql:
                raise RuntimeError("simulated query failure")

        self.cursor_mock.execute.side_effect = execute

    @property
    def read_only(self):
        return self._read_only

    @read_only.setter
    def read_only(self, value):
        self.events.append(("read_only", value))
        self._read_only = value

    def cursor(self):
        self.events.append(("cursor",))
        return self.cursor_mock

    def commit(self):
        self.events.append(("commit",))

    def rollback(self):
        self.events.append(("rollback",))

    def close(self):
        self.events.append(("close",))


class DatabaseExecutionTests(unittest.TestCase):
    def setUp(self):
        self.datasource = DataSourceConfig(
            host="localhost",
            user="test_reader",
            password="test-only-placeholder",
            database="test_database",
        )
        self.sql = "SELECT id, total FROM orders LIMIT 10"
        self.timeout_sql = "SET LOCAL statement_timeout = '30s'"
        self.lock_timeout_sql = "SET LOCAL lock_timeout = '5s'"

    def test_success_sets_read_only_and_timeout_before_query(self):
        connection = FakeConnection(rows=[(1, 12.5)], columns=["id", "total"])
        with patch("app.services.database._connect", return_value=connection) as connect:
            result = execute_select_sql(self.datasource, self.sql)

        connect.assert_called_once_with(self.datasource)
        self.assertEqual(result, (["id", "total"], [[1, 12.5]], 1))
        self.assertEqual(
            connection.events,
            [
                ("read_only", True),
                ("cursor",),
                ("execute", self.timeout_sql),
                ("execute", self.lock_timeout_sql),
                ("execute", self.sql),
                ("rollback",),
                ("close",),
            ],
        )

    def test_empty_result_keeps_column_names(self):
        connection = FakeConnection(columns=["id"])
        with patch("app.services.database._connect", return_value=connection):
            result = execute_select_sql(self.datasource, self.sql)
        self.assertEqual(result, (["id"], [], 0))
        self.assertEqual(connection.events[-2:], [("rollback",), ("close",)])

    def test_query_or_timeout_failure_rolls_back_and_closes(self):
        for failing_sql in (self.timeout_sql, self.lock_timeout_sql, self.sql):
            with self.subTest(failing_sql=failing_sql):
                connection = FakeConnection(fail_sql=failing_sql)
                with patch("app.services.database._connect", return_value=connection):
                    with self.assertRaises(AppError) as caught:
                        execute_select_sql(self.datasource, self.sql)

                self.assertEqual(caught.exception.code, 1003)
                self.assertEqual(caught.exception.error_type, "db_error")
                self.assertIsInstance(caught.exception.__cause__, RuntimeError)
                self.assertNotIn(("commit",), connection.events)
                self.assertEqual(connection.events[-2:], [("rollback",), ("close",)])
                if failing_sql != self.sql:
                    self.assertNotIn(("execute", self.sql), connection.events)


if __name__ == "__main__":
    unittest.main()
