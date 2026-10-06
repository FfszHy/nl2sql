import copy
import unittest
from unittest.mock import patch

from app.core.errors import AppError
from app.models.contracts import DataSourceConfig
from app.services import business_rewrite_service as service


def cinema_schema():
    profile = service._load_profiles()[0]
    tables = {
        table: set(columns) for table, columns in profile["required_columns"].items()
    }
    tables["ticket_orders"].update({"total_amount", "refund_amount", "ticket_count", "order_status"})
    tables["screenings"].add("sold_tickets")
    tables["movie_reviews"].add("rating")
    return [
        {"table_name": table, "columns": [{"name": column} for column in sorted(columns)]}
        for table, columns in tables.items()
    ]


class BusinessRewriteTests(unittest.TestCase):
    def setUp(self):
        self.datasource = DataSourceConfig(
            host="localhost", user="test_reader", password="test-only-placeholder", database="test_database"
        )
        self.schema = cinema_schema()

    def rewrite(self, question, schema=None, data_source_id=None):
        return service.rewrite_business_question(
            question, self.schema if schema is None else schema, self.datasource, data_source_id
        )

    def test_original_business_channel_question_uses_observed_values_and_correct_metrics(self):
        question = "App、网站、自助机和柜台，哪个渠道带来的收入最多？一起比较购票人数、平均每单实收，以及退款金额占销售额的比例。"
        with patch.object(
            service, "execute_select_sql",
            return_value=(["sales_channel"], [["app"], ["counter"], ["kiosk"], ["web"]], 4),
        ) as execute:
            result = self.rewrite(question)
        execute.assert_called_once()
        self.assertIn('SELECT DISTINCT "sales_channel" FROM "public"."ticket_orders"', execute.call_args.args[1])
        self.assertTrue(execute.call_args.args[1].endswith("LIMIT 51"))
        self.assertEqual(result["original_question"], question)
        self.assertTrue(result["rewritten_question"].startswith(question))
        self.assertEqual(
            {item["term"]: item["value"] for item in result["value_mappings"]},
            {"App": "app", "网站": "web", "自助机": "kiosk", "柜台": "counter"},
        )
        self.assertEqual(
            {item["id"] for item in result["metrics"]},
            {"net_revenue", "purchasing_customers", "average_net_order_amount", "refund_amount_percentage"},
        )
        for item in result["value_mappings"]:
            self.assertEqual(item["observed_values"], ["app", "counter", "kiosk", "web"])
        self.assertEqual(result["required_tables"], ["ticket_orders"])
        self.assertIn("禁止重复累计订单金额", result["rewritten_question"])
        self.assertIn("购票人数不得使用票数代替", result["rewritten_question"])

    def test_unobserved_canonical_value_is_rejected_instead_of_guessed(self):
        with patch.object(service, "execute_select_sql", return_value=(["sales_channel"], [["website"]], 1)):
            with self.assertRaises(AppError) as caught:
                self.rewrite("网站带来了多少收入？")
        self.assertEqual(caught.exception.code, 1026)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.error_type, "business_semantic_error")
        self.assertIn("未观察到该值", caught.exception.message)

    def test_case_sensitive_database_values_are_not_silently_lowercased(self):
        with patch.object(service, "execute_select_sql", return_value=(["sales_channel"], [["App"]], 1)):
            with self.assertRaises(AppError):
                self.rewrite("APP购票人数")

    def test_membership_aliases_are_observed_and_preserve_business_term(self):
        with patch.object(
            service, "execute_select_sql",
            return_value=(["membership_level"], [["gold"], ["normal"], ["platinum"], ["silver"]], 4),
        ) as execute:
            result = self.rewrite("普通、银卡、金卡和白金会员各自有多少购票人数？")
        execute.assert_called_once()
        self.assertIn('FROM "public"."customers"', execute.call_args.args[1])
        self.assertEqual(
            [item["value"] for item in result["value_mappings"]],
            ["normal", "silver", "gold", "platinum"],
        )
        self.assertEqual(set(result["required_tables"]), {"customers", "ticket_orders"})

    def test_longest_phrase_does_not_add_income_inside_average_or_sales_inside_ratio(self):
        with patch.object(service, "execute_select_sql") as execute:
            result = self.rewrite("比较平均每单实收和退款金额占销售额的比例。")
        execute.assert_not_called()
        self.assertEqual(
            [item["id"] for item in result["metrics"]],
            ["average_net_order_amount", "refund_amount_percentage"],
        )
        self.assertEqual(result["assumptions"], [])

    def test_gross_sales_phrase_does_not_turn_refund_before_income_into_net(self):
        result = self.rewrite("统计退款前收入和净收款。")
        self.assertEqual([item["id"] for item in result["metrics"]], ["gross_sales", "net_revenue"])
        self.assertEqual(result["assumptions"], [])

    def test_customer_count_and_ticket_count_have_different_formulas(self):
        result = self.rewrite("比较购票人数、售票张数和订单数。")
        self.assertEqual(
            {item["id"]: item["expression"] for item in result["metrics"]},
            {
                "purchasing_customers": "COUNT(DISTINCT customer_id)",
                "ticket_count": "SUM(ticket_count)",
                "order_count": "COUNT(*)",
            },
        )

    def test_plain_income_explicitly_records_default_net_assumption(self):
        result = self.rewrite("哪个城市收入最多？")
        self.assertEqual(result["metrics"][0]["expression"], "SUM(total_amount - refund_amount)")
        self.assertEqual(len(result["assumptions"]), 1)
        self.assertIn("默认将“收入”解释为扣除退款后的净收款", result["assumptions"][0])
        self.assertIn(result["assumptions"][0], result["rewritten_question"])

    def test_explicit_net_income_has_no_default_assumption(self):
        result = self.rewrite("扣掉退款后的收入是多少？")
        self.assertEqual(result["metrics"][0]["matched_terms"], ["扣掉退款后的收入"])
        self.assertEqual(result["assumptions"], [])

    def test_missing_formula_field_is_rejected_before_enum_read(self):
        schema = copy.deepcopy(self.schema)
        orders = next(table for table in schema if table["table_name"] == "ticket_orders")
        orders["columns"] = [column for column in orders["columns"] if column["name"] != "refund_amount"]
        with patch.object(service, "execute_select_sql") as execute:
            with self.assertRaises(AppError) as caught:
                self.rewrite("App的净收款", schema)
        execute.assert_not_called()
        self.assertEqual(caught.exception.code, 1026)
        self.assertIn("ticket_orders.refund_amount", caught.exception.message)

    def test_matching_profile_irrelevant_question_does_not_read_enum(self):
        question = "哪部电影评分最高？"
        with patch.object(service, "execute_select_sql") as execute:
            result = self.rewrite(question)
        execute.assert_not_called()
        self.assertEqual(result["rewritten_question"], question)
        self.assertEqual(result["metrics"], [])
        self.assertEqual(result["value_mappings"], [])

    def test_unrelated_schema_does_not_apply_cinema_rules(self):
        question = "网站的收入和利润是多少？"
        with patch.object(service, "execute_select_sql") as execute:
            result = self.rewrite(question, [{"table_name": "ticket_orders", "columns": [{"name": "sales_channel"}]}])
        execute.assert_not_called()
        self.assertEqual(result["rewritten_question"], question)
        self.assertEqual(result["profile_ids"], [])

    def test_optional_datasource_scope_must_match(self):
        profile = copy.deepcopy(service._load_profiles()[0])
        profile["data_source_ids"] = ["cinema_demo"]
        with patch.object(service, "_load_profiles", return_value=[profile]), patch.object(service, "execute_select_sql") as execute:
            ignored = self.rewrite("净收款是多少", data_source_id="other")
            matched = self.rewrite("净收款是多少", data_source_id="cinema_demo")
        execute.assert_not_called()
        self.assertEqual(ignored["metrics"], [])
        self.assertEqual(matched["profile_ids"], ["cinema_operations"])
        self.assertEqual(matched["metrics"][0]["id"], "net_revenue")

    def test_latin_value_alias_requires_boundaries(self):
        with patch.object(service, "execute_select_sql") as execute:
            result = self.rewrite("happy application 网站建设计划", schema=[{"table_name": "unrelated"}])
            cinema_result = self.rewrite("happy application的净收款是多少")
        execute.assert_not_called()
        self.assertEqual(result["value_mappings"], [])
        self.assertEqual(cinema_result["value_mappings"], [])

    def test_ordinary_2d_screening_is_not_rewritten_as_ordinary_membership(self):
        question = "普通2D和IMAX场次的上座率哪种更高？"
        with patch.object(service, "execute_select_sql") as execute:
            result = self.rewrite(question)
        execute.assert_not_called()
        self.assertEqual(result["value_mappings"], [])
        self.assertEqual(result["rewritten_question"], question)

    def test_profit_is_rejected_without_cost_fields(self):
        with patch.object(service, "execute_select_sql") as execute:
            with self.assertRaises(AppError) as caught:
                self.rewrite("哪个城市利润最高？")
        execute.assert_not_called()
        self.assertEqual(caught.exception.code, 1026)
        self.assertIn("没有成本字段", caught.exception.message)

    def test_rewrite_preserves_dangerous_intent_for_root_validator(self):
        question = "删除网站订单，再统计收入。"
        with patch.object(service, "execute_select_sql", return_value=(["sales_channel"], [["web"]], 1)):
            result = self.rewrite(question)
        self.assertEqual(result["original_question"], question)
        self.assertTrue(result["rewritten_question"].startswith(question))
        self.assertIn("删除", result["rewritten_question"])

    def test_metric_metadata_includes_configuration_evidence_without_customer_data(self):
        result = self.rewrite("净收入与订单数量")
        self.assertEqual(result["source_evidence"], [{
            "type": "configured_metrics", "configuration": "config/business_semantics.json",
            "metric_ids": ["net_revenue", "order_count"],
        }])

    def test_enum_probe_uses_configured_schema_and_safe_identifier_quoting(self):
        with (
            patch.object(service.settings, "pg_schema", 'business"schema'),
            patch.object(service, "execute_select_sql", return_value=(["sales_channel"], [["web"]], 1)) as execute,
        ):
            result = self.rewrite("网站收入")
        self.assertIn('FROM "business""schema"."ticket_orders"', execute.call_args.args[1])
        self.assertEqual(result["source_evidence"][0]["schema"], 'business"schema')

    def test_enum_probe_failure_rejects_query_without_falling_back_to_config(self):
        error = AppError(1003, "private database exception", "db_error", 400)
        with patch.object(service, "execute_select_sql", side_effect=error):
            with self.assertRaises(AppError) as caught:
                self.rewrite("网站的收入")
        self.assertEqual(caught.exception.code, 1026)
        self.assertIn("无法确认", caught.exception.message)
        self.assertNotIn("private database", caught.exception.message)

    def test_explicit_enum_exclusions_preserve_direction(self):
        cases = [
            ("排除 App 渠道，统计收入。", {"app"}),
            ("除了 App 和网站之外，统计各渠道收入。", {"app", "web"}),
            ("App 渠道不算，统计收入。", {"app"}),
        ]
        for question, expected in cases:
            with self.subTest(question=question), patch.object(
                service, "execute_select_sql", return_value=(["sales_channel"], [["app"], ["web"]], 2)
            ):
                result = self.rewrite(question)
            self.assertEqual({item["value"] for item in result["value_mappings"]}, expected)
            self.assertTrue(all(item["operator"] == "exclude" for item in result["value_mappings"]))
            self.assertIn("方向为排除", result["rewritten_question"])
            self.assertIn("sales_channel <> 'app'", result["rewritten_question"])

    def test_different_values_can_have_different_directions(self):
        with patch.object(service, "execute_select_sql", return_value=(["sales_channel"], [["app"], ["web"]], 2)):
            result = self.rewrite("排除 App 渠道，只看网站收入。")
        self.assertEqual(
            {item["value"]: item["operator"] for item in result["value_mappings"]},
            {"app": "exclude", "web": "include"},
        )

    def test_conflicting_directions_for_same_value_require_clarification(self):
        with patch.object(service, "execute_select_sql") as execute:
            with self.assertRaises(AppError) as caught:
                self.rewrite("只看 App 收入，但 App 渠道不算。")
        execute.assert_not_called()
        self.assertEqual(caught.exception.code, 1026)
        self.assertIn("同时出现包含与排除", caught.exception.message)

    def test_explicit_screening_ticket_count_does_not_use_order_ticket_count(self):
        with patch.object(service, "execute_select_sql") as execute:
            result = self.rewrite("根据场次中的已售票数和座位总数，按放映类型计算上座率。")
        execute.assert_not_called()
        self.assertEqual([item["id"] for item in result["metrics"]], ["screening_tickets"])
        self.assertEqual(result["metrics"][0]["expression"], "SUM(sold_tickets)")
        self.assertEqual(result["required_tables"], ["screenings"])
        self.assertNotIn("一条订单一个统计单位", result["rewritten_question"])

    def test_ambiguous_ticket_count_in_screening_context_is_not_guessed(self):
        with patch.object(service, "execute_select_sql") as execute:
            with self.assertRaises(AppError) as caught:
                self.rewrite("比较场次的票数和上座率。")
        execute.assert_not_called()
        self.assertEqual(caught.exception.code, 1026)
        self.assertIn("订单票数或场次已售票数", caught.exception.message)

    def test_explicit_order_and_screening_ticket_counts_can_be_requested_together(self):
        result = self.rewrite("分别比较订单票数和场次已售票数。")
        self.assertEqual(
            {item["id"]: item["expression"] for item in result["metrics"]},
            {"ticket_count": "SUM(ticket_count)", "screening_tickets": "SUM(sold_tickets)"},
        )

    def test_overlapping_profile_value_definitions_require_clarification(self):
        first = copy.deepcopy(service._load_profiles()[0])
        second = copy.deepcopy(first)
        second["id"] = "other_cinema"
        second["values"][0]["mappings"] = {"other_web": ["网站"]}
        with patch.object(service, "_load_profiles", return_value=[first, second]), patch.object(service, "execute_select_sql") as execute:
            with self.assertRaises(AppError) as caught:
                self.rewrite("网站收入")
        execute.assert_not_called()
        self.assertIn("不同定义", caught.exception.message)

    def test_overlapping_profile_metric_definitions_require_clarification(self):
        first = copy.deepcopy(service._load_profiles()[0])
        second = copy.deepcopy(first)
        second["id"] = "other_cinema"
        next(metric for metric in second["metrics"] if metric["id"] == "net_revenue")["expression"] = "SUM(total_amount)"
        with patch.object(service, "_load_profiles", return_value=[first, second]), patch.object(service, "execute_select_sql") as execute:
            with self.assertRaises(AppError) as caught:
                self.rewrite("收入")
        execute.assert_not_called()
        self.assertIn("不同定义", caught.exception.message)

    def test_metadata_identifies_database_schema_and_unsafe_order_joins(self):
        result = self.rewrite("订单数、收入和购票人数")
        self.assertEqual(result["database_schema"], service.settings.pg_schema)
        self.assertTrue(all(metric["unsafe_join_tables"] == ["order_items", "movie_reviews"] for metric in result["metrics"]))
        order_count = next(metric for metric in result["metrics"] if metric["id"] == "order_count")
        self.assertIn("COUNT(order_id)", order_count["alternatives"])

    def test_city_and_cinema_analysis_keeps_full_city_denominator_and_join_path(self):
        question = "扣掉退款后，收入最高的5个城市是哪些？每个城市收入最高的3家影院分别贡献了多少，占所在城市收入的多少？"
        with patch.object(service, "execute_select_sql") as execute:
            result = self.rewrite(question)
        execute.assert_not_called()
        self.assertEqual({item["id"] for item in result["dimensions"]}, {"city", "cinema"})
        self.assertTrue(all(item["require_output"] for item in result["dimensions"]))
        self.assertEqual(set(result["required_tables"]), {"ticket_orders", "screenings", "cinemas", "cities"})
        city_path = next(path for path in result["join_paths"] if path["to_table"] == "cities")
        self.assertEqual(city_path["tables"], ["ticket_orders", "screenings", "cinemas", "cities"])
        self.assertEqual(
            {item["id"] for item in result["analysis_constraints"]},
            {"nested_city_cinema_ranking", "cinema_share_of_city"},
        )
        self.assertIn("分母在影院TopN筛选前计算", result["rewritten_question"])
        self.assertIn("城市的全部影院净收款", result["rewritten_question"])
        self.assertIn("不要按购票顾客居住城市归属", result["rewritten_question"])

    def test_membership_genre_ranking_excludes_full_refunds_and_preserves_partial_refunds(self):
        question = "普通、银卡、金卡和白金用户，各自最喜欢哪三类电影？按实际购票人数来看，同时看看订单量和扣掉退款后的收入，全额退款的订单不算。"
        def observe(datasource, sql):
            if '"membership_level"' in sql:
                return ["membership_level"], [["normal"], ["silver"], ["gold"], ["platinum"]], 4
            if '"order_status"' in sql:
                return ["order_status"], [["paid"], ["completed"], ["refunded"], ["partial_refund"]], 4
            self.fail("Unexpected enum probe")
        with patch.object(service, "execute_select_sql", side_effect=observe) as execute:
            result = self.rewrite(question)
        self.assertEqual(execute.call_count, 2)
        refund = next(item for item in result["value_mappings"] if item["column"] == "order_status")
        self.assertEqual(refund["value"], "refunded")
        self.assertEqual(refund["operator"], "exclude")
        self.assertIn("partial_refund", refund["observed_values"])
        self.assertEqual({item["id"] for item in result["dimensions"]}, {"membership_level", "movie_genre"})
        self.assertTrue(all(item["require_output"] for item in result["dimensions"]))
        self.assertEqual({item["id"] for item in result["metrics"]}, {"purchasing_customers", "order_count", "net_revenue"})
        self.assertEqual(set(result["required_tables"]), {"ticket_orders", "customers", "screenings", "movies"})
        self.assertIn("不能改成电影片名title", result["rewritten_question"])
        self.assertIn("不能推断心理偏好", result["rewritten_question"])
        self.assertIn("每个等级内部独立按去重购票人数排名", result["rewritten_question"])
        self.assertIn("仍保留部分退款订单", result["rewritten_question"])

    def test_configured_genre_rules_are_reusable_across_different_rank_sizes(self):
        with patch.object(service, "execute_select_sql") as execute:
            result = self.rewrite("按购票人数看看各自偏好的前五类影片，并展示销售额。")
        execute.assert_not_called()
        genre = next(item for item in result["dimensions"] if item["id"] == "movie_genre")
        self.assertEqual(genre["column"], "genre")
        self.assertEqual(result["interpretations"][0]["ranking_metric_id"], "purchasing_customers")
        self.assertIn("不能用评分、订单量、票数或收入替代排名依据", result["rewritten_question"])

    def test_preference_without_measurable_purchase_metric_requires_clarification(self):
        with patch.object(service, "execute_select_sql") as execute:
            with self.assertRaises(AppError) as caught:
                self.rewrite("大家最喜欢哪类电影？")
        execute.assert_not_called()
        self.assertEqual(caught.exception.code, 1026)
        self.assertIn("需要明确可查询的衡量口径", caught.exception.message)

    def test_unrelated_like_word_does_not_trigger_movie_preference_rule(self):
        with patch.object(service, "execute_select_sql") as execute:
            result = self.rewrite("我喜欢这个应用，它有哪些表？")
        execute.assert_not_called()
        self.assertEqual(result["interpretations"], [])

    def test_full_refund_mapping_still_requires_observed_database_value(self):
        with patch.object(service, "execute_select_sql", return_value=(["order_status"], [["partial_refund"]], 1)):
            with self.assertRaises(AppError) as caught:
                self.rewrite("剔除全额退款订单，看看净收款。")
        self.assertEqual(caught.exception.code, 1026)
        self.assertIn("未观察到该值", caught.exception.message)

    def test_plain_revenue_query_does_not_silently_exclude_refunds(self):
        with patch.object(service, "execute_select_sql") as execute:
            result = self.rewrite("各城市净收款是多少？")
        execute.assert_not_called()
        self.assertFalse(any(item["column"] == "order_status" for item in result["value_mappings"]))

    def test_movie_genre_dimension_does_not_require_orders_when_only_listing_types(self):
        with patch.object(service, "execute_select_sql") as execute:
            result = self.rewrite("电影类型有哪些？")
        execute.assert_not_called()
        self.assertEqual(result["required_tables"], ["movies"])
        self.assertEqual(result["metrics"], [])
        self.assertEqual(result["join_paths"], [])

    def test_membership_filter_does_not_force_membership_into_total_revenue_output(self):
        with patch.object(service, "execute_select_sql", return_value=(["membership_level"], [["platinum"]], 1)):
            result = self.rewrite("白金用户总收入是多少？")
        dimension = next(item for item in result["dimensions"] if item["id"] == "membership_level")
        self.assertFalse(dimension["require_output"])
        self.assertIn("汇总问题无需输出该维度", result["rewritten_question"])
        self.assertEqual(result["value_mappings"][0]["value"], "platinum")

    def test_cinema_total_does_not_require_output_or_grouping_by_cinema(self):
        with patch.object(service, "execute_select_sql") as execute:
            result = self.rewrite("统计影院总收入。")
        execute.assert_not_called()
        self.assertEqual([item["id"] for item in result["dimensions"]], ["cinema"])
        self.assertFalse(result["dimensions"][0]["require_output"])

    def test_explicit_dimension_grouping_requires_output(self):
        cases = [("按城市统计收入", "city"), ("各影院净收款是多少", "cinema"), ("按会员等级统计购票人数", "membership_level")]
        for question, expected in cases:
            with self.subTest(question=question), patch.object(service, "execute_select_sql") as execute:
                result = self.rewrite(question)
            execute.assert_not_called()
            dimension = next(item for item in result["dimensions"] if item["id"] == expected)
            self.assertTrue(dimension["require_output"])

    def test_review_revenue_dual_ranking_uses_viewer_ratings_and_one_filtered_candidate_set(self):
        question = "只看至少有20条观众评价的电影，哪些电影的平均评分排进前20，但扣掉退款后的收入没有进前20？把评分、评价数量、收入和两项排名列出来。"
        with patch.object(service, "execute_select_sql") as execute:
            result = self.rewrite(question)
        execute.assert_not_called()
        self.assertEqual(
            {metric["id"]: metric["expression"] for metric in result["metrics"]},
            {"movie_review_count": "COUNT(review_id)", "movie_review_average": "AVG(rating)", "net_revenue": "SUM(total_amount - refund_amount)"},
        )
        self.assertEqual({metric["output_alias"] for metric in result["metrics"]}, {"review_count", "viewer_average_rating", "net_revenue"})
        self.assertEqual([dimension["id"] for dimension in result["dimensions"]], ["movie"])
        self.assertTrue(result["dimensions"][0]["require_output"])
        self.assertEqual(set(result["required_tables"]), {"movies", "movie_reviews", "screenings", "ticket_orders"})
        constraint = next(item for item in result["analysis_constraints"] if item["id"] == "review_revenue_rank_mismatch")
        self.assertEqual(constraint["review_count_min"], 20)
        self.assertEqual(constraint["output_aliases"], ["rating_rank", "revenue_rank"])
        self.assertEqual(constraint["resolved_rank_filters"], [
            {"output_alias": "rating_rank", "operator": "lte", "maximum": 20},
            {"output_alias": "revenue_rank", "operator": "gt", "maximum": 20},
        ])
        self.assertIn("同一完整候选集合", result["rewritten_question"])
        self.assertIn("review_count >= 20", result["rewritten_question"])
        self.assertIn("不是movies.imdb_score", result["rewritten_question"])
        self.assertIn("必须分别先按movie_id独立聚合", result["rewritten_question"])
        self.assertTrue(any("同一候选集合" in assumption for assumption in result["assumptions"]))
        self.assertTrue(any("原始平均值AVG" in assumption for assumption in result["assumptions"]))
        self.assertTrue(any("稳定排序" in assumption for assumption in result["assumptions"]))

    def test_rank_comparisons_use_different_explicit_cutoffs_and_count_population_independently(self):
        result = self.rewrite("至少80条观众评价的电影，平均分进入前10，但净收款未进入前30，把评价数量和两项排名列出来。")
        rule = next(item for item in result["analysis_constraints"] if item["id"] == "review_revenue_rank_mismatch")
        self.assertEqual(rule["review_count_min"], 80)
        self.assertEqual(rule["resolved_rank_filters"], [
            {"output_alias": "rating_rank", "operator": "lte", "maximum": 10},
            {"output_alias": "revenue_rank", "operator": "gt", "maximum": 30},
        ])
        contract = next(item for item in result["query_contract"]["analysis"] if item["kind"] == "parallel_rankings")
        self.assertEqual(contract["post_rank_filters"], rule["resolved_rank_filters"])
        self.assertEqual(contract["population_min_count"]["minimum"], 80)

    def test_rank_comparison_without_explicit_filter_does_not_invent_cutoffs(self):
        result = self.rewrite("至少80条观众评价的电影，比较平均评分排名和净收入排名，列出评价数量。")
        rule = next(item for item in result["analysis_constraints"] if item["id"] == "review_revenue_rank_mismatch")
        self.assertEqual(rule["resolved_rank_filters"], [])
        contract = next(item for item in result["query_contract"]["analysis"] if item["kind"] == "parallel_rankings")
        self.assertEqual(contract["post_rank_filters"], [])

    def test_conflicting_rank_cutoffs_require_clarification(self):
        with self.assertRaises(AppError) as caught:
            self.rewrite("至少20条观众评价的电影，平均评分进入前10，平均评分进入前30，但收入没有进前20，列出评价数量和两项排名。")
        self.assertEqual(caught.exception.code, 1026)
        self.assertIn("冲突的后置比较门槛", caught.exception.message)

    def test_review_threshold_rule_is_reusable_for_other_thresholds(self):
        result = self.rewrite("至少30条观众评价的影片，按平均评分排名和净收入排名比较，把评价数量列出来。")
        constraint = next(item for item in result["analysis_constraints"] if item["id"] == "review_revenue_rank_mismatch")
        self.assertEqual(constraint["review_count_min"], 30)
        self.assertIn("review_count >= 30", result["rewritten_question"])

    def test_movie_table_score_without_review_context_does_not_become_viewer_score(self):
        with patch.object(service, "execute_select_sql") as execute:
            result = self.rewrite("IMDb评分最高的电影有哪些？")
        execute.assert_not_called()
        self.assertEqual(result["metrics"], [])
        self.assertEqual(result["rewritten_question"], "IMDb评分最高的电影有哪些？")

    def test_genre_preference_does_not_trigger_movie_review_ranking(self):
        with patch.object(service, "execute_select_sql", return_value=(["membership_level"], [["gold"], ["silver"]], 2)):
            result = self.rewrite("金卡和银卡会员各自最喜欢哪三类电影？按购票人数来看，看看订单量和净收款。")
        self.assertNotIn("movie", {dimension["id"] for dimension in result["dimensions"]})
        self.assertNotIn("movie_review_average", {metric["id"] for metric in result["metrics"]})
        self.assertNotIn("movie_review_count", {metric["id"] for metric in result["metrics"]})
        self.assertNotIn("review_revenue_rank_mismatch", {rule["id"] for rule in result["analysis_constraints"]})

    def test_viewer_review_rating_requires_rating_field(self):
        schema = copy.deepcopy(self.schema)
        reviews = next(table for table in schema if table["table_name"] == "movie_reviews")
        reviews["columns"] = [column for column in reviews["columns"] if column["name"] != "rating"]
        with patch.object(service, "execute_select_sql") as execute:
            with self.assertRaises(AppError) as caught:
                self.rewrite("观众评价的平均评分是多少？", schema)
        execute.assert_not_called()
        self.assertIn("movie_reviews.rating", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
