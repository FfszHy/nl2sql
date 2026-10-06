import unittest
from unittest.mock import patch

from app.core.errors import AppError
from app.models.contracts import DataSourceConfig, QueryExplainRequest, QueryOptions, QueryRequest
from app.services import llm_service, query_service
from app.services.schema_service import apply_semantic_config, build_schema_context


class BusinessQueryTests(unittest.TestCase):
    def setUp(self):
        self.datasource = DataSourceConfig(
            host="localhost", user="reader", password="test-only", database="test_database",
        )
        self.question = "网站和App哪个渠道收入最多？比较购票人数。"
        self.schema = [{"table_name": "ticket_orders", "columns": [
            {"name": name, "type": datatype} for name, datatype in (
                ("sales_channel", "text"), ("customer_id", "integer"),
            )
        ]}]
        self.rewrite = {
            "original_question": self.question,
            "rewritten_question": self.question + " 网站对应web，App对应app；购票人数是去重顾客。",
            "value_mappings": [], "metrics": [], "assumptions": [],
            "required_tables": ["ticket_orders"], "profile_ids": ["test_profile"],
        }

    def test_semantic_failure_retries_before_execution_with_same_constraints(self):
        request = QueryRequest(
            question=self.question, datasource=self.datasource,
            options=QueryOptions(include_explanation=False),
        )
        bad_sql = "SELECT sales_channel FROM ticket_orders WHERE sales_channel = '网站'"
        good_sql = "SELECT sales_channel FROM ticket_orders WHERE sales_channel = 'web'"
        failure = AppError(1027, "网站必须对应web", "business_semantic_error", 422)
        with (
            patch.object(query_service, "_resolve_datasource", return_value=(self.datasource, self.schema, None)),
            patch.object(query_service, "_prepare_business_query", return_value=(self.schema, self.rewrite)),
            patch.object(query_service, "_build_retrieved_schema_context", return_value=("schema", None)) as retrieve,
            patch.object(query_service, "generate_sql", side_effect=[(bad_sql, {}), (good_sql, {})]) as generate,
            patch.object(query_service, "validate_business_sql", side_effect=[failure, None]) as validate,
            patch.object(query_service, "execute_select_sql", return_value=(["sales_channel"], [["web"]], 1)) as execute,
            patch.object(query_service, "generate_response", return_value="网站渠道。") as respond,
        ):
            result = query_service.execute_query(request, "business-retry")
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(validate.call_count, 2)
        execute.assert_called_once()
        self.assertNotIn("网站", execute.call_args.args[1])
        for call in generate.call_args_list:
            self.assertEqual(call.kwargs["question"], self.question)
            self.assertIs(call.kwargs["business_rewrite"], self.rewrite)
        self.assertTrue(generate.call_args_list[1].kwargs["error_feedback"].startswith(failure.message))
        self.assertIn(bad_sql, generate.call_args_list[1].kwargs["error_feedback"])
        self.assertEqual(retrieve.call_args.kwargs["question"], self.rewrite["rewritten_question"])
        self.assertEqual(respond.call_args.kwargs["question"], self.question)
        self.assertIs(respond.call_args.kwargs["business_rewrite"], self.rewrite)
        self.assertEqual(result["business_rewrite"], self.rewrite)

    def test_semantic_failure_without_retry_never_executes(self):
        request = QueryRequest(
            question=self.question, datasource=self.datasource,
            options=QueryOptions(retry_on_error=False),
        )
        with (
            patch.object(query_service, "_resolve_datasource", return_value=(self.datasource, self.schema, None)),
            patch.object(query_service, "_prepare_business_query", return_value=(self.schema, self.rewrite)),
            patch.object(query_service, "_build_retrieved_schema_context", return_value=("schema", None)),
            patch.object(query_service, "generate_sql", return_value=("SELECT customer_id FROM ticket_orders", {})),
            patch.object(query_service, "validate_business_sql", side_effect=AppError(1027, "人数口径不符", "business_semantic_error", 422)),
            patch.object(query_service, "execute_select_sql") as execute,
        ):
            with self.assertRaises(AppError):
                query_service.execute_query(request, "business-no-retry")
        execute.assert_not_called()

    def test_explain_uses_same_rewrite_and_retries_semantic_error_without_executing(self):
        request = QueryExplainRequest(question=self.question, datasource=self.datasource)
        failure = AppError(1027, "网站必须对应web", "business_semantic_error", 422)
        with (
            patch.object(query_service, "_resolve_datasource", return_value=(self.datasource, self.schema, None)),
            patch.object(query_service, "_prepare_business_query", return_value=(self.schema, self.rewrite)),
            patch.object(query_service, "_build_retrieved_schema_context", return_value=("schema", None)),
            patch.object(query_service, "generate_sql", side_effect=[
                ("SELECT sales_channel FROM ticket_orders WHERE sales_channel='网站'", {}),
                ("SELECT sales_channel FROM ticket_orders WHERE sales_channel='web'", {}),
            ]) as generate,
            patch.object(query_service, "validate_business_sql", side_effect=[failure, None]),
            patch.object(query_service, "generate_explanation", return_value="解释"),
            patch.object(query_service, "execute_select_sql") as execute,
        ):
            result = query_service.explain_query(request, "business-explain")
        self.assertEqual(generate.call_count, 2)
        self.assertTrue(generate.call_args.kwargs["error_feedback"].startswith(failure.message))
        self.assertIs(generate.call_args.kwargs["business_rewrite"], self.rewrite)
        self.assertEqual(result["business_rewrite"], self.rewrite)
        execute.assert_not_called()

    def test_dangerous_original_question_is_rejected_before_rewrite(self):
        request = QueryRequest(question="删除网站渠道的订单", datasource=self.datasource)
        with patch.object(query_service, "_prepare_business_query") as prepare:
            with self.assertRaises(AppError) as caught:
                query_service.execute_query(request, "business-dangerous")
        self.assertEqual(caught.exception.code, 2006)
        prepare.assert_not_called()

    def test_required_business_tables_survive_top_k_retrieval(self):
        schema = [{"table_name": name, "columns": []} for name in ("movies", "ticket_orders")]
        with patch.object(query_service, "retrieve_relevant_tables", return_value={"selected_tables": ["movies"]}):
            context, meta = query_service._build_retrieved_schema_context(
                "test-id", "question", schema, required_tables=["ticket_orders"],
            )
        self.assertIn("ticket_orders", context)
        self.assertEqual(meta["business_required_tables"], ["ticket_orders"])

    def test_configured_field_meaning_reaches_sql_prompt_without_mutating_schema(self):
        original = [{"table_name": "orders", "columns": [
            {"name": "buyer_id", "type": "bigint", "nullable": "NO", "comment": ""},
        ]}]
        enriched = apply_semantic_config(
            original,
            {("orders", "buyer_id"): {"field_comment": "购票顾客唯一编号", "field_aliases": ["顾客", "购票人"], "field_type": "wrong-type"}},
            {"orders": {"table_comment": "业务订单"}},
        )
        context = build_schema_context(enriched)
        with patch.object(llm_service, "_call_generation", return_value="SELECT buyer_id FROM orders") as call:
            llm_service.generate_sql(self.question, context, "db", 20, business_rewrite=self.rewrite)
        prompt = call.call_args.kwargs["messages"][1]["content"]
        self.assertIn("购票顾客唯一编号", prompt)
        self.assertIn("购票人", prompt)
        self.assertIn("网站对应web", prompt)
        self.assertIn("bigint", context)
        self.assertNotIn("wrong-type", context)
        self.assertEqual(original[0]["columns"][0]["comment"], "")


if __name__ == "__main__":
    unittest.main()
