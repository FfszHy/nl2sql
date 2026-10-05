import unittest

from app.core.errors import AppError
from app.services.sql_security import (
    reject_dangerous_intent,
    validate_and_normalize_sql,
)


class SQLSecurityTests(unittest.TestCase):
    def test_rejects_empty_non_select_and_multiple_statements(self):
        cases = [
            (" ", 2000),
            ("DELETE FROM orders", 2005),
            ("SELECT 1; SELECT 2", 2004),
        ]
        for sql, expected_code in cases:
            with self.subTest(sql=sql):
                with self.assertRaises(AppError) as caught:
                    validate_and_normalize_sql(sql, max_rows=100)
                self.assertEqual(caught.exception.code, expected_code)
                self.assertEqual(caught.exception.error_type, "sql_security_error")

    def test_rejects_forbidden_keywords_functions_and_system_schemas(self):
        cases = [
            ("SELECT * FROM orders WHERE id = 1 FOR UPDATE", 2001),
            ("SELECT 1 INTO x", 2001),
            ("SELECT pg_sleep(10)", 2002),
            ("SELECT pg_read_file('/synthetic/path')", 2002),
            ("SELECT nextval('seq')", 2002),
            ("SELECT pg_advisory_lock(1)", 2002),
            ('SELECT "pg_sleep"(10)', 2002),
            ('SELECT "pg_read_file"(\'/synthetic/path\')', 2002),
            ("SELECT * FROM pg_catalog.pg_tables", 2003),
            ("SELECT * FROM information_schema.tables", 2003),
            ("SELECT * FROM movies -- comment", 2007),
            ("SELECT * FROM movies /* comment */", 2007),
        ]
        for sql, expected_code in cases:
            with self.subTest(sql=sql):
                with self.assertRaises(AppError) as caught:
                    validate_and_normalize_sql(sql, max_rows=100)
                self.assertEqual(caught.exception.code, expected_code)

    def test_enforces_limit_and_preserves_offset(self):
        cases = [
            ("SELECT * FROM orders;", "SELECT * FROM orders LIMIT 100", True),
            ("SELECT * FROM orders LIMIT 10", "SELECT * FROM orders LIMIT 10", False),
            ("SELECT * FROM orders LIMIT 1000 OFFSET 20", "SELECT * FROM orders LIMIT 100 OFFSET 20", True),
            ("SELECT * FROM orders OFFSET 20", "SELECT * FROM orders LIMIT 100 OFFSET 20", True),
        ]
        for sql, expected_sql, expected_applied in cases:
            with self.subTest(sql=sql):
                normalized, checks = validate_and_normalize_sql(sql, max_rows=100)
                self.assertEqual(normalized, expected_sql)
                self.assertEqual(
                    checks.to_dict(),
                    {
                        "is_select_only": True,
                        "has_single_statement": True,
                        "limit_applied": expected_applied,
                    },
                )

    def test_rejects_dangerous_intent_in_chinese_and_english(self):
        for question in ("删除所有订单", "清空客户表", "更新商品价格", "DROP TABLE orders", "Insert an order"):
            with self.subTest(question=question):
                with self.assertRaises(AppError) as caught:
                    reject_dangerous_intent(question)
                self.assertEqual(caught.exception.code, 2006)
        reject_dangerous_intent("按月份统计订单总金额")


if __name__ == "__main__":
    unittest.main()
