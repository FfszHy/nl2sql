import copy
import unittest
from datetime import date, datetime
from decimal import Decimal

from app.services.chart_planning_service import plan_chart


def metric(name, unit=None, additive=None, terms=None):
    result = {"id": name, "output_alias": name, "display_name": name, "matched_terms": terms or []}
    if unit is not None:
        result["unit"] = unit
    if additive is not None:
        result["additive"] = additive
    return result


class ChartPlanningTests(unittest.TestCase):
    def plan(self, question, sql, columns, rows, rewrite=None, **options):
        original = copy.deepcopy(rows)
        result = plan_chart(question, sql, columns, rows, rewrite, **options)
        self.assertEqual(rows, original)
        self.assertIn("code", result["reason"])
        self.assertIn("message", result["reason"])
        return result

    def test_ranking_uses_primary_amount_only_with_long_labels(self):
        rewrite = {"metrics": [metric("received", "amount", True), metric("buyers", "people", False)]}
        result = self.plan("收入最高的影院", "SELECT name,SUM(paid) AS received,COUNT(DISTINCT buyer_id) AS buyers FROM sales GROUP BY name ORDER BY received DESC",
                           ["name", "received", "buyers"], [["很长的城市影院名称甲", Decimal("120.50"), 3], ["影院乙", "80.00", 2]], rewrite)
        self.assertEqual(result["config"]["type"], "bar")
        self.assertEqual(result["config"]["orientation"], "horizontal")
        self.assertEqual([item["field"] for item in result["config"]["series"]], ["received"])

    def test_member_groups_keep_combined_labels_and_requested_ranking_metric(self):
        rewrite = {"metrics": [metric("received", "amount", True), metric("buyers", "people", False), metric("orders", "orders", True)],
                   "analysis_constraints": [{"ranking_metric_id": "buyers"}]}
        result = self.plan("各会员最喜欢的类型，按购票人数看", "SELECT level,genre,SUM(paid) AS received,COUNT(DISTINCT buyer_id) AS buyers,COUNT(*) AS orders FROM sales GROUP BY level,genre",
                           ["level", "genre", "received", "buyers", "orders"], [["gold", "drama", 100, 2, 3], ["gold", "crime", 80, 1, 2], ["silver", "drama", 50, 1, 1]], rewrite)
        self.assertEqual(result["config"]["category"], ["level", "genre"])
        self.assertEqual(result["config"]["series"][0]["field"], "buyers")

    def test_monthly_ordered_dates_choose_line_and_report_grain(self):
        rows = [["2026-01-01T00:00:00", Decimal("100.30")], [date(2026, 2, 1), "110.2"], [datetime(2026, 3, 1), None]]
        result = self.plan("按月看收入趋势", "SELECT DATE_TRUNC('month',order_time) AS month,SUM(amount) AS revenue FROM orders GROUP BY 1 ORDER BY month",
                           ["month", "revenue"], rows, {"metrics": [metric("revenue", "amount", True)]})
        self.assertEqual(result["config"]["type"], "line")
        self.assertEqual(result["config"]["category"], ["month"])
        self.assertEqual(result["profile"]["columns"][0]["time_grain"], "month")
        self.assertEqual(result["profile"]["columns"][1]["null_count"], 1)

    def test_unordered_duplicate_and_missing_time_do_not_connect_a_false_trend(self):
        for values in (["2026-02", "2026-01", "2026-03"], ["2026-01", "2026-01", "2026-02"], ["2026-01", None, "2026-03"]):
            with self.subTest(values=values):
                result = self.plan("收入趋势", "SELECT month,SUM(amount) AS revenue FROM orders GROUP BY month", ["month", "revenue"], [[value, 1] for value in values])
                self.assertIsNone(result["config"])
                self.assertEqual(result["reason"]["code"], "unordered_or_grouped_time")

    def test_year_extract_can_be_timeline_but_month_number_or_arbitrary_extract_cannot(self):
        for part, values, expected in (("year", [2024, 2025, 2026], "line"), ("month", [1, 2, 3], None), ("epoch", [1, 2, 3], None)):
            with self.subTest(part=part):
                result = self.plan("按时间看收入趋势", f"SELECT EXTRACT({part} FROM order_time) AS period,SUM(amount) AS revenue FROM orders GROUP BY period ORDER BY period",
                                   ["period", "revenue"], [[value, 10] for value in values])
                self.assertEqual(result["config"]["type"] if result["config"] else None, expected)

    def test_complete_channel_composition_uses_additive_amount_pie(self):
        result = self.plan("各渠道收入占整体收入多少？展示渠道构成。", "SELECT channel,SUM(paid-refunded) AS received FROM sales GROUP BY channel ORDER BY received DESC LIMIT 200",
                           ["channel", "received"], [["app", "100.10"], ["web", Decimal("200.50")], ["counter", 0]],
                           {"metrics": [metric("received", "amount", True)]}, row_count=3)
        self.assertEqual(result["config"]["type"], "pie")
        self.assertEqual(result["reason"]["code"], "complete_additive_composition")

    def test_top_n_truncated_offset_dynamic_limit_and_postgroup_filters_refuse_pie(self):
        base = "SELECT channel,SUM(paid) AS received FROM sales GROUP BY channel"
        cases = [
            ("渠道整体构成前2名", base, {}),
            ("渠道整体构成", base, {"rows_truncated": True}),
            ("渠道整体构成", base, {"row_count": 3}),
            ("渠道整体构成", base + " LIMIT 2", {}),
            ("渠道整体构成", base + " LIMIT 10 OFFSET 1", {}),
            ("渠道整体构成", base + " LIMIT 1+9", {}),
            ("渠道整体构成", base + " HAVING SUM(paid)>0", {}),
            ("渠道整体构成", base.replace("FROM sales", "FROM sales WHERE channel IN('app','web')"), {}),
            ("渠道整体构成", "WITH amounts AS (" + base + ") SELECT channel,received FROM amounts WHERE received>0", {}),
            ("渠道整体构成", base + ",region", {}),
        ]
        for question, sql, options in cases:
            with self.subTest(sql=sql, options=options):
                result = self.plan(question, sql, ["channel", "received"], [["app", 5], ["web", 10]], {"metrics": [metric("received", "amount", True)]}, **options)
                self.assertIsNone(result["config"])
                self.assertEqual(result["reason"]["code"], "incomplete_composition")

    def test_pie_refuses_nonadditive_distinct_people_average_and_percent(self):
        for expression, unit in (("COUNT(DISTINCT buyer_id)", "people"), ("AVG(score)", "rating"), ("100.0*SUM(refund)/SUM(paid)", "percent")):
            with self.subTest(expression=expression):
                result = self.plan("全部渠道的整体构成", f"SELECT channel,{expression} AS value FROM sales GROUP BY channel",
                                   ["channel", "value"], [["app", 2], ["web", 3]], {"metrics": [metric("value", unit, False)]})
                self.assertIsNone(result["config"])
                self.assertEqual(result["reason"]["code"], "non_additive_composition")

    def test_simple_non_distinct_count_can_prove_additivity_without_unit_metadata(self):
        result = self.plan("全部电影类型构成", "SELECT genre,COUNT(*) AS n FROM movies GROUP BY genre", ["genre", "n"], [["drama", 2], ["crime", 3]])
        self.assertEqual(result["config"]["type"], "pie")

    def test_negative_null_zero_total_and_too_many_slices_refuse_pie(self):
        datasets = [[["app", -1], ["web", 2]], [["app", None], ["web", 2]], [["app", 0], ["web", 0]], [[str(index), 1] for index in range(7)]]
        for rows in datasets:
            with self.subTest(rows=rows):
                result = self.plan("整体渠道构成", "SELECT channel,SUM(paid) AS received FROM sales GROUP BY channel", ["channel", "received"], rows,
                                   {"metrics": [metric("received", "amount", True)]})
                self.assertIsNone(result["config"])

    def test_movie_relationship_selects_named_rating_and_revenue_among_extra_numeric_columns(self):
        columns = ["movie_id", "title", "viewer_average_rating", "review_count", "net_revenue", "rating_rank", "revenue_rank"]
        rows = [[1, "A", "8.5", 25, Decimal("120.3"), 1, 23], [2, "B", "8.4", 30, "110.50", 2, 24]]
        rewrite = {"metrics": [metric("net_revenue", "amount", True), metric("review_count", "reviews", True), metric("viewer_average_rating", "rating", False)]}
        rewrite["metrics"][0]["display_name"] = "净收款"
        rewrite["metrics"][2]["display_name"] = "平均观众评分"
        result = self.plan("评分前20收入未进前20，把评分、评价数量、收入和排名列出。用散点图观察这些电影评分与收入的关系", "SELECT movie_id,title,viewer_average_rating,review_count,net_revenue,rating_rank,revenue_rank FROM ranked",
                           columns, rows, rewrite)
        self.assertEqual(result["config"]["type"], "scatter")
        self.assertEqual(result["config"]["category"], ["viewer_average_rating"])
        self.assertEqual(result["config"]["series"][0]["field"], "net_revenue")
        self.assertEqual(result["config"]["title"], "平均观众评分与净收款的关系")

    def test_ambiguous_relationship_with_three_numeric_metrics_keeps_table(self):
        result = self.plan("看看这些指标之间的关系", "SELECT label,x,y,z FROM readings", ["label", "x", "y", "z"], [["A", 1, 2, 3], ["B", 2, 3, 4]])
        self.assertIsNone(result["config"])
        self.assertEqual(result["reason"]["code"], "ambiguous_relationship")

    def test_relationship_excludes_ids_ranks_and_boolean_and_requires_paired_variation(self):
        for columns, rows in ((["row_id", "ranking", "enabled"], [[1, 1, True], [2, 2, False]]),
                              (["x", "y"], [[1, None], [None, 2]]), (["x", "y"], [["1", 2], ["1.0", 3]])):
            with self.subTest(columns=columns, rows=rows):
                result = self.plan("数值关系", "SELECT " + ",".join(columns) + " FROM readings", columns, rows)
                self.assertIsNone(result["config"])

    def test_bool_category_does_not_become_numeric_metric(self):
        result = self.plan("是否启用的数量", "SELECT enabled,COUNT(*) AS n FROM readings GROUP BY enabled", ["enabled", "n"], [[True, 3], [False, 2]])
        self.assertEqual(result["config"]["type"], "bar")
        self.assertEqual(result["config"]["category"], ["enabled"])

    def test_empty_one_row_all_null_invalid_numeric_and_duplicate_columns_keep_table(self):
        cases = [(["name", "value"], []), (["name", "value"], [["A", 1]]), (["name", "value"], [["A", None], ["B", None]]),
                 (["name", "value"], [["A", Decimal("NaN")], ["B", Decimal("Infinity")]]), (["value", "value"], [[1, 2], [3, 4]])]
        for columns, rows in cases:
            with self.subTest(columns=columns, rows=rows):
                result = self.plan("分类比较", "SELECT name,value FROM readings", columns, rows)
                self.assertIsNone(result["config"])

    def test_actual_aliases_from_projection_positions_drive_profile(self):
        result = self.plan("全部电影类型构成", 'SELECT genre AS "电影类别",COUNT(*) AS "电影数量" FROM movies GROUP BY genre',
                           ["电影类别", "电影数量"], [["drama", "2"], ["crime", "3"]])
        self.assertEqual(result["config"]["type"], "pie")
        self.assertEqual(result["config"]["series"][0]["field"], "电影数量")

    def test_joined_tags_are_not_proven_mutually_exclusive_by_unique_group_labels(self):
        result = self.plan("全部销售标签的收入构成", "SELECT t.tag,SUM(s.paid) AS received FROM sales s JOIN tags t ON t.sale_id=s.sale_id GROUP BY t.tag",
                           ["tag", "received"], [["popular", 10], ["new", 20]], {"metrics": [metric("received", "amount", True)]})
        self.assertIsNone(result["config"])
        self.assertEqual(result["reason"]["code"], "incomplete_composition")
        self.assertIn("单事实表", result["reason"]["message"])

    def test_unknown_ratio_unit_is_not_guessed_or_rescaled_and_metadata_wins(self):
        rows = [["A", "0.1"], ["B", "0.2"]]
        for field in ("share", "ratio", "percentage", "比例"):
            with self.subTest(field=field):
                result = self.plan("分类比较", f"SELECT label,{field} FROM readings", ["label", field], rows)
                self.assertIsNone(result["profile"]["columns"][1]["unit"])
                self.assertEqual(result["profile"]["columns"][1]["min"], "0.1")
        result = self.plan("分类比较", "SELECT label,share FROM readings", ["label", "share"], rows,
                           {"metrics": [metric("share", "percent", False)]})
        self.assertEqual(result["profile"]["columns"][1]["unit"], "percent")
        self.assertEqual(result["profile"]["columns"][1]["min"], "0.1")

    def test_malformed_column_or_row_containers_do_not_bind_characters_or_dict_keys(self):
        for columns, rows in (([1, "value"], [["A", 1], ["B", 2]]),
                              (["label", "value"], ["AB", "CD"]),
                              (["label", "value"], [{"label": "A", "value": 1}, {"label": "B", "value": 2}]),
                              ("AB", [[1, 2], [3, 4]]), (["label", "value"], None)):
            with self.subTest(columns=columns, rows=rows):
                result = plan_chart("分类比较", "SELECT label,value FROM readings", columns, rows)
                self.assertIsNone(result["config"])
                self.assertEqual(result["reason"]["code"], "ambiguous_columns")

    def test_real_primary_and_entity_keys_remain_ids_through_cte_aliases(self):
        for evidence in ({"schema_keys": {"accounts": {"primary_key": ["code"]}}},
                         {"entities": [{"entity_keys": ["accounts.code"]}]}):
            for sql in ("SELECT code,revenue FROM accounts",
                        "WITH raw AS (SELECT code AS observation,revenue FROM accounts) SELECT observation AS code,revenue FROM raw"):
                with self.subTest(evidence=evidence, sql=sql):
                    result = self.plan("code与revenue的关系", sql, ["code", "revenue"], [[1001, 5], [1002, 8]], {"query_contract": evidence})
                    self.assertIsNone(result["config"])
                    self.assertEqual(result["profile"]["columns"][0]["role"], "id")
                    self.assertEqual(result["profile"]["columns"][0]["source_columns"], ["accounts.code"])

    def test_cartesian_disconnected_and_unknown_joined_metrics_are_not_observations(self):
        rewrite = {"metrics": [metric("height"), metric("rating", "rating", False)]}
        rewrite["metrics"][0]["display_name"] = "身高"
        rows = [["A", "M", 160, 8], ["A", "N", 160, 9], ["B", "M", 180, 8], ["B", "N", 180, 9]]
        for source in ("accounts a CROSS JOIN movies m", "accounts a,movies m", "accounts a JOIN movies m ON TRUE",
                       "accounts a JOIN movies m ON a.code=m.movie_id"):
            with self.subTest(source=source):
                result = self.plan("身高与评分的关系", "SELECT a.name,m.title,a.height,m.rating FROM " + source,
                                   ["name", "title", "height", "rating"], rows, rewrite)
                self.assertIsNone(result["config"])
                self.assertEqual(result["reason"]["code"], "unconfirmed_observation_grain")

    def test_prevalidated_parallel_movie_population_allows_two_fact_metrics(self):
        rewrite = {"metrics": [metric("rating", "rating", False), metric("received", "amount", True)], "query_contract": {"analysis": [{
            "shared_population": True, "population_entity_keys": ["movies.movie_id"],
            "rankings": [{"metric_id": "rating"}, {"metric_id": "received"}],
        }]}}
        result = self.plan("rating与received的关系", "WITH r AS (SELECT movie_id,AVG(score) AS rating FROM reviews GROUP BY movie_id), "
                           "v AS (SELECT movie_id,SUM(amount) AS received FROM sales GROUP BY movie_id) "
                           "SELECT m.title,r.rating,v.received FROM movies m JOIN r ON r.movie_id=m.movie_id JOIN v ON v.movie_id=m.movie_id",
                           ["title", "rating", "received"], [["A", 8.5, 100], ["B", 8.3, 80]], rewrite)
        self.assertEqual(result["config"]["type"], "scatter")

    def test_explicit_preference_metric_precedes_earlier_mentioned_other_units(self):
        interpretation = {"ranking_metric_id": "buyers"}
        for interpretations in ([interpretation], {"preference": interpretation}):
            rewrite = {"metrics": [metric("orders", "orders", True, ["订单数"]), metric("buyers", "people", False, ["购票人数"])],
                       "interpretations": interpretations}
            result = self.plan("先列订单数，再按购票人数看偏好", "SELECT genre,COUNT(*) AS orders,COUNT(DISTINCT buyer_id) AS buyers FROM sales GROUP BY genre",
                               ["genre", "orders", "buyers"], [["A", 4, 2], ["B", 3, 1]], rewrite)
            self.assertEqual(result["config"]["series"][0]["field"], "buyers")


if __name__ == "__main__":
    unittest.main()
