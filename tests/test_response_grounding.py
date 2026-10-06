import json
import unittest
from unittest.mock import patch

from app.core.errors import AppError
from app.services import llm_service


CITY_SQL = """WITH result AS (
    SELECT ct.city_name, c.cinema_name,
           1 AS net_revenue, 1 AS city_net_revenue, 1 AS contribution_percentage
    FROM cinemas c JOIN cities ct ON ct.city_id = c.city_id
)
SELECT city_name, cinema_name, net_revenue, city_net_revenue, contribution_percentage
FROM result"""
CITY_COLUMNS = ["city_name", "cinema_name", "net_revenue", "city_net_revenue", "contribution_percentage"]
CITY_REWRITE = {
    "dimensions": [
        {"id": "city", "table": "cities", "column": "city_name"},
        {"id": "cinema", "table": "cinemas", "column": "cinema_name"},
    ],
    "metrics": [{"id": "net_revenue", "output_alias": "net_revenue"}],
    "analysis_constraints": [{"id": "nested_city_cinema_ranking"}, {"id": "cinema_share_of_city"}],
    "assumptions": ["收入按订单总金额减退款金额计算。"],
}
MOVIE_SQL = """WITH result AS (
    SELECT m.title AS movie_title, 1 AS viewer_average_rating, 1 AS review_count,
           1 AS net_revenue, 1 AS rating_rank, 21 AS revenue_rank
    FROM movies m
)
SELECT movie_title, viewer_average_rating, review_count, net_revenue, rating_rank, revenue_rank
FROM result"""
MOVIE_COLUMNS = ["movie_title", "viewer_average_rating", "review_count", "net_revenue", "rating_rank", "revenue_rank"]
MOVIE_REWRITE = {
    "dimensions": [{"id": "movie", "table": "movies", "column": "title"}],
    "metrics": [
        {"id": "movie_review_average", "output_alias": "viewer_average_rating"},
        {"id": "movie_review_count", "output_alias": "review_count"},
        {"id": "net_revenue", "output_alias": "net_revenue"},
    ],
    "analysis_constraints": [{"id": "review_revenue_rank_mismatch"}],
}


class ResponseGroundingTests(unittest.TestCase):
    def city_response(self, rows, columns=CITY_COLUMNS, sql=CITY_SQL, row_count=None):
        return llm_service.generate_response(
            "按净收款列城市和影院排名。", sql, columns, rows,
            len(rows) if row_count is None else row_count, CITY_REWRITE,
        )

    def test_wrong_model_city_order_is_never_used_and_child_amount_cannot_reorder_cities(self):
        rows = [
            ["City_020", "Cinema_001", 900, 300000, 0.3],
            ["City_020", "Cinema_002", 800, 300000, 0.2667],
            ["City_008", "Cinema_003", 100000, 200000, 50],
        ]
        with patch.object(llm_service, "_call_generation", return_value="City_008、City_020") as call:
            response = self.city_response(rows)
        call.assert_not_called()
        self.assertIn("城市依次为：City_020、City_008", response)
        self.assertLess(response.index("Cinema_001"), response.index("Cinema_002"))
        self.assertIn("城市净收款 300000", response)
        self.assertIn("净收款 100000", response)
        self.assertIn("占城市收入 50%", response)
        self.assertIn("收入按订单总金额减退款金额计算", response)

    def test_correct_result_order_is_kept_without_extra_response_generation(self):
        with patch.object(llm_service, "_call_generation", return_value="正确答复") as call:
            response = self.city_response([
                ["City_019", "Cinema_B", 9, 20, 45],
                ["City_018", "Cinema_A", 8, 19, 42],
            ])
        call.assert_not_called()
        self.assertIn("城市依次为：City_019、City_018", response)

    def test_answer_provider_failure_cannot_discard_ranked_query_results(self):
        failure = AppError(1006, "provider unavailable", "llm_error", 502)
        with patch.object(llm_service, "_call_generation", side_effect=failure) as call:
            response = self.city_response([["City_020", "Cinema_A", 12, 24, 50]])
        call.assert_not_called()
        self.assertIn("City_020", response)
        self.assertIn("净收款 12", response)

    def test_unknown_extra_columns_are_not_guessed_as_amounts_or_percentages(self):
        sql = CITY_SQL.replace("city_net_revenue", "unknown_total").replace("contribution_percentage", "unknown_share")
        columns = ["city_name", "cinema_name", "net_revenue", "unknown_total", "unknown_share"]
        response = self.city_response([["City_020", "Cinema_A", 12, 999999, 765432]], columns, sql)
        self.assertIn("净收款 12", response)
        self.assertNotIn("999999", response)
        self.assertNotIn("765432", response)
        self.assertIn("贡献占比", response)
        self.assertIn("结果表", response)

    def test_recognizes_verified_percentage_output_aliases(self):
        for alias in ("share_of_city", "net_revenue_share_percent"):
            with self.subTest(alias=alias):
                sql = CITY_SQL.replace("contribution_percentage", alias)
                columns = ["city_name", "cinema_name", "net_revenue", "city_net_revenue", alias]
                response = self.city_response([["City_020", "Cinema_A", 12, 24, 50]], columns, sql)
                self.assertIn("占城市收入 50%", response)

    def test_member_group_type_order_comes_from_rows_and_case_labels_are_preserved(self):
        sql = """WITH result AS (
            SELECT CASE c.membership_level WHEN 'normal' THEN '普通' ELSE '银卡' END AS 会员等级,
                   m.genre AS 类型, 1 AS purchasing_customers, 1 AS order_count, 1 AS net_revenue
            FROM customers c CROSS JOIN movies m
        ) SELECT 会员等级, 类型, purchasing_customers, order_count, net_revenue FROM result"""
        rewrite = {
            "dimensions": [
                {"id": "membership_level", "table": "customers", "column": "membership_level"},
                {"id": "movie_genre", "table": "movies", "column": "genre"},
            ],
            "metrics": [
                {"id": "purchasing_customers", "output_alias": "purchasing_customers"},
                {"id": "order_count", "output_alias": "order_count"},
                {"id": "net_revenue", "output_alias": "net_revenue"},
            ],
            "analysis_constraints": [{"id": "member_genre_ranking"}],
        }
        with patch.object(llm_service, "_call_generation", return_value="银卡第一，comedy优于crime") as call:
            response = llm_service.generate_response(
                "各等级最喜欢的类型", sql,
                ["会员等级", "类型", "purchasing_customers", "order_count", "net_revenue"],
                [["普通", "crime", 100, 150, 500], ["普通", "comedy", 95, 200, 999], ["银卡", "drama", 120, 180, 800]],
                3, rewrite,
            )
        call.assert_not_called()
        self.assertLess(response.index("crime"), response.index("comedy"))
        self.assertLess(response.index("普通："), response.index("银卡："))
        self.assertIn("购票人数 100，订单数 150，净收款 500", response)
        self.assertIn("不表示会员等级之间的总体排名", response)

    def test_unresolved_dimension_falls_back_without_model_or_invented_entities(self):
        with patch.object(llm_service, "_call_generation", return_value="City_fake") as call:
            response = self.city_response([[12]], ["net_revenue"], "SELECT 12 AS net_revenue")
        call.assert_not_called()
        self.assertNotIn("City_fake", response)
        self.assertIn("查询返回 1 行", response)
        self.assertIn("表格明细", response)

    def test_ranked_preview_is_bounded_and_explicit_about_truncation(self):
        rows = [[f"City_{i:03d}", f"Cinema_{i:03d}", 1, 10, 10] for i in range(21)]
        with patch.object(llm_service, "_call_generation") as call:
            response = self.city_response(rows)
        call.assert_not_called()
        self.assertIn("仅展示前20行", response)
        self.assertIn("City_019", response)
        self.assertNotIn("City_020", response)

    def test_unconfigured_answer_still_calls_provider_once_with_row_order_constraint(self):
        with patch.object(llm_service, "_call_generation", return_value="原始顺序答复") as call:
            response = llm_service.generate_response("列出城市", "SELECT city_name FROM cities", ["city_name"], [["City_020"], ["City_008"]], 2)
        self.assertEqual(response, "原始顺序答复")
        self.assertEqual(call.call_count, 1)
        messages = call.call_args.kwargs["messages"]
        self.assertIn("不重新排序", messages[0]["content"])
        payload = json.loads(messages[1]["content"].split("\n", 1)[1])
        self.assertEqual([row["city_name"] for row in payload["rows_preview"]], ["City_020", "City_008"])
        self.assertIn("原始", payload["row_order_policy"])

    def test_movie_review_mismatch_preserves_rows_and_reports_both_actual_ranks(self):
        with patch.object(llm_service, "_call_generation", return_value="Movie_0053 排第一") as call:
            response = llm_service.generate_response(
                "评分进前20但收入没进前20的电影", MOVIE_SQL, MOVIE_COLUMNS,
                [["Movie_0051", "6.2531645569620253", 79, "18794.76", 1, 229],
                 ["Movie_0053", "6.2", 80, "27000.00", 2, 150]],
                2, MOVIE_REWRITE,
            )
        call.assert_not_called()
        self.assertLess(response.index("Movie_0051"), response.index("Movie_0053"))
        self.assertIn("平均观众评分 6.2531645569620253，评价条数 79，净收款 18794.76，评分排名 1，收入排名 229", response)
        self.assertNotIn("IMDb", response)

    def test_movie_review_mismatch_provider_failure_is_isolated(self):
        with patch.object(llm_service, "_call_generation", side_effect=AppError(1006, "offline provider", "llm_error", 502)) as call:
            response = llm_service.generate_response(
                "两项排名", MOVIE_SQL, MOVIE_COLUMNS, [["Movie_0051", 6.2, 79, 12, 1, 229]], 1, MOVIE_REWRITE,
            )
        call.assert_not_called()
        self.assertIn("Movie_0051", response)
        self.assertIn("收入排名 229", response)

    def test_movie_review_unknown_metric_or_rank_alias_falls_back_to_actual_table(self):
        for original, unknown in [("viewer_average_rating", "unconfigured_score"), ("revenue_rank", "unconfigured_rank")]:
            with self.subTest(column=original), patch.object(llm_service, "_call_generation", return_value="invented") as call:
                columns = [unknown if name == original else name for name in MOVIE_COLUMNS]
                response = llm_service.generate_response(
                    "两项排名", MOVIE_SQL.replace(original, unknown), columns,
                    [["Movie_0051", 6.2, 79, 12, 1, 229]], 1, MOVIE_REWRITE,
                )
            call.assert_not_called()
            self.assertIn("表格明细", response)
            self.assertNotIn("invented", response)
            self.assertNotIn("平均观众评分 6.2", response)

    def test_movie_review_preview_is_bounded_and_empty_results_are_explicit(self):
        rows = [[f"Movie_{i:04d}", 6, 20, 12, i + 1, i + 21] for i in range(21)]
        with patch.object(llm_service, "_call_generation") as call:
            response = llm_service.generate_response("两项排名", MOVIE_SQL, MOVIE_COLUMNS, rows, 21, MOVIE_REWRITE)
            empty = llm_service.generate_response("两项排名", MOVIE_SQL, MOVIE_COLUMNS, [], 0, MOVIE_REWRITE)
        call.assert_not_called()
        self.assertIn("仅展示前20行", response)
        self.assertIn("Movie_0019", response)
        self.assertNotIn("Movie_0020", response)
        self.assertEqual(empty, "未查询到符合条件的数据。")

    def test_movie_review_null_value_is_not_fabricated_as_zero(self):
        response = llm_service.generate_response(
            "两项排名", MOVIE_SQL, MOVIE_COLUMNS, [["Movie_0051", None, 79, 12, 1, 229]], 1, MOVIE_REWRITE,
        )
        self.assertIn("平均观众评分 空值", response)
        self.assertNotIn("平均观众评分 0", response)


if __name__ == "__main__":
    unittest.main()
