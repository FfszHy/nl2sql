import unittest

from app.core.errors import AppError
from app.services.schema_sql_validation import validate_schema_sql


SCHEMA = [
    {"table_name": "cities", "columns": [{"name": name} for name in ["city_id", "city_name"]]},
    {"table_name": "cinemas", "columns": [{"name": name} for name in ["cinema_id", "city_id", "cinema_name"]]},
    {"table_name": "ticket_orders", "columns": [{"name": name} for name in ["order_id", "customer_id", "total_amount", "refund_amount"]]},
    {"table_name": "customers", "columns": [{"name": name} for name in ["customer_id", "membership_level"]]},
]


class SchemaSQLValidationTests(unittest.TestCase):
    def check(self, sql, schema=SCHEMA):
        validate_schema_sql(sql, schema, database_schema="public")

    def assert_schema_error(self, sql):
        with self.assertRaises(AppError) as caught:
            self.check(sql)
        self.assertEqual(caught.exception.code, 1028)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.error_type, "schema_validation_error")
        return caught.exception

    def test_nonexistent_physical_column_feedback_names_real_source(self):
        error = self.assert_schema_error("SELECT c.city FROM cinemas c")
        self.assertIn("c.city", error.message)
        self.assertIn("cinemas", error.message)
        self.assertIn("city_id", error.message)
        self.assertNotIn("city_name", error.message)

    def test_rejects_missing_table_and_unknown_alias(self):
        self.assert_schema_error("SELECT * FROM unknown_table")
        self.assert_schema_error("SELECT x.city_id FROM cinemas c")
        self.assert_schema_error("SELECT cinemas.city_id FROM cinemas c")

    def test_checks_all_expression_locations(self):
        for sql in [
            "SELECT cinema_id FROM cinemas WHERE nonexistent = 1",
            "SELECT cinema_id FROM cinemas HAVING nonexistent > 0",
            "SELECT cinema_id FROM cinemas ORDER BY nonexistent",
            "SELECT ROW_NUMBER() OVER (PARTITION BY nonexistent ORDER BY cinema_id) FROM cinemas",
            "SELECT c.cinema_id FROM cinemas c JOIN cities ci ON ci.nonexistent = c.city_id",
        ]:
            with self.subTest(sql=sql):
                self.assert_schema_error(sql)

    def test_unique_unqualified_join_column_is_valid_but_ambiguous_is_not(self):
        self.check("SELECT cinema_name FROM cinemas c JOIN cities ci ON ci.city_id = c.city_id")
        self.assert_schema_error("SELECT city_id FROM cinemas c JOIN cities ci ON ci.city_id = c.city_id")
        self.check("SELECT city_id FROM cinemas c JOIN cities ci USING (city_id)")

    def test_cte_star_subquery_and_window_rank(self):
        self.check(
            "WITH base AS (SELECT c.*, ci.city_name FROM cinemas c JOIN cities ci ON ci.city_id = c.city_id), "
            "ranked AS (SELECT base.*, ROW_NUMBER() OVER (PARTITION BY city_id ORDER BY cinema_id) AS position FROM base) "
            "SELECT city_name, cinema_name, position FROM ranked WHERE position <= 3 ORDER BY city_name, position"
        )
        self.check("SELECT x.total FROM (SELECT SUM(total_amount - refund_amount) AS total FROM ticket_orders) x")
        self.assert_schema_error("WITH x AS (SELECT cinema_id FROM cinemas) SELECT x.city_name FROM x")
        self.assert_schema_error("SELECT x.bad FROM (SELECT cinema_id FROM cinemas) x")

    def test_cte_column_alias_list_and_union(self):
        self.check("WITH x(id) AS (SELECT cinema_id FROM cinemas) SELECT id FROM x")
        self.check("SELECT c.id FROM cinemas c(id, city, title)")
        self.check("SELECT city_id AS id FROM cities UNION ALL SELECT city_id FROM cinemas ORDER BY id")

    def test_pg_output_alias_locations(self):
        self.check("SELECT membership_level AS level, COUNT(*) AS n FROM customers GROUP BY level ORDER BY n")
        for sql in [
            "SELECT total_amount AS revenue FROM ticket_orders WHERE revenue > 0",
            "SELECT COUNT(*) AS n FROM ticket_orders HAVING n > 1",
            "SELECT total_amount AS revenue, revenue + 1 AS adjusted FROM ticket_orders",
            "SELECT total_amount AS revenue FROM ticket_orders ORDER BY revenue + 1",
            "SELECT total_amount AS revenue, ROW_NUMBER() OVER (ORDER BY revenue) FROM ticket_orders",
        ]:
            with self.subTest(sql=sql):
                self.assert_schema_error(sql)

    def test_actual_input_column_wins_over_matching_output_alias(self):
        self.check("SELECT total_amount AS order_id FROM ticket_orders WHERE order_id > 1")

    def test_correlated_subquery_resolves_outer_alias(self):
        self.check("SELECT o.order_id FROM ticket_orders o WHERE EXISTS (SELECT 1 FROM customers c WHERE c.customer_id = o.customer_id)")
        self.assert_schema_error("SELECT o.order_id FROM ticket_orders o WHERE EXISTS (SELECT 1 FROM customers c WHERE c.customer_id = o.not_present)")

    def test_identifier_case_and_schema_binding(self):
        self.check("SELECT C.CITY_ID FROM PUBLIC.CINEMAS C")
        self.assert_schema_error('SELECT "CITY_ID" FROM cinemas')
        self.assert_schema_error("SELECT city_id FROM other_schema.cinemas")
        self.check('SELECT "ExactName" FROM "MixedTable"', [
            {"table_name": "MixedTable", "columns": [{"name": "ExactName"}]},
        ])


if __name__ == "__main__":
    unittest.main()
