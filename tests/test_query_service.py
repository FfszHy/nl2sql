import json
import unittest
from datetime import datetime
from unittest.mock import patch

from sqlglot import parse_one

from app.core.errors import AppError
from app.models.contracts import DataSourceConfig, QueryOptions, QueryRequest
from app.services import llm_service, query_service


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
            {key: payloads[0][key] for key in ("event", "trace_id", "sql", "error_type", "error_code")},
            {
                "event": "sql_validation_failed",
                "trace_id": self.trace_id,
                "sql": sql,
                "error_type": caught.exception.error_type,
                "error_code": caught.exception.code,
            },
        )
        self.assertEqual(payloads[0]["error_message"], caught.exception.message)
        self.assertEqual(payloads[0]["phase"], "sql_validation")
        self.assertIsNone(payloads[0]["sqlstate"])
        self.assertTrue(payloads[0]["timestamp"].endswith("Z"))
        datetime.fromisoformat(payloads[0]["timestamp"].replace("Z", "+00:00"))
        self.assertNotIn("rows", payloads[0])

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


class QueryChartTests(unittest.TestCase):
    def setUp(self):
        self.datasource = DataSourceConfig(
            host="localhost", user="test_reader", password="test-only-placeholder", database="test_database",
        )
        self.result = {
            "sql": "SELECT city, revenue FROM report LIMIT 100",
            "columns": ["city", "revenue"], "rows": [["上海", 120], ["北京", 100]], "row_count": 2,
            "used_tables": ["report"], "safety_checks": {},
            "response": "上海收入为 120。", "explanation": None,
        }
        self.raw_chart = json.dumps({
            "version": 1, "type": "bar", "category": ["city"], "series": [{"field": "revenue"}],
        })

    def execute(self, include_chart=True):
        request = QueryRequest(
            question="各城市收入", datasource=self.datasource,
            options=QueryOptions(include_chart=include_chart, include_explanation=False),
        )
        with (
            patch.object(query_service, "_resolve_datasource", return_value=(self.datasource, [{"table_name": "report"}], None)),
            patch.object(query_service, "_build_retrieved_schema_context", return_value=("test schema", None)),
            patch.object(query_service, "_run_once", return_value=(dict(self.result), {})),
        ):
            return query_service.execute_query(request, trace_id="test-chart-trace")

    def test_requested_chart_is_selected_locally_and_keeps_query_result(self):
        with (
            patch.object(llm_service, "_call_generation", side_effect=AssertionError("Chart selection must stay local")) as call,
            self.assertLogs("app.audit.query", level="INFO") as logs,
        ):
            data = self.execute()
        call.assert_not_called()
        self.assertEqual(data["chart_config"]["type"], "bar")
        self.assertEqual(data["chart_config"]["category"], ["city"])
        self.assertEqual(data["chart_config"]["series"][0]["field"], "revenue")
        self.assertEqual(data["chart_config"]["series"][0]["axis"], "primary")
        self.assertIsNone(data["chart_error"])
        self.assertIn("reason", data["chart_selection"])
        self.assertIn("profile", data["chart_selection"])
        self.assertNotIn("echarts_code", data)
        self.assertEqual(data["rows"], self.result["rows"])
        self.assertEqual(data["response"], self.result["response"])
        payloads = [json.loads(record.getMessage()) for record in logs.records]
        selected = next(p for p in payloads if p["event"] == "chart_selected")
        self.assertEqual(selected["chart_type"], "bar")
        self.assertNotIn("profile", selected)
        self.assertNotIn("rows", selected)

    def test_unrequested_chart_skips_planning(self):
        with patch.object(query_service, "plan_chart") as chart:
            data = self.execute(include_chart=False)
        chart.assert_not_called()
        self.assertIsNone(data["chart_config"])
        self.assertIsNone(data["chart_error"])
        self.assertIsNone(data["chart_selection"])

    def test_invalid_chart_preserves_success_and_logs_only_safe_metadata(self):
        hostile_output = "(()=>{fetch('/malicious-model-code');return {}})()"
        selection = {"config": hostile_output, "intent": "comparison", "reason": {"code": "fixture"}, "profile": {}}
        with (
            patch.object(query_service, "plan_chart", return_value=selection),
            self.assertLogs("app.audit.query", level="INFO") as logs,
        ):
            data = self.execute()
        self.assertIsNone(data["chart_config"])
        self.assertEqual(data["chart_error"], "图表暂时无法生成，查询结果仍可查看。")
        for key in ("sql", "columns", "rows", "row_count", "response"):
            self.assertEqual(data[key], self.result[key])
        payloads = [json.loads(record.getMessage()) for record in logs.records]
        chart_event = next(payload for payload in payloads if payload["event"] == "chart_failed")
        self.assertEqual({key: chart_event[key] for key in ("event", "trace_id", "error_type", "error_code")}, {
            "event": "chart_failed", "trace_id": "test-chart-trace", "error_type": "chart_validation_error", "error_code": 1014,
        })
        self.assertTrue(any(payload["event"] == "query_succeeded" for payload in payloads))
        self.assertNotIn(hostile_output, "\n".join(logs.output))

    def test_chart_planning_errors_do_not_leak_detail_or_fail_query(self):
        for private_error in (AppError(1014, "private planner detail", "chart_validation_error", 502),
                              ValueError("private planner detail")):
            with (
                self.subTest(error_type=type(private_error).__name__),
                patch.object(query_service, "plan_chart", side_effect=private_error),
                self.assertLogs("app.audit.query", level="INFO") as logs,
            ):
                data = self.execute()
            self.assertEqual(data["rows"], self.result["rows"])
            self.assertIsNone(data["chart_config"])
            self.assertNotIn("private planner detail", data["chart_error"])
            self.assertNotIn("private planner detail", "\n".join(logs.output))

    def test_no_applicable_chart_is_a_reasoned_table_result_without_error(self):
        selection = {"config": None, "intent": "scalar", "reason": {"code": "single_metric", "message": "仅有一个数值"}, "profile": {}}
        with patch.object(query_service, "plan_chart", return_value=selection):
            data = self.execute()
        self.assertIsNone(data["chart_config"])
        self.assertIsNone(data["chart_error"])
        self.assertEqual(data["chart_selection"]["reason"]["code"], "single_metric")
        self.assertEqual(data["rows"], self.result["rows"])

    def test_reaching_query_limit_is_conservatively_marked_as_truncated_for_pie(self):
        self.result["row_count"] = 200
        selection = {"config": None, "intent": "composition", "reason": {"code": "truncated"}, "profile": {}}
        with patch.object(query_service, "plan_chart", return_value=selection) as plan:
            self.execute()
        self.assertTrue(plan.call_args.kwargs["rows_truncated"])

    def test_schema_metadata_query_skips_chart_generation(self):
        request = QueryRequest(
            question="数据库有多少张表", datasource=self.datasource,
            options=QueryOptions(include_chart=True, include_explanation=False),
        )
        with (
            patch.object(query_service, "_resolve_datasource", return_value=(self.datasource, [{"table_name": "report"}], None)),
            patch.object(query_service, "_build_retrieved_schema_context", return_value=("test schema", None)),
            patch.object(query_service, "_run_once", side_effect=AppError(2003, "metadata", "sql_security_error")),
            patch.object(query_service, "_build_schema_meta_result", return_value=dict(self.result)),
            patch.object(query_service, "plan_chart") as chart,
        ):
            data = query_service.execute_query(request, trace_id="test-chart-trace")
        chart.assert_not_called()
        self.assertIsNone(data["chart_config"])


if __name__ == "__main__":
    unittest.main()
