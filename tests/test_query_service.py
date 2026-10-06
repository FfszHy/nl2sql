import json
import unittest
from unittest.mock import patch

from sqlglot import parse_one

from app.core.errors import AppError
from app.models.contracts import DataSourceConfig
from app.services import query_service


class QueryValidationTests(unittest.TestCase):
    def setUp(self):
        self.datasource = DataSourceConfig(
            host="localhost",
            user="test_reader",
            password="test-only-placeholder",
            database="test_database",
        )
        self.trace_id = "test-query-trace"

    def test_rejected_sql_never_reaches_database_and_audit_records_candidate(self):
        sql = "WITH changed AS (DELETE FROM ticket_orders RETURNING order_id) SELECT * FROM changed"
        with (
            patch.object(query_service, "generate_sql", return_value=(sql, {})),
            patch.object(query_service, "execute_select_sql") as execute,
            patch.object(query_service, "generate_response") as respond,
            self.assertLogs("app.audit.query", level="INFO") as logs,
        ):
            with self.assertRaises(AppError) as caught:
                query_service._run_once(
                    question="统计收入",
                    datasource=self.datasource,
                    schema_context="test schema",
                    max_rows=100,
                    include_explanation=False,
                    trace_id=self.trace_id,
                )

        execute.assert_not_called()
        respond.assert_not_called()
        self.assertEqual(caught.exception.error_type, "sql_security_error")
        payloads = [json.loads(record.getMessage()) for record in logs.records]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(
            payloads[0],
            {
                "event": "sql_validation_failed",
                "trace_id": self.trace_id,
                "sql": sql,
                "error_type": caught.exception.error_type,
                "error_code": caught.exception.code,
            },
        )

    def test_read_only_cte_executes_capped_sql_and_reports_actual_tables(self):
        sql = (
            "WITH net_orders AS (SELECT order_id, total_amount - refund_amount AS net_revenue "
            "FROM ticket_orders) SELECT order_id, net_revenue FROM net_orders"
        )
        prompt_meta = {"rewritten_question": "统计扣退款后的收入"}
        with (
            patch.object(query_service, "generate_sql", return_value=(sql, prompt_meta)),
            patch.object(
                query_service, "execute_select_sql",
                return_value=(["order_id", "net_revenue"], [[1, 125]], 1),
            ) as execute,
            patch.object(query_service, "generate_response", return_value="示例答复"),
            patch.object(query_service, "generate_explanation") as explain,
        ):
            data, returned_meta = query_service._run_once(
                question="统计收入",
                datasource=self.datasource,
                schema_context="test schema",
                max_rows=100,
                include_explanation=False,
                trace_id=self.trace_id,
            )

        execute.assert_called_once_with(self.datasource, data["sql"])
        parsed = parse_one(data["sql"], read="postgres")
        self.assertEqual(parsed.args["limit"].expression.this, "100")
        self.assertEqual(data["used_tables"], ["ticket_orders"])
        self.assertTrue(data["safety_checks"]["is_select_only"])
        self.assertEqual(data["rows"], [[1, 125]])
        self.assertEqual(data["response"], "示例答复")
        self.assertIsNone(data["explanation"])
        self.assertEqual(returned_meta, prompt_meta)
        explain.assert_not_called()


if __name__ == "__main__":
    unittest.main()
