import unittest
from contextlib import ExitStack
from unittest.mock import patch

from app.core.errors import AppError
from app.models.contracts import DataSourceConfig, QueryExplainRequest, QueryOptions, QueryRequest
from app.services import query_service


class QueryRepairTests(unittest.TestCase):
    def setUp(self):
        self.datasource = DataSourceConfig(host="localhost", user="reader", password="test-only", database="demo")
        self.schema = [{"table_name": "cities", "columns": [
            {"name": "city_id", "type": "integer"}, {"name": "city_name", "type": "text"},
        ]}]
        self.plan = {
            "rewritten_question": "各城市的名称", "required_tables": ["cities"],
            "profile_ids": [], "metrics": [], "value_mappings": [],
        }

    def harness(self, stack, candidates, max_attempts=3):
        stack.enter_context(patch.object(query_service, "_resolve_datasource", return_value=(self.datasource, self.schema, None)))
        stack.enter_context(patch.object(query_service, "_prepare_business_query", return_value=(self.schema, self.plan)))
        stack.enter_context(patch.object(query_service, "_build_retrieved_schema_context", return_value=("retrieved context", None)))
        stack.enter_context(patch.object(query_service.settings, "sql_generation_max_attempts", max_attempts))
        generate = stack.enter_context(patch.object(query_service, "generate_sql", side_effect=[(sql, {}) for sql in candidates]))
        execute = stack.enter_context(patch.object(query_service, "execute_select_sql", return_value=(["city_name"], [["City_001"]], 1)))
        respond = stack.enter_context(patch.object(query_service, "generate_response", return_value="City_001"))
        return generate, execute, respond

    def request(self, retry=True):
        return QueryRequest(question="各城市的名称", datasource=self.datasource,
                            options=QueryOptions(include_explanation=False, retry_on_error=retry))

    def test_stacked_select_then_missing_column_are_repaired_before_one_execution(self):
        candidates = ["SELECT city_name FROM cities; SELECT city_id FROM cities",
                      "SELECT c.city FROM cities c", "SELECT c.city_name FROM cities c"]
        with ExitStack() as stack:
            generate, execute, respond = self.harness(stack, candidates)
            result = query_service.execute_query(self.request(), "repair-chain")
        self.assertEqual(generate.call_count, 3)
        execute.assert_called_once()
        respond.assert_called_once()
        self.assertIn("city_name", execute.call_args.args[1])
        self.assertNotIn(";", execute.call_args.args[1])
        self.assertIn(candidates[0], generate.call_args_list[1].kwargs["error_feedback"])
        self.assertIn("city_name", generate.call_args_list[2].kwargs["error_feedback"])
        self.assertIn("表 cities", generate.call_args_list[1].kwargs["schema_context"])
        self.assertEqual(result["sql_generation"]["attempt_count"], 3)
        self.assertEqual([item["error_code"] for item in result["sql_generation"]["failures"]], [2004, 1028])

    def test_dangerous_stacked_candidate_is_not_retried_or_executed(self):
        with ExitStack() as stack:
            generate, execute, _ = self.harness(stack, ["SELECT city_name FROM cities; DELETE FROM cities"])
            with self.assertRaises(AppError) as caught:
                query_service.execute_query(self.request(), "repair-danger")
        self.assertEqual(caught.exception.code, 2004)
        generate.assert_called_once()
        execute.assert_not_called()

    def test_retry_disabled_keeps_stacked_selects_blocked(self):
        with ExitStack() as stack:
            generate, execute, _ = self.harness(stack, ["SELECT 1; SELECT 2"])
            with self.assertRaises(AppError):
                query_service.execute_query(self.request(retry=False), "repair-off")
        generate.assert_called_once()
        execute.assert_not_called()

    def test_repeated_schema_failure_stops_at_configured_limit_without_execution(self):
        with ExitStack() as stack:
            generate, execute, _ = self.harness(stack, ["SELECT c.city FROM cities c"] * 3)
            with self.assertRaises(AppError) as caught:
                query_service.execute_query(self.request(), "repair-bounded")
        self.assertEqual(caught.exception.code, 1028)
        self.assertEqual(generate.call_count, 3)
        execute.assert_not_called()

    def test_extended_budget_reaches_fifth_candidate_and_validates_every_attempt(self):
        candidates = ["SELECT c.city FROM cities c"] * 4 + ["SELECT c.city_name FROM cities c"]
        with ExitStack() as stack:
            generate, execute, _ = self.harness(stack, candidates, max_attempts=5)
            validate = stack.enter_context(patch.object(query_service, "validate_schema_sql", wraps=query_service.validate_schema_sql))
            result = query_service.execute_query(self.request(), "repair-fifth-candidate")
        self.assertEqual(generate.call_count, 5)
        self.assertEqual(validate.call_count, 5)
        execute.assert_called_once()
        self.assertEqual(result["sql_generation"]["attempt_count"], 5)
        self.assertEqual(len(result["sql_generation"]["failures"]), 4)
        for call in generate.call_args_list[1:]:
            self.assertIn("city", call.kwargs["error_feedback"])

    def test_extended_budget_exhaustion_stops_and_never_executes_bad_candidates(self):
        with ExitStack() as stack:
            generate, execute, _ = self.harness(stack, ["SELECT c.city FROM cities c"] * 5, max_attempts=5)
            with self.assertRaises(AppError) as caught:
                query_service.execute_query(self.request(), "repair-five-bounded")
        self.assertEqual(caught.exception.code, 1028)
        self.assertEqual(generate.call_count, 5)
        execute.assert_not_called()

    def test_extended_budget_does_not_override_disabled_retry(self):
        with ExitStack() as stack:
            generate, execute, _ = self.harness(stack, ["SELECT c.city FROM cities c"] * 5, max_attempts=5)
            with self.assertRaises(AppError):
                query_service.execute_query(self.request(retry=False), "repair-five-off")
        generate.assert_called_once()
        execute.assert_not_called()

    def test_explain_reaches_fifth_candidate_without_database_execution(self):
        candidates = ["SELECT c.city FROM cities c"] * 4 + ["SELECT c.city_name FROM cities c"]
        with ExitStack() as stack:
            generate, execute, _ = self.harness(stack, candidates, max_attempts=5)
            stack.enter_context(patch.object(query_service, "generate_explanation", return_value="解释"))
            result = query_service.explain_query(QueryExplainRequest(question="各城市的名称", datasource=self.datasource), "explain-five")
        self.assertEqual(generate.call_count, 5)
        execute.assert_not_called()
        self.assertEqual(result["sql_generation"]["attempt_count"], 5)

    def test_business_failure_reason_reaches_repair_and_next_candidate_still_validates(self):
        failure = AppError(1027, "实际输出口径不符合", "business_semantic_error", 422)
        failure.business_failure_reason = "join_fanout"
        failure.business_failure_details = {"metric_id": "city_count", "output_alias": "city_count", "source_table": "cities"}
        with ExitStack() as stack:
            generate, execute, _ = self.harness(stack, ["SELECT city_name FROM cities"] * 2)
            validate = stack.enter_context(patch.object(query_service, "validate_business_sql", side_effect=[failure, None]))
            result = query_service.execute_query(self.request(), "structured-repair")
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(validate.call_count, 2)
        execute.assert_called_once()
        feedback = generate.call_args.kwargs["error_feedback"]
        self.assertIn('"reason": "join_fanout"', feedback)
        self.assertIn('"source_table": "cities"', feedback)
        self.assertIn("分别聚合", feedback)
        self.assertEqual(result["sql_generation"]["attempt_count"], 2)

    def test_structured_business_failure_does_not_extend_candidate_budget(self):
        failure = AppError(1027, "平均值空值处理不符合", "business_semantic_error", 422)
        failure.business_failure_reason = "nullable_aggregate_zero_fill"
        with ExitStack() as stack:
            generate, execute, _ = self.harness(stack, ["SELECT city_name FROM cities"] * 3)
            validate = stack.enter_context(patch.object(query_service, "validate_business_sql", side_effect=failure))
            with self.assertRaises(AppError) as caught:
                query_service.execute_query(self.request(), "structured-bounded")
        self.assertIs(caught.exception, failure)
        self.assertEqual(generate.call_count, 3)
        self.assertEqual(validate.call_count, 3)
        execute.assert_not_called()
        self.assertIn("不要为了消除NULL将LEFT JOIN改成INNER JOIN", generate.call_args.kwargs["error_feedback"])

    def test_unsafe_candidate_after_structured_hint_is_not_executed_or_retried(self):
        failure = AppError(1027, "关联会重复", "business_semantic_error", 422)
        failure.business_failure_reason = "join_fanout"
        with ExitStack() as stack:
            generate, execute, _ = self.harness(stack, [
                "SELECT city_name FROM cities", "SELECT city_name FROM cities; DELETE FROM cities",
            ])
            validate = stack.enter_context(patch.object(query_service, "validate_business_sql", side_effect=failure))
            with self.assertRaises(AppError) as caught:
                query_service.execute_query(self.request(), "structured-security")
        self.assertEqual(caught.exception.code, 2004)
        self.assertEqual(generate.call_count, 2)
        validate.assert_called_once()
        execute.assert_not_called()

    def test_explain_uses_structured_feedback_without_executing_query(self):
        failure = AppError(1027, "排名输入指标不符合", "business_semantic_error", 422)
        failure.business_failure_reason = "rank_metric_mismatch"
        with ExitStack() as stack:
            generate, execute, _ = self.harness(stack, ["SELECT city_name FROM cities"] * 2)
            validate = stack.enter_context(patch.object(query_service, "validate_business_sql", side_effect=[failure, None]))
            stack.enter_context(patch.object(query_service, "generate_explanation", return_value="解释"))
            result = query_service.explain_query(QueryExplainRequest(question="各城市的名称", datasource=self.datasource), "explain-structured")
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(validate.call_count, 2)
        execute.assert_not_called()
        self.assertIn("ROUND只放最外层", generate.call_args.kwargs["error_feedback"])
        self.assertEqual(result["sql_generation"]["attempt_count"], 2)

    def test_database_preflight_feedback_repairs_valid_names_but_invalid_query(self):
        candidates = ["SELECT city_name FROM cities", "SELECT city_name FROM cities"]
        with ExitStack() as stack:
            generate, execute, respond = self.harness(stack, candidates)
            execute.side_effect = [AppError(1003, "SQL 预检失败: grouping error", "db_error"),
                                   (["city_name"], [["City_001"]], 1)]
            result = query_service.execute_query(self.request(), "repair-preflight")
        self.assertEqual(generate.call_count, 2)
        respond.assert_called_once()
        self.assertIn("grouping error", generate.call_args.kwargs["error_feedback"])
        self.assertTrue(result["sql_generation"]["repaired"])

    def test_answer_provider_failure_does_not_regenerate_or_repeat_query(self):
        with ExitStack() as stack:
            generate, execute, respond = self.harness(stack, ["SELECT city_name FROM cities"])
            respond.side_effect = AppError(1006, "answer service unavailable", "llm_error", 502)
            with self.assertRaises(AppError) as caught:
                query_service.execute_query(self.request(), "answer-failure")
        self.assertEqual(caught.exception.code, 1006)
        generate.assert_called_once()
        execute.assert_called_once()

    def test_connection_loss_after_query_start_does_not_regenerate_sql(self):
        failure = AppError(1003, "connection lost", "db_error")
        failure.retryable_sql = False
        with ExitStack() as stack:
            generate, execute, _ = self.harness(stack, ["SELECT city_name FROM cities"])
            execute.side_effect = failure
            with self.assertRaises(AppError):
                query_service.execute_query(self.request(), "query-connection-loss")
        generate.assert_called_once()
        execute.assert_called_once()

    def database_failure(self, state):
        error = AppError(1003, "SQL 预检失败: stale schema", "db_error", 400)
        error.sqlstate, error.phase, error.retryable_sql = state, "preflight", True
        return error

    def test_permission_denied_never_regenerates_or_refreshes(self):
        failure = self.database_failure("42501")
        with ExitStack() as stack:
            generate, execute, _ = self.harness(stack, ["SELECT city_name FROM cities"])
            refresh = stack.enter_context(patch.object(query_service, "refresh_schema_cache"))
            execute.side_effect = failure
            with self.assertRaises(AppError) as caught:
                query_service.execute_query(self.request(), "permission-denied")
        self.assertIs(caught.exception, failure)
        generate.assert_called_once()
        execute.assert_called_once()
        refresh.assert_not_called()

    def test_registered_stale_schema_refreshes_once_and_rebuilds_business_contract_within_budget(self):
        for state in ("42703", "42P01"):
            with self.subTest(sqlstate=state), ExitStack() as stack:
                generate, execute, _ = self.harness(stack, [
                    "SELECT city_name FROM cities", "SELECT city_label FROM cities", "SELECT city_label FROM cities",
                ])
                stack.enter_context(patch.object(query_service, "_resolve_datasource", return_value=(self.datasource, self.schema, "registered-demo")))
                fresh_schema = [{"table_name": "cities", "columns": [{"name": "city_label", "type": "text"}]}]
                fresh_plan = {**self.plan, "query_contract": {"schema_version": "fresh"}}
                prepare = stack.enter_context(patch.object(query_service, "_prepare_business_query", side_effect=[(self.schema, self.plan), (fresh_schema, fresh_plan)]))
                refresh = stack.enter_context(patch.object(query_service, "refresh_schema_cache", return_value={"refreshed": True}))
                fetch = stack.enter_context(patch.object(query_service, "fetch_schema_with_cache", return_value=(self.datasource, fresh_schema)))
                execute.side_effect = [self.database_failure(state), self.database_failure(state), (["city_label"], [["City_001"]], 1)]
                result = query_service.execute_query(self.request(), "schema-refresh")
            refresh.assert_called_once_with("registered-demo")
            fetch.assert_called_once_with("registered-demo")
            self.assertEqual(prepare.call_count, 2)
            self.assertEqual(generate.call_count, 3)
            self.assertIs(generate.call_args_list[1].kwargs["business_rewrite"], fresh_plan)
            self.assertIn("city_label", generate.call_args_list[1].kwargs["schema_context"])
            self.assertNotIn("city_name", generate.call_args_list[1].kwargs["schema_context"])
            self.assertEqual(result["sql_generation"]["attempt_count"], 3)
            self.assertTrue(result["sql_generation"]["schema_refreshed"])

    def test_business_profile_or_metric_loss_after_refresh_fails_closed(self):
        old_plan = {**self.plan, "profile_ids": ["business"], "metrics": [
            {"id": "city_count", "table": "cities", "expression": "COUNT(*)", "output_alias": "city_count"},
        ]}
        for refreshed_plan in ({**self.plan, "profile_ids": []}, {**self.plan, "profile_ids": ["business"]}):
            with self.subTest(profile_ids=refreshed_plan["profile_ids"]), ExitStack() as stack:
                generate, execute, _ = self.harness(stack, ["SELECT COUNT(*) AS city_count FROM cities"])
                stack.enter_context(patch.object(query_service, "_resolve_datasource", return_value=(self.datasource, self.schema, "registered-demo")))
                stack.enter_context(patch.object(query_service, "_prepare_business_query", side_effect=[(self.schema, old_plan), (self.schema, refreshed_plan)]))
                stack.enter_context(patch.object(query_service, "refresh_schema_cache", return_value={"refreshed": True}))
                stack.enter_context(patch.object(query_service, "fetch_schema_with_cache", return_value=(self.datasource, self.schema)))
                failure = self.database_failure("42P01")
                execute.side_effect = failure
                with self.assertRaises(AppError) as caught:
                    query_service.execute_query(self.request(), "schema-profile-loss")
            self.assertEqual(caught.exception.code, 1026)
            self.assertIs(caught.exception.__cause__, failure)
            self.assertEqual(caught.exception.sqlstate, "42P01")
            self.assertIn("业务配置", caught.exception.message)
            generate.assert_called_once()
            execute.assert_called_once()

    def test_refresh_failure_preserves_original_database_error_and_stops(self):
        with ExitStack() as stack:
            generate, execute, _ = self.harness(stack, ["SELECT city_name FROM cities"])
            stack.enter_context(patch.object(query_service, "_resolve_datasource", return_value=(self.datasource, self.schema, "registered-demo")))
            refresh = stack.enter_context(patch.object(query_service, "refresh_schema_cache", side_effect=RuntimeError("cache storage unavailable")))
            fetch = stack.enter_context(patch.object(query_service, "fetch_schema_with_cache"))
            failure = self.database_failure("42703")
            execute.side_effect = failure
            with self.assertRaises(AppError) as caught:
                query_service.execute_query(self.request(), "schema-refresh-failure")
        self.assertIs(caught.exception, failure)
        self.assertEqual(caught.exception.sqlstate, "42703")
        generate.assert_called_once()
        execute.assert_called_once()
        refresh.assert_called_once()
        fetch.assert_not_called()

    def test_dangerous_candidate_after_refresh_is_still_blocked_before_execution(self):
        with ExitStack() as stack:
            generate, execute, _ = self.harness(stack, ["SELECT city_name FROM cities", "SELECT city_name FROM cities; DELETE FROM cities"])
            stack.enter_context(patch.object(query_service, "_resolve_datasource", return_value=(self.datasource, self.schema, "registered-demo")))
            refresh = stack.enter_context(patch.object(query_service, "refresh_schema_cache", return_value={"refreshed": True}))
            stack.enter_context(patch.object(query_service, "fetch_schema_with_cache", return_value=(self.datasource, self.schema)))
            execute.side_effect = self.database_failure("42703")
            with self.assertRaises(AppError) as caught:
                query_service.execute_query(self.request(), "schema-refresh-danger")
        self.assertEqual(caught.exception.code, 2004)
        self.assertEqual(generate.call_count, 2)
        execute.assert_called_once()
        refresh.assert_called_once()

    def test_explain_repairs_stacked_output_without_running_result_query(self):
        with ExitStack() as stack:
            generate, execute, _ = self.harness(stack, ["SELECT 1; SELECT 2", "SELECT city_name FROM cities"])
            stack.enter_context(patch.object(query_service, "generate_explanation", return_value="解释"))
            result = query_service.explain_query(QueryExplainRequest(question="各城市的名称", datasource=self.datasource), "explain-repair")
        self.assertEqual(generate.call_count, 2)
        execute.assert_not_called()
        self.assertEqual(result["sql_generation"]["attempt_count"], 2)


if __name__ == "__main__":
    unittest.main()
