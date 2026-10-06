import unittest

from sqlglot import exp, parse_one

from app.core.errors import AppError
from app.services.sql_security import (
    extract_used_columns,
    extract_used_tables,
    reject_dangerous_intent,
    validate_and_normalize_sql,
)


CITY_CINEMA_REVENUE_SQL = """
WITH cinema_revenue AS (
    SELECT ci.city_id, ci.city_name, c.cinema_id, c.cinema_name,
           SUM(o.total_amount - o.refund_amount) AS net_revenue
    FROM ticket_orders AS o
    JOIN screenings AS s ON s.screening_id = o.screening_id
    JOIN cinemas AS c ON c.cinema_id = s.cinema_id
    JOIN cities AS ci ON ci.city_id = c.city_id
    GROUP BY ci.city_id, ci.city_name, c.cinema_id, c.cinema_name
), city_revenue AS (
    SELECT city_id, city_name, SUM(net_revenue) AS city_net_revenue
    FROM cinema_revenue GROUP BY city_id, city_name
), top_cities AS (
    SELECT * FROM city_revenue
    ORDER BY city_net_revenue DESC, city_id LIMIT 5
), ranked_cinemas AS (
    SELECT cr.*, tc.city_net_revenue,
           ROW_NUMBER() OVER (
               PARTITION BY cr.city_id ORDER BY cr.net_revenue DESC, cr.cinema_id
           ) AS cinema_rank
    FROM cinema_revenue AS cr
    JOIN top_cities AS tc ON tc.city_id = cr.city_id
)
SELECT city_name, cinema_name, net_revenue, city_net_revenue,
       100.0 * net_revenue / NULLIF(city_net_revenue, 0) AS city_revenue_pct
FROM ranked_cinemas WHERE cinema_rank <= 3
ORDER BY city_net_revenue DESC, city_id, cinema_rank
""".strip()


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

    def test_accepts_multiple_nested_and_recursive_read_only_ctes(self):
        cases = [
            "WITH paid AS (SELECT * FROM orders) SELECT * FROM paid",
            "WITH a AS (SELECT * FROM orders), b AS (SELECT * FROM a) SELECT * FROM b",
            "WITH outer_query AS (WITH inner_query AS (SELECT * FROM orders) "
            "SELECT * FROM inner_query) SELECT * FROM outer_query",
            "WITH RECURSIVE numbers(n) AS (SELECT 1 UNION ALL "
            "SELECT n + 1 FROM numbers WHERE n < 5) SELECT n FROM numbers",
            'WITH "Paid Orders" AS MATERIALIZED (SELECT * FROM orders) '
            'SELECT * FROM "Paid Orders"',
        ]
        for sql in cases:
            with self.subTest(sql=sql):
                normalized, checks = validate_and_normalize_sql(sql, max_rows=100)
                parsed = parse_one(normalized, read="postgres")
                self.assertEqual(parsed.args["limit"].expression.this, "100")
                self.assertTrue(checks.is_select_only)
                self.assertTrue(checks.has_single_statement)

    def test_accepts_refund_city_cinema_ranking_without_limiting_cte_calculations(self):
        normalized, checks = validate_and_normalize_sql(CITY_CINEMA_REVENUE_SQL, max_rows=100)
        parsed = parse_one(normalized, read="postgres")
        ctes = {cte.alias: cte.this for cte in parsed.find_all(exp.CTE)}
        self.assertEqual(parsed.args["limit"].expression.this, "100")
        self.assertEqual(ctes["top_cities"].args["limit"].expression.this, "5")
        self.assertIsNone(ctes["cinema_revenue"].args.get("limit"))
        self.assertIsNone(ctes["city_revenue"].args.get("limit"))
        self.assertEqual(
            extract_used_tables(normalized),
            ["ticket_orders", "screenings", "cinemas", "cities"],
        )
        self.assertTrue(checks.limit_applied)

    def test_rejects_data_modifying_ctes_and_outer_merge(self):
        cases = [
            "WITH changed AS (DELETE FROM orders RETURNING id) SELECT * FROM changed",
            "WITH changed AS (UPDATE orders SET total = 0 RETURNING id) SELECT * FROM changed",
            "WITH changed AS (INSERT INTO orders (id) VALUES (1) RETURNING id) "
            "SELECT * FROM changed",
            "WITH changed AS (MERGE INTO orders AS o USING source_orders AS s "
            "ON o.id = s.id WHEN MATCHED THEN DELETE RETURNING o.id) SELECT * FROM changed",
            "WITH x AS (SELECT 1) MERGE INTO orders AS o USING source_orders AS s "
            "ON o.id = s.id WHEN MATCHED THEN DELETE",
            "WITH outer_query AS (WITH changed AS (DELETE FROM orders RETURNING id) "
            "SELECT * FROM changed) SELECT * FROM outer_query",
        ]
        for sql in cases:
            with self.subTest(sql=sql):
                with self.assertRaises(AppError) as caught:
                    validate_and_normalize_sql(sql, max_rows=100)
                self.assertEqual(caught.exception.error_type, "sql_security_error")
                self.assertEqual(caught.exception.status_code, 400)

    def test_ctes_keep_existing_security_restrictions(self):
        cases = [
            ("WITH x AS (SELECT pg_sleep(1)) SELECT * FROM x", 2002),
            ("WITH x AS (SELECT * FROM pg_catalog.pg_tables) SELECT * FROM x", 2003),
            ("WITH x AS (SELECT * FROM orders) SELECT * FROM x -- ignored cap", 2007),
            ("WITH x AS (SELECT 1) SELECT * FROM x; SELECT 2", 2004),
        ]
        for sql, expected_code in cases:
            with self.subTest(sql=sql):
                with self.assertRaises(AppError) as caught:
                    validate_and_normalize_sql(sql, max_rows=100)
                self.assertEqual(caught.exception.code, expected_code)

    def test_only_outer_result_limit_is_capped(self):
        sql = "WITH recent AS (SELECT * FROM orders LIMIT 5) SELECT * FROM recent LIMIT 1000 OFFSET 20"
        normalized, checks = validate_and_normalize_sql(sql, max_rows=100)
        parsed = parse_one(normalized, read="postgres")
        self.assertEqual(parsed.args["limit"].expression.this, "100")
        self.assertEqual(parsed.args["offset"].expression.this, "20")
        self.assertEqual(next(parsed.find_all(exp.CTE)).this.args["limit"].expression.this, "5")
        self.assertTrue(checks.limit_applied)

    def test_union_limit_caps_whole_result_and_preserves_branch_limit(self):
        sql = "SELECT id FROM orders UNION ALL (SELECT id FROM archive_orders LIMIT 5)"
        normalized, _ = validate_and_normalize_sql(sql, max_rows=100)
        parsed = parse_one(normalized, read="postgres")
        self.assertIsInstance(parsed, exp.Union)
        self.assertEqual(parsed.args["limit"].expression.this, "100")
        self.assertEqual(parsed.expression.this.args["limit"].expression.this, "5")

    def test_small_outer_limit_and_fetch_are_never_enlarged(self):
        cases = [
            "WITH x AS (SELECT * FROM orders) SELECT * FROM x LIMIT 5",
            "SELECT * FROM orders LIMIT 0",
            "SELECT * FROM orders LIMIT (5)",
            "SELECT * FROM orders FETCH FIRST 5 ROWS ONLY",
        ]
        for sql in cases:
            with self.subTest(sql=sql):
                normalized, checks = validate_and_normalize_sql(sql, max_rows=100)
                self.assertEqual(normalized, sql)
                self.assertFalse(checks.limit_applied)

    def test_computed_limit_is_preserved_beneath_outer_cap(self):
        sql = "SELECT * FROM orders LIMIT 2 + 3 OFFSET 4"
        normalized, checks = validate_and_normalize_sql(sql, max_rows=100)
        parsed = parse_one(normalized, read="postgres")
        self.assertEqual(parsed.args["limit"].expression.this, "100")
        preserved = [
            query for query in parsed.find_all(exp.Select)
            if isinstance(query.args.get("limit"), exp.Limit)
            and isinstance(query.args["limit"].expression, exp.Add)
        ]
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].args["offset"].expression.this, "4")
        self.assertTrue(checks.limit_applied)

    def test_fetch_with_ties_is_preserved_beneath_outer_cap(self):
        sql = "SELECT * FROM orders ORDER BY total DESC FETCH FIRST 5 ROWS WITH TIES"
        normalized, checks = validate_and_normalize_sql(sql, max_rows=100)
        parsed = parse_one(normalized, read="postgres")
        self.assertEqual(parsed.args["limit"].expression.this, "100")
        fetches = list(parsed.find_all(exp.Fetch))
        self.assertEqual(len(fetches), 1)
        self.assertEqual(fetches[0].args["count"].this, "5")
        self.assertTrue(fetches[0].args["limit_options"].args["with_ties"])
        self.assertTrue(checks.limit_applied)

    def test_unlimited_and_large_fetch_results_are_capped(self):
        cases = [
            "SELECT * FROM orders LIMIT ALL",
            "SELECT * FROM orders LIMIT NULL",
            "SELECT * FROM orders OFFSET 20 FETCH FIRST 1000 ROWS ONLY",
        ]
        for sql in cases:
            with self.subTest(sql=sql):
                normalized, checks = validate_and_normalize_sql(sql, max_rows=100)
                parsed = parse_one(normalized, read="postgres")
                self.assertEqual(parsed.args["limit"].expression.this, "100")
                if "OFFSET" in sql:
                    self.assertEqual(parsed.args["offset"].expression.this, "20")
                self.assertTrue(checks.limit_applied)

    def test_extracts_physical_tables_using_cte_scope(self):
        cases = [
            ("WITH orders AS (SELECT * FROM public.orders) SELECT * FROM orders", ["orders"]),
            ("WITH orders AS (SELECT * FROM sales) "
             "SELECT * FROM public.orders AS physical_orders JOIN orders AS cte_orders USING (id)",
             ["sales", "orders"]),
            ("WITH outer_query AS (WITH inner_query AS (SELECT * FROM orders) "
             "SELECT * FROM inner_query) SELECT * FROM outer_query JOIN inner_query USING (id)",
             ["orders", "inner_query"]),
        ]
        for sql, expected_tables in cases:
            with self.subTest(sql=sql):
                self.assertEqual(extract_used_tables(sql), expected_tables)

    def test_extracts_outer_columns_instead_of_cte_projection(self):
        sql = "WITH totals AS (SELECT city_id, SUM(total) AS revenue FROM orders GROUP BY city_id) "
        sql += "SELECT city_id, COALESCE(revenue, 0) AS net_revenue FROM totals"
        self.assertEqual(extract_used_columns(sql), ["city_id", "COALESCE(revenue, 0)"])

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
