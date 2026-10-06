import unittest

from app.core.errors import AppError
from app.services.business_sql_service import business_result_dimensions, validate_business_sql


def mapping(value="app"):
    return {
        "table": "ticket_orders", "column": "sales_channel", "term": "App",
        "value": value, "observed_values": ["app", "web", "kiosk", "counter"],
    }


def metric(expression="COUNT(DISTINCT customer_id)", alias="purchasing_customers"):
    return {
        "id": alias, "table": "ticket_orders", "expression": expression,
        "description": alias, "output_alias": alias, "alternatives": [],
    }


def relation(left_table, column, right_table):
    return {"left_table": left_table, "left_column": column,
            "right_table": right_table, "right_column": column, "cardinality": "many_to_one"}


def dimension(table, column, require_output=True):
    return {"id": column, "table": table, "column": column, "require_output": require_output}


def movie_rewrite():
    return {
        "metrics": [metric("SUM(total_amount - refund_amount)", "net_revenue")],
        "dimensions": [dimension("movies", "genre")],
        "join_paths": [{"from_table": "ticket_orders", "to_table": "movies",
                        "tables": ["ticket_orders", "screenings", "movies"],
                        "joins": [relation("ticket_orders", "screening_id", "screenings"),
                                  relation("screenings", "movie_id", "movies")]}],
    }


def review_rank_rewrite(minimum=20):
    net = {**metric("SUM(total_amount-refund_amount)", "net_revenue"), "unsafe_join_tables": ["movie_reviews"]}
    average = {**metric("AVG(rating)", "viewer_average_rating"), "id": "movie_review_average", "table": "movie_reviews", "unsafe_join_tables": ["ticket_orders"]}
    count = {**metric("COUNT(*)", "review_count"), "id": "movie_review_count", "table": "movie_reviews", "unsafe_join_tables": ["ticket_orders"]}
    return {"metrics": [net, average, count], "query_contract": {"version": 1, "analysis": [{
        "kind": "parallel_rankings", "policy": "ROW_NUMBER", "shared_population": True,
        "rankings": [{"output_alias": "rating_rank", "metric_id": "movie_review_average", "direction": "desc", "partition_by": []},
                     {"output_alias": "revenue_rank", "metric_id": "net_revenue", "direction": "desc", "partition_by": []}],
        "tie_keys": ["movies.movie_id"],
        "population_min_count": {"metric_id": "movie_review_count", "output_alias": "review_count", "operator": "gte", "minimum": minimum},
        "post_rank_filters": [{"output_alias": "rating_rank", "operator": "lte", "maximum": 20},
                              {"output_alias": "revenue_rank", "operator": "gt", "maximum": 20}],
    }]}}


def review_rank_sql(minimum=20):
    return (
        "WITH reviews AS (SELECT movie_id, AVG(rating) AS viewer_average_rating, COUNT(*) AS review_count "
        f"FROM movie_reviews GROUP BY movie_id HAVING COUNT(*) >= {minimum}), "
        "revenue AS (SELECT s.movie_id, SUM(o.total_amount-o.refund_amount) AS net_revenue "
        "FROM ticket_orders o JOIN screenings s ON s.screening_id=o.screening_id GROUP BY s.movie_id), "
        "eligible AS (SELECT m.movie_id,m.title,rv.viewer_average_rating,rv.review_count,COALESCE(v.net_revenue,0) AS net_revenue "
        "FROM reviews rv JOIN movies m ON m.movie_id=rv.movie_id LEFT JOIN revenue v ON v.movie_id=rv.movie_id), "
        "ranked AS (SELECT movie_id,title,viewer_average_rating,review_count,net_revenue, "
        "ROW_NUMBER() OVER(ORDER BY viewer_average_rating DESC,movie_id) AS rating_rank, "
        "ROW_NUMBER() OVER(ORDER BY net_revenue DESC,movie_id) AS revenue_rank FROM eligible) "
        "SELECT movie_id,title,viewer_average_rating,review_count,net_revenue,rating_rank,revenue_rank "
        "FROM ranked WHERE rating_rank<=20 AND revenue_rank>20"
    )


def feedback_average_rewrite(nullable="NO"):
    average = {**metric("AVG(score)", "mean_score"), "table": "feedback"}
    return {"metrics": [average], "query_contract": {
        "metrics": [{"id": "mean_score", "source": {"nullability": {"feedback.score": nullable}}}],
        "schema_keys": {"feedback": {"primary_key": ["feedback_id"], "unique_keys": [], "metadata_verified": True},
                        "accounts": {"primary_key": ["account_id"], "unique_keys": [], "metadata_verified": True}},
    }}


def sales_count_rewrite():
    count = {**metric("COUNT(*)", "sale_count"), "table": "sales", "alternatives": ["COUNT(sale_id)"]}
    return {"metrics": [count], "query_contract": {"schema_keys": {
        "sales": {"primary_key": ["sale_id"], "unique_keys": [], "metadata_verified": True},
        "accounts": {"primary_key": ["account_id"], "unique_keys": [], "metadata_verified": True},
        "sessions": {"primary_key": ["session_id"], "unique_keys": [], "metadata_verified": True},
        "items": {"primary_key": ["item_id"], "unique_keys": [], "metadata_verified": True},
        "feedback": {"primary_key": ["feedback_id"], "unique_keys": [], "metadata_verified": True},
        "item_languages": {"primary_key": [], "unique_keys": [["item_id", "language"]], "metadata_verified": True},
    }}}


class BusinessSQLTests(unittest.TestCase):
    def assert_business_error(self, sql, rewrite):
        with self.assertRaises(AppError) as caught:
            validate_business_sql(sql, rewrite)
        self.assertEqual(caught.exception.code, 1027)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.error_type, "business_semantic_error")
        return caught.exception

    def test_rejects_wrong_enum_case_alias_and_unrequested_channel(self):
        for value in ["App", "APP", "网站", "web"]:
            with self.subTest(value=value):
                self.assert_business_error(
                    f"SELECT COUNT(*) FROM ticket_orders o WHERE o.sales_channel = '{value}'",
                    {"value_mappings": [mapping()]},
                )

    def test_accepts_actual_value_and_chinese_display_label(self):
        validate_business_sql(
            "SELECT CASE WHEN o.sales_channel = 'app' THEN 'App渠道' ELSE '其他' END AS 渠道, "
            "COUNT(*) FROM ticket_orders o WHERE o.sales_channel = 'app' GROUP BY 1",
            {"value_mappings": [mapping()]},
        )

    def test_rejects_omitted_or_extra_mapped_values(self):
        for sql in [
            "SELECT COUNT(*) FROM ticket_orders",
            "SELECT COUNT(*) FROM ticket_orders WHERE sales_channel IN ('app', 'web')",
            "SELECT COUNT(*) FROM ticket_orders WHERE sales_channel <> 'app'",
        ]:
            self.assert_business_error(sql, {"value_mappings": [mapping()]})
        self.assert_business_error(
            "SELECT COUNT(*) FROM ticket_orders WHERE sales_channel = 'app'",
            {"value_mappings": [mapping(), mapping("web")]},
        )

    def test_accepts_complete_multi_value_filter(self):
        validate_business_sql(
            "SELECT COUNT(*) FROM ticket_orders WHERE sales_channel IN ('app', 'web')",
            {"value_mappings": [mapping(), mapping("web")]},
        )

    def test_preserves_include_and_exclude_directions(self):
        excluded = {**mapping(), "operator": "exclude"}
        for clause in ["sales_channel <> 'app'", "sales_channel NOT IN ('app')", "NOT (sales_channel = 'app')"]:
            validate_business_sql(
                f"SELECT COUNT(*) FROM ticket_orders WHERE {clause}", {"value_mappings": [excluded]},
            )
        for clause in ["sales_channel = 'app'", "sales_channel <> 'App'", "sales_channel NOT IN ('app', 'web')"]:
            self.assert_business_error(
                f"SELECT COUNT(*) FROM ticket_orders WHERE {clause}", {"value_mappings": [excluded]},
            )
        validate_business_sql(
            "SELECT COUNT(*) FROM ticket_orders WHERE sales_channel <> 'app' AND sales_channel = 'web'",
            {"value_mappings": [excluded, mapping("web")]},
        )

    def test_rejects_or_that_bypasses_the_requested_filter(self):
        self.assert_business_error(
            "SELECT COUNT(*) FROM ticket_orders WHERE sales_channel = 'app' OR ticket_count > 1",
            {"value_mappings": [mapping()]},
        )
        validate_business_sql(
            "SELECT COUNT(*) FROM ticket_orders WHERE "
            "(sales_channel = 'app' AND ticket_count > 1) OR (sales_channel = 'web' AND ticket_count = 1)",
            {"value_mappings": [mapping(), mapping("web")]},
        )
        validate_business_sql(
            "SELECT COUNT(*) FROM ticket_orders WHERE "
            "(sales_channel = 'app' OR ticket_count > 1) AND sales_channel = 'app'",
            {"value_mappings": [mapping()]},
        )
        self.assert_business_error(
            "SELECT COUNT(*) FROM ticket_orders WHERE NOT (sales_channel = 'app' AND ticket_count > 1)",
            {"value_mappings": [{**mapping(), "operator": "exclude"}]},
        )

    def test_rejects_same_table_name_from_an_unverified_schema(self):
        self.assert_business_error(
            "SELECT COUNT(*) FROM other_schema.ticket_orders WHERE sales_channel = 'app'",
            {"database_schema": "public", "value_mappings": [mapping()]},
        )
        validate_business_sql(
            "SELECT COUNT(*) FROM public.ticket_orders WHERE sales_channel = 'app'",
            {"database_schema": "public", "value_mappings": [mapping()]},
        )
        self.assert_business_error(
            "SELECT COUNT(DISTINCT customer_id) AS purchasing_customers FROM other_schema.ticket_orders",
            {"source_evidence": [{"schema": "public"}], "metrics": [metric()]},
        )

    def test_same_column_on_another_table_is_not_mapped(self):
        validate_business_sql(
            "SELECT COUNT(*) FROM ticket_orders o JOIN other_orders x ON x.sales_channel = 'App' "
            "WHERE o.sales_channel = 'app' AND x.sales_channel = '网站'",
            {"value_mappings": [mapping()]},
        )
        validate_business_sql(
            "SELECT COUNT(*) FROM other_orders WHERE sales_channel = '网站'",
            {"value_mappings": [mapping()]},
        )

    def test_checks_join_having_and_aggregate_filter(self):
        for sql in [
            "SELECT COUNT(*) FROM customers c JOIN ticket_orders o "
            "ON o.customer_id = c.customer_id AND o.sales_channel = 'App'",
            "SELECT sales_channel, COUNT(*) FROM ticket_orders GROUP BY sales_channel "
            "HAVING sales_channel = 'App'",
            "SELECT COUNT(*) FILTER (WHERE sales_channel = 'App') FROM ticket_orders",
        ]:
            self.assert_business_error(sql, {"value_mappings": [mapping()]})
        validate_business_sql(
            "SELECT COUNT(DISTINCT customer_id) FILTER (WHERE sales_channel = 'app') "
            "AS purchasing_customers FROM ticket_orders",
            {"value_mappings": [mapping()], "metrics": [metric()]},
        )

    def test_tracks_enum_through_cte_column_alias(self):
        validate_business_sql(
            "WITH orders AS (SELECT sales_channel AS channel FROM ticket_orders) "
            "SELECT COUNT(*) FROM orders WHERE channel = 'app'",
            {"value_mappings": [mapping()]},
        )
        self.assert_business_error(
            "WITH orders AS (SELECT sales_channel AS channel FROM ticket_orders) "
            "SELECT COUNT(*) FROM orders WHERE channel = 'App'",
            {"value_mappings": [mapping()]},
        )

    def test_accepts_correct_cte_output_projection(self):
        validate_business_sql(
            "WITH buyers AS (SELECT COUNT(DISTINCT o.customer_id) AS n FROM ticket_orders o) "
            "SELECT n AS purchasing_customers FROM buyers",
            {"metrics": [metric()]},
        )

    def test_unrelated_correct_formula_cannot_validate_wrong_output(self):
        self.assert_business_error(
            "WITH correct AS (SELECT COUNT(DISTINCT customer_id) AS purchasing_customers "
            "FROM ticket_orders) SELECT SUM(ticket_count) AS purchasing_customers FROM ticket_orders",
            {"metrics": [metric()]},
        )

    def test_rejects_tickets_or_other_table_customer_ids_as_people(self):
        for sql in [
            "SELECT SUM(ticket_count) AS purchasing_customers FROM ticket_orders",
            "SELECT COUNT(customer_id) AS purchasing_customers FROM ticket_orders",
            "SELECT COUNT(DISTINCT customer_id) AS purchasing_customers FROM other_orders",
        ]:
            self.assert_business_error(sql, {"metrics": [metric()]})

    def test_requires_actual_output_alias(self):
        error = self.assert_business_error(
            "SELECT COUNT(DISTINCT customer_id) AS people FROM ticket_orders",
            {"metrics": [metric()]},
        )
        self.assertIn("purchasing_customers", error.message)

    def test_refund_ratio_keeps_zero_sales_guard(self):
        ratio = metric(
            "100.0 * SUM(refund_amount) / NULLIF(SUM(total_amount), 0)",
            "refund_amount_percentage",
        )
        validate_business_sql(
            "SELECT 100.0 * SUM(refund_amount) / NULLIF(SUM(total_amount),0) "
            "AS refund_amount_percentage FROM ticket_orders",
            {"metrics": [ratio]},
        )
        self.assert_business_error(
            "SELECT 100.0 * SUM(refund_amount) / SUM(total_amount) "
            "AS refund_amount_percentage FROM ticket_orders",
            {"metrics": [ratio]},
        )

    def test_accepts_net_revenue_equivalence_and_display_wrappers(self):
        net = metric("SUM(total_amount - refund_amount)", "net_revenue")
        for expression in [
            "SUM(o.total_amount - o.refund_amount)",
            "SUM(o.total_amount) - SUM(o.refund_amount)",
            "ROUND(CAST(SUM(o.total_amount) - SUM(o.refund_amount) AS NUMERIC), 2)",
        ]:
            validate_business_sql(
                f"SELECT {expression} AS net_revenue FROM ticket_orders o", {"metrics": [net]},
            )
        validate_business_sql(
            "WITH amounts AS (SELECT total_amount - refund_amount AS net FROM ticket_orders) "
            "SELECT SUM(net) AS net_revenue FROM amounts", {"metrics": [net]},
        )

    def test_accepts_average_sum_divided_by_count(self):
        average = metric("AVG(total_amount - refund_amount)", "average_net_order_amount")
        validate_business_sql(
            "SELECT SUM(total_amount - refund_amount) / NULLIF(COUNT(*), 0) "
            "AS average_net_order_amount FROM ticket_orders", {"metrics": [average]},
        )

    def test_count_star_binds_actual_output_row_source(self):
        orders = metric("COUNT(*)", "order_count")
        validate_business_sql(
            "SELECT COUNT(*) AS order_count FROM ticket_orders", {"metrics": [orders]},
        )
        self.assert_business_error(
            "WITH ignored AS (SELECT COUNT(*) AS order_count FROM ticket_orders) "
            "SELECT COUNT(*) AS order_count FROM movies", {"metrics": [orders]},
        )

    def test_rejects_raw_order_amount_join_fanout_but_accepts_preaggregation(self):
        net = {**metric("SUM(total_amount - refund_amount)", "net_revenue"),
               "unsafe_join_tables": ["order_items", "movie_reviews"]}
        self.assert_business_error(
            "SELECT SUM(o.total_amount - o.refund_amount) AS net_revenue FROM ticket_orders o "
            "JOIN order_items i ON i.order_id = o.order_id", {"metrics": [net]},
        )
        validate_business_sql(
            "WITH revenue AS (SELECT SUM(total_amount - refund_amount) AS net_revenue FROM ticket_orders), "
            "review_stats AS (SELECT COUNT(*) AS review_count FROM movie_reviews) "
            "SELECT r.net_revenue FROM revenue r JOIN review_stats v ON TRUE", {"metrics": [net]},
        )
        self.assert_business_error(
            "WITH revenue AS (SELECT SUM(total_amount - refund_amount) AS net_revenue FROM ticket_orders) "
            "SELECT r.net_revenue FROM revenue r JOIN movie_reviews v ON TRUE", {"metrics": [net]},
        )

    def test_required_dimension_checks_actual_output_and_allows_case_cte(self):
        rewrite = {"dimensions": [dimension("movies", "genre")]}
        self.assert_business_error("SELECT title AS genre FROM movies", rewrite)
        self.assert_business_error(
            "WITH unused AS (SELECT genre FROM movies) SELECT title AS genre FROM movies", rewrite,
        )
        validate_business_sql(
            "WITH x AS (SELECT genre AS kind FROM movies) "
            "SELECT CASE kind WHEN 'action' THEN '动作' ELSE '其他' END AS 类型 FROM x", rewrite,
        )
        validate_business_sql(
            "SELECT COUNT(*) FROM movies", {"dimensions": [dimension("movies", "genre", False)]},
        )

    def test_join_paths_accept_reverse_on_where_and_using(self):
        for from_clause in [
            "ticket_orders o JOIN screenings s ON s.screening_id = o.screening_id "
            "JOIN movies m ON m.movie_id = s.movie_id",
            "ticket_orders o CROSS JOIN screenings s CROSS JOIN movies m "
            "WHERE o.screening_id = s.screening_id AND s.movie_id = m.movie_id",
            "ticket_orders o JOIN screenings s USING (screening_id) JOIN movies m USING (movie_id)",
        ]:
            validate_business_sql(
                "SELECT m.genre, SUM(o.total_amount - o.refund_amount) AS net_revenue FROM " + from_clause,
                movie_rewrite(),
            )

    def test_join_paths_reject_wrong_real_columns_or_or_bypass(self):
        for on in ["o.screening_id = s.movie_id", "o.screening_id = s.screening_id OR o.total_amount > 10"]:
            self.assert_business_error(
                "SELECT m.genre, SUM(o.total_amount - o.refund_amount) AS net_revenue "
                f"FROM ticket_orders o JOIN screenings s ON {on} JOIN movies m ON m.movie_id = s.movie_id",
                movie_rewrite(),
            )

    def test_city_output_must_follow_cinema_city_role_even_with_extra_correct_join(self):
        rewrite = {
            "metrics": [metric("SUM(total_amount - refund_amount)", "net_revenue")],
            "dimensions": [dimension("cities", "city_name")],
            "join_paths": [{"tables": ["ticket_orders", "screenings", "cinemas", "cities"],
                            "joins": [relation("ticket_orders", "screening_id", "screenings"),
                                      relation("screenings", "cinema_id", "cinemas"),
                                      relation("cinemas", "city_id", "cities")]}],
        }
        joins = (
            "FROM ticket_orders o JOIN screenings s ON s.screening_id = o.screening_id "
            "JOIN cinemas c ON c.cinema_id = s.cinema_id JOIN cities actual ON actual.city_id = c.city_id "
            "JOIN customers u ON u.customer_id = o.customer_id JOIN cities residence ON residence.city_id = u.city_id"
        )
        self.assert_business_error(
            "SELECT residence.city_name, SUM(o.total_amount - o.refund_amount) AS net_revenue " + joins, rewrite,
        )
        validate_business_sql(
            "SELECT actual.city_name, SUM(o.total_amount - o.refund_amount) AS net_revenue " + joins, rewrite,
        )

    def test_derived_foreign_key_path_through_cte(self):
        validate_business_sql(
            "WITH x AS (SELECT s.movie_id, SUM(o.total_amount - o.refund_amount) AS net_revenue "
            "FROM ticket_orders o JOIN screenings s ON s.screening_id = o.screening_id GROUP BY s.movie_id) "
            "SELECT m.genre, x.net_revenue FROM x JOIN movies m ON m.movie_id = x.movie_id", movie_rewrite(),
        )

    def test_independent_aggregate_scans_can_bridge_configured_unique_city_key(self):
        rewrite = {
            "metrics": [metric("SUM(total_amount - refund_amount)", "net_revenue")],
            "dimensions": [dimension("cities", "city_name"), dimension("cinemas", "cinema_name")],
            "join_paths": [{"tables": ["ticket_orders", "screenings", "cinemas", "cities"],
                            "joins": [relation("ticket_orders", "screening_id", "screenings"),
                                      relation("screenings", "cinema_id", "cinemas"),
                                      relation("cinemas", "city_id", "cities")]}],
        }
        scans = (
            "WITH city_revenue AS ("
            "SELECT ci.city_id, ci.city_name, SUM(o.total_amount - o.refund_amount) AS city_net_revenue "
            "FROM ticket_orders o JOIN screenings s ON s.screening_id = o.screening_id "
            "JOIN cinemas c ON c.cinema_id = s.cinema_id JOIN cities ci ON ci.city_id = c.city_id "
            "GROUP BY ci.city_id, ci.city_name), cinema_revenue AS ("
            "SELECT ci.city_id, ci.city_name, c.cinema_name, SUM(o.total_amount - o.refund_amount) AS net_revenue "
            "FROM ticket_orders o JOIN screenings s ON s.screening_id = o.screening_id "
            "JOIN cinemas c ON c.cinema_id = s.cinema_id JOIN cities ci ON ci.city_id = c.city_id "
            "GROUP BY ci.city_id, ci.city_name, c.cinema_name) "
            "SELECT tc.city_name, rc.cinema_name, rc.net_revenue "
            "FROM city_revenue tc JOIN cinema_revenue rc ON "
        )
        validate_business_sql(scans + "tc.city_id = rc.city_id", rewrite)
        rewrite["query_contract"] = {"schema_keys": {
            "cities": {"primary_key": ["city_id"], "unique_keys": [], "metadata_verified": True},
            "cinemas": {"primary_key": ["cinema_id"], "unique_keys": [], "metadata_verified": True},
            "screenings": {"primary_key": ["screening_id"], "unique_keys": [], "metadata_verified": True},
        }}
        validate_business_sql(scans + "tc.city_id = rc.city_id", rewrite)
        self.assert_business_error(scans + "tc.city_name = rc.city_name", rewrite)
        self.assert_business_error(scans + "tc.city_id = rc.city_id OR tc.city_name = rc.city_name", rewrite)

    def test_result_dimension_helper_uses_actual_column_positions_and_cte_case(self):
        rewrite = {"dimensions": [
            {**dimension("customers", "membership_level"), "id": "membership_level"},
            {**dimension("movies", "genre"), "id": "movie_genre"},
        ]}
        result = business_result_dimensions(
            "WITH x AS (SELECT membership_level AS level FROM customers) "
            "SELECT 1 AS n, CASE level WHEN 'gold' THEN '金卡' ELSE '其他' END AS label FROM x",
            rewrite, ["n", "会员等级"],
        )
        self.assertEqual(result, {"membership_level": "会员等级"})

    def test_result_dimension_helper_omits_nonunique_or_incorrect_sources(self):
        rewrite = {"dimensions": [{**dimension("movies", "genre"), "id": "movie_genre"}]}
        for sql, columns in [
            ("SELECT genre AS raw, genre AS label FROM movies", ["raw", "label"]),
            ("SELECT genre, title AS genre FROM movies", ["genre", "genre"]),
            ("SELECT title AS genre FROM movies", ["genre"]),
            ("SELECT COUNT(DISTINCT genre) AS genre FROM movies", ["genre"]),
            ("SELECT * FROM movies", ["movie_id", "genre"]),
            ("SELECT genre FROM movies", ["genre", "extra"]),
        ]:
            with self.subTest(sql=sql):
                self.assertEqual(business_result_dimensions(sql, rewrite, columns), {})

    def test_zero_fill_independent_movie_revenue_and_reviews(self):
        net = {**metric("SUM(total_amount - refund_amount)", "net_revenue"),
               "unsafe_join_tables": ["movie_reviews", "order_items"]}
        for aggregate in ["SUM(o.total_amount - o.refund_amount)", "SUM(o.total_amount) - SUM(o.refund_amount)"]:
            for default in ["0", "0::numeric"]:
                validate_business_sql(
                    "WITH revenue AS (SELECT s.movie_id, " + aggregate + " AS net_revenue "
                    "FROM ticket_orders o JOIN screenings s ON s.screening_id=o.screening_id GROUP BY s.movie_id), "
                    "reviews AS (SELECT movie_id, AVG(rating) AS score, COUNT(*) AS n FROM movie_reviews GROUP BY movie_id), "
                    "combined AS (SELECT rv.movie_id, rv.score, COALESCE(r.net_revenue, " + default + ") AS net_revenue "
                    "FROM reviews rv LEFT JOIN revenue r ON r.movie_id=rv.movie_id), "
                    "ranked AS (SELECT movie_id, net_revenue, ROW_NUMBER() OVER (ORDER BY net_revenue DESC) AS rank FROM combined) "
                    "SELECT movie_id, net_revenue FROM ranked", {"metrics": [net]},
                )
        for formula in ["COALESCE(SUM(total_amount), 0)", "COALESCE(SUM(total_amount-refund_amount), 5)"]:
            self.assert_business_error(
                f"SELECT {formula} AS net_revenue FROM ticket_orders", {"metrics": [net]},
            )

    def test_movie_revenue_still_rejects_unaggregated_or_incompletely_keyed_reviews(self):
        net = {**metric("SUM(total_amount - refund_amount)", "net_revenue"),
               "unsafe_join_tables": ["movie_reviews"]}
        for review_sql in [
            "SELECT movie_id, rating FROM movie_reviews",
            "SELECT movie_id, cinema_id, AVG(rating) AS score FROM movie_reviews GROUP BY movie_id, cinema_id",
        ]:
            self.assert_business_error(
                "WITH revenue AS (SELECT s.movie_id, SUM(o.total_amount-o.refund_amount) AS net_revenue "
                "FROM ticket_orders o JOIN screenings s ON s.screening_id=o.screening_id GROUP BY s.movie_id), "
                "reviews AS (" + review_sql + ") SELECT COALESCE(r.net_revenue,0) AS net_revenue "
                "FROM revenue r JOIN reviews rv ON rv.movie_id=r.movie_id", {"metrics": [net]},
            )
        self.assert_business_error(
            "WITH revenue AS (SELECT s.movie_id, SUM(o.total_amount-o.refund_amount) AS net_revenue "
            "FROM ticket_orders o JOIN screenings s ON s.screening_id=o.screening_id GROUP BY s.movie_id), "
            "reviews AS (SELECT movie_id, COUNT(*) AS n FROM movie_reviews GROUP BY movie_id) "
            "SELECT COALESCE(r.net_revenue,0) AS net_revenue FROM revenue r JOIN reviews rv ON rv.movie_id=rv.movie_id",
            {"metrics": [net]},
        )

    def test_fk_path_can_follow_true_movie_id_equality_transitivity(self):
        sql = (
            "WITH reviews AS (SELECT movie_id, COUNT(*) AS review_count FROM movie_reviews GROUP BY movie_id), "
            "revenue AS (SELECT s.movie_id, SUM(o.total_amount-o.refund_amount) AS net_revenue "
            "FROM ticket_orders o JOIN screenings s ON s.screening_id=o.screening_id GROUP BY s.movie_id) "
            "SELECT m.genre, COALESCE(v.net_revenue,0) AS net_revenue FROM reviews r "
            "JOIN movies m ON m.movie_id=r.movie_id LEFT JOIN revenue v ON v.movie_id=r.movie_id"
        )
        validate_business_sql(sql, movie_rewrite())
        self.assert_business_error(sql.replace("v.movie_id=r.movie_id", "v.movie_id=r.review_count"), movie_rewrite())

    def test_parallel_rank_contract_uses_raw_metrics_and_configured_threshold(self):
        for minimum in [20, 80]:
            validate_business_sql(review_rank_sql(minimum), review_rank_rewrite(minimum))
        validate_business_sql(
            review_rank_sql().replace("COUNT(*) >= 20", "19 < COUNT(*)"), review_rank_rewrite(),
        )
        # Display precision changes do not change the values used for ranking.
        validate_business_sql(review_rank_sql().replace(
            "SELECT movie_id,title,viewer_average_rating,review_count,net_revenue,rating_rank,revenue_rank",
            "SELECT movie_id,title,ROUND(viewer_average_rating,2) AS viewer_average_rating,review_count,net_revenue,rating_rank,revenue_rank",
        ), review_rank_rewrite())

    def test_parallel_rank_contract_rejects_wrong_metric_precision_policy_and_direction(self):
        for original, replacement in [
            ("ORDER BY viewer_average_rating DESC,movie_id", "ORDER BY ROUND(viewer_average_rating,2) DESC,movie_id"),
            ("ORDER BY viewer_average_rating DESC,movie_id", "ORDER BY review_count DESC,movie_id"),
            ("ORDER BY viewer_average_rating DESC,movie_id", "ORDER BY viewer_average_rating ASC,movie_id"),
            ("ROW_NUMBER()", "RANK()"),
            ("DESC,movie_id", "DESC,title"),
            ("DESC,movie_id", "DESC"),
        ]:
            with self.subTest(replacement=replacement):
                self.assert_business_error(review_rank_sql().replace(original, replacement), review_rank_rewrite())

    def test_parallel_rank_contract_rejects_late_missing_nullable_and_or_population_gates(self):
        for sql in [
            review_rank_sql().replace(" HAVING COUNT(*) >= 20", ""),
            review_rank_sql().replace(" HAVING COUNT(*) >= 20", "").replace("WHERE rating_rank", "WHERE review_count>=20 AND rating_rank"),
            review_rank_sql().replace("HAVING COUNT(*) >= 20", "HAVING COUNT(*) >= 20 OR AVG(rating)>8"),
            review_rank_sql().replace("FROM reviews rv JOIN movies m ON m.movie_id=rv.movie_id", "FROM movies m LEFT JOIN reviews rv ON m.movie_id=rv.movie_id"),
            review_rank_sql().replace("FROM eligible)", "FROM (SELECT * FROM eligible LIMIT 20) e)"),
        ]:
            with self.subTest(sql=sql):
                self.assert_business_error(sql, review_rank_rewrite())
        validate_business_sql(review_rank_sql().replace(
            "FROM reviews rv JOIN movies m ON m.movie_id=rv.movie_id",
            "FROM movies m LEFT JOIN reviews rv ON m.movie_id=rv.movie_id",
        ).replace("LEFT JOIN revenue v ON v.movie_id=rv.movie_id)",
                  "LEFT JOIN revenue v ON v.movie_id=rv.movie_id WHERE rv.review_count>=20)"), review_rank_rewrite())

    def test_parallel_rank_contract_rejects_sequential_rank_populations(self):
        sql = review_rank_sql().replace(
            "ROW_NUMBER() OVER(ORDER BY net_revenue DESC,movie_id) AS revenue_rank FROM eligible)",
            "0 AS ignored FROM eligible), revenue_ranked AS (SELECT movie_id,title,viewer_average_rating,review_count,net_revenue,rating_rank, "
            "ROW_NUMBER() OVER(ORDER BY net_revenue DESC,movie_id) AS revenue_rank FROM ranked WHERE rating_rank<=20)",
        ).replace("FROM ranked WHERE rating_rank", "FROM revenue_ranked WHERE rating_rank")
        self.assert_business_error(sql, review_rank_rewrite())

    def test_post_rank_contract_traces_window_through_projection_layers(self):
        sql = review_rank_sql()
        final = "SELECT movie_id,title,viewer_average_rating,review_count,net_revenue,rating_rank,revenue_rank "
        sql = sql.replace(final + "FROM ranked WHERE rating_rank<=20 AND revenue_rank>20",
                          "filtered AS (" + final + "FROM ranked WHERE 20>=rating_rank AND revenue_rank>=21) " + final + "FROM filtered")
        sql = sql.replace("FROM eligible) filtered AS", "FROM eligible), filtered AS")
        validate_business_sql(sql, review_rank_rewrite())
        rewrite = review_rank_rewrite()
        rewrite["query_contract"]["analysis"][0]["post_rank_filters"][1]["maximum"] = 30
        validate_business_sql(review_rank_sql().replace("revenue_rank>20", "revenue_rank>30"), rewrite)

    def test_post_rank_contract_rejects_reverse_missing_fake_or_bypassed_filters(self):
        for condition in [
            "rating_rank<=20",
            "rating_rank<=20 AND revenue_rank<=20",
            "rating_rank<=20 AND revenue_rank>19",
            "rating_rank<=20 AND (revenue_rank>20 OR review_count>100)",
            "rating_rank<=20 OR revenue_rank>20",
            "20>=rating_rank AND 1>20",
        ]:
            with self.subTest(condition=condition):
                self.assert_business_error(review_rank_sql().replace(
                    "rating_rank<=20 AND revenue_rank>20", condition), review_rank_rewrite())

    def test_verified_keys_protect_generic_sales_feedback_join_grain(self):
        total = {**metric("SUM(paid_amount-refunded_amount)", "received"), "table": "sales"}
        keys = {
            "sales": {"primary_key": ["sale_id"], "unique_keys": [], "metadata_verified": True},
            "feedback": {"primary_key": ["feedback_id"], "unique_keys": [], "metadata_verified": True},
            "items": {"primary_key": ["item_id"], "unique_keys": [], "metadata_verified": True},
        }
        rewrite = {"metrics": [total], "query_contract": {"schema_keys": keys}}
        self.assert_business_error(
            "SELECT SUM(s.paid_amount-s.refunded_amount) AS received FROM sales s "
            "JOIN feedback f ON f.item_id=s.item_id", rewrite,
        )
        validate_business_sql(
            "SELECT SUM(s.paid_amount-s.refunded_amount) AS received FROM sales s "
            "JOIN items i ON i.item_id=s.item_id", rewrite,
        )
        validate_business_sql(
            "WITH comments AS (SELECT item_id,COUNT(*) AS n FROM feedback GROUP BY item_id) "
            "SELECT SUM(s.paid_amount-s.refunded_amount) AS received FROM sales s "
            "LEFT JOIN comments f ON f.item_id=s.item_id", rewrite,
        )
        self.assert_business_error(
            "WITH comments AS (SELECT item_id,language,COUNT(*) AS n FROM feedback GROUP BY item_id,language) "
            "SELECT SUM(s.paid_amount-s.refunded_amount) AS received FROM sales s "
            "LEFT JOIN comments f ON f.item_id=s.item_id", rewrite,
        )

    def test_verified_composite_unique_keys_need_every_join_key(self):
        total = {**metric("SUM(paid_amount-refunded_amount)", "received"), "table": "sales"}
        rewrite = {"metrics": [total], "query_contract": {"schema_keys": {
            "item_languages": {"primary_key": [], "unique_keys": [["item_id", "language"]], "metadata_verified": True},
        }}}
        base = "SELECT SUM(s.paid_amount-s.refunded_amount) AS received FROM sales s JOIN item_languages i ON i.item_id=s.item_id"
        self.assert_business_error(base, rewrite)
        validate_business_sql(base + " AND i.language=s.language", rewrite)
        self.assert_business_error(base + " AND i.language=i.language", rewrite)
        self.assert_business_error(base + " OR i.language=s.language", rewrite)

    def test_unverified_keys_never_override_legacy_unsafe_guard(self):
        total = {**metric("SUM(paid_amount-refunded_amount)", "received"), "table": "sales", "unsafe_join_tables": ["feedback"]}
        rewrite = {"metrics": [total], "query_contract": {"schema_keys": {
            "feedback": {"primary_key": ["item_id"], "unique_keys": [], "metadata_verified": False},
        }}}
        self.assert_business_error(
            "SELECT SUM(s.paid_amount-s.refunded_amount) AS received FROM sales s JOIN feedback f ON f.item_id=s.item_id", rewrite,
        )

    def test_verified_lookup_keys_survive_cte_and_redundant_group_columns(self):
        total = {**metric("SUM(paid_amount-refunded_amount)", "received"), "table": "sales"}
        rewrite = {"metrics": [total], "query_contract": {"schema_keys": {
            "items": {"primary_key": ["item_id"], "unique_keys": [], "metadata_verified": True},
        }}}
        for source in [
            "SELECT item_id,title FROM items",
            "SELECT i.item_id,i.title,COUNT(*) AS n FROM items i JOIN feedback f ON f.item_id=i.item_id GROUP BY i.item_id,i.title",
        ]:
            validate_business_sql(
                "WITH labels AS (" + source + ") SELECT SUM(s.paid_amount-s.refunded_amount) AS received "
                "FROM sales s JOIN labels l ON l.item_id=s.item_id", rewrite,
            )
        self.assert_business_error(
            "WITH labels AS (SELECT i.item_id,i.title,f.language,COUNT(*) AS n FROM items i "
            "JOIN feedback f ON f.item_id=i.item_id GROUP BY i.item_id,i.title,f.language) "
            "SELECT SUM(s.paid_amount-s.refunded_amount) AS received FROM sales s JOIN labels l ON l.item_id=s.item_id", rewrite,
        )

    def test_nullable_columns_disable_sum_distribution_equivalence(self):
        total = {**metric("SUM(paid_amount-refunded_amount)", "received"), "table": "sales"}
        rewrite = {"metrics": [total], "query_contract": {"metrics": [{
            "id": "received", "source": {"nullability": {"sales.paid_amount": "NO", "sales.refunded_amount": "YES"}},
        }]}}
        validate_business_sql("SELECT SUM(paid_amount-refunded_amount) AS received FROM sales", rewrite)
        self.assert_business_error("SELECT SUM(paid_amount)-SUM(refunded_amount) AS received FROM sales", rewrite)

    def test_nullable_average_preserves_its_nonnull_denominator(self):
        average = {**metric("AVG(score)", "average_score"), "table": "feedback"}
        rewrite = {"metrics": [average], "query_contract": {"metrics": [{
            "id": "average_score", "source": {"nullability": {"feedback.score": "YES"}},
        }]}}
        validate_business_sql("SELECT AVG(score) AS average_score FROM feedback", rewrite)
        self.assert_business_error("SELECT SUM(score)/NULLIF(COUNT(*),0) AS average_score FROM feedback", rewrite)
        average["alternatives"] = ["SUM(score)/NULLIF(COUNT(score),0)"]
        validate_business_sql("SELECT SUM(score)/NULLIF(COUNT(score),0) AS average_score FROM feedback", rewrite)

    def test_nested_zero_fill_of_generic_dimension_driven_sum(self):
        total = {**metric("SUM(paid_amount-refunded_amount)", "received"), "table": "sales"}
        rewrite = {"metrics": [total], "query_contract": {"schema_keys": {
            "accounts": {"primary_key": ["account_id"], "unique_keys": [], "metadata_verified": True},
            "sessions": {"primary_key": ["session_id"], "unique_keys": [], "metadata_verified": True},
            "sales": {"primary_key": ["sale_id"], "unique_keys": [], "metadata_verified": True},
        }}}
        sql = (
            "WITH amounts AS (SELECT a.account_id, COALESCE(SUM(s.paid_amount-s.refunded_amount),0) AS received "
            "FROM accounts a LEFT JOIN sessions e ON e.account_id=a.account_id "
            "LEFT JOIN sales s ON s.session_id=e.session_id GROUP BY a.account_id) "
            "SELECT COALESCE(COALESCE(received,0::numeric),0) AS received FROM amounts"
        )
        validate_business_sql(sql, rewrite)
        for bad in [
            sql.replace("SUM(s.paid_amount-s.refunded_amount)", "SUM(s.paid_amount)"),
            sql.replace("SUM(s.paid_amount-s.refunded_amount),0)", "SUM(s.paid_amount-s.refunded_amount),5)"),
            sql.replace("received,0::numeric", "received,7::numeric"),
            sql.replace("COALESCE(received,0::numeric),0)", "COALESCE(received,0::numeric),9)"),
        ]:
            self.assert_business_error(bad, rewrite)
        null_rewrite = {**rewrite, "query_contract": {**rewrite["query_contract"], "metrics": [{
            "id": "received", "source": {"nullability": {"sales.refunded_amount": "YES"}},
        }]}}
        self.assert_business_error(sql.replace("SUM(s.paid_amount-s.refunded_amount)",
                                               "SUM(s.paid_amount)-SUM(s.refunded_amount)"), null_rewrite)

    def test_average_zero_fill_requires_direct_nonnull_and_nonempty_input(self):
        rewrite = feedback_average_rewrite()
        for sql in [
            "SELECT account_id,COALESCE(AVG(score),0) AS mean_score FROM feedback GROUP BY account_id",
            "WITH scores AS (SELECT account_id,AVG(score) AS mean_score FROM feedback GROUP BY account_id) "
            "SELECT COALESCE(COALESCE(s.mean_score,0),0) AS mean_score FROM accounts a JOIN scores s ON s.account_id=a.account_id",
            "SELECT COALESCE(AVG(score),0) AS mean_score FROM feedback HAVING COUNT(feedback_id)>0",
            "SELECT a.account_id,COALESCE(AVG(f.score),0) AS mean_score FROM accounts a LEFT JOIN feedback f "
            "ON f.account_id=a.account_id GROUP BY a.account_id HAVING COUNT(f.feedback_id)>=1",
        ]:
            with self.subTest(sql=sql):
                validate_business_sql(sql, rewrite)
        for nullable in ["YES", "UNKNOWN"]:
            error = self.assert_business_error(
                "SELECT account_id,COALESCE(AVG(score),0) AS mean_score FROM feedback GROUP BY account_id",
                feedback_average_rewrite(nullable),
            )
            self.assertEqual(error.business_failure_reason, "nullable_aggregate_zero_fill")
            self.assertEqual(error.business_failure_details["source_table"], "feedback")

    def test_average_zero_fill_rejects_empty_nullable_projection_and_null_expression(self):
        rewrite = feedback_average_rewrite()
        for sql in [
            "SELECT COALESCE(AVG(score),0) AS mean_score FROM feedback",
            "SELECT COALESCE(AVG(score),0) AS mean_score FROM feedback HAVING COUNT(feedback_id)>=0",
            "SELECT account_id,COALESCE(AVG(NULLIF(score,0)),0) AS mean_score FROM feedback GROUP BY account_id",
            "SELECT account_id,COALESCE(AVG(CASE WHEN score>0 THEN score ELSE NULL END),0) AS mean_score FROM feedback GROUP BY account_id",
            "SELECT account_id,COALESCE(AVG(score),5) AS mean_score FROM feedback GROUP BY account_id",
            "WITH scores AS (SELECT account_id,AVG(score) AS mean_score FROM feedback GROUP BY account_id) "
            "SELECT COALESCE(s.mean_score,0) AS mean_score FROM accounts a LEFT JOIN scores s ON s.account_id=a.account_id",
            "WITH raw_scores AS (SELECT account_id,AVG(score) AS mean_score FROM feedback GROUP BY account_id), "
            "scores AS (SELECT * FROM raw_scores) SELECT COALESCE(s.mean_score,0) AS mean_score FROM accounts a "
            "LEFT JOIN scores s ON s.account_id=a.account_id",
            "SELECT a.account_id,COALESCE(AVG(f.score),0) AS mean_score FROM accounts a LEFT JOIN feedback f "
            "ON f.account_id=a.account_id GROUP BY a.account_id",
            "SELECT a.account_id,COALESCE(AVG(f.score),0) AS mean_score FROM accounts a LEFT JOIN feedback f "
            "ON f.account_id=a.account_id GROUP BY a.account_id HAVING COUNT(*)>0",
            "SELECT a.account_id,COALESCE(AVG(f.score),0) AS mean_score FROM accounts a LEFT JOIN feedback f "
            "ON f.account_id=a.account_id GROUP BY a.account_id HAVING COUNT(f.feedback_id)>0 OR COUNT(*)>0",
        ]:
            with self.subTest(sql=sql):
                self.assert_business_error(sql, rewrite)

    def test_nonnull_average_zero_fill_never_bypasses_raw_fanout(self):
        rewrite = feedback_average_rewrite()
        rewrite["query_contract"]["schema_keys"]["sales"] = {
            "primary_key": ["sale_id"], "unique_keys": [], "metadata_verified": True,
        }
        error = self.assert_business_error(
            "SELECT f.account_id,COALESCE(AVG(f.score),0) AS mean_score FROM feedback f "
            "JOIN sales s ON s.account_id=f.account_id GROUP BY f.account_id", rewrite,
        )
        self.assertEqual(error.business_failure_reason, "join_fanout")
        self.assertEqual(error.business_failure_details["metric_id"], "mean_score")

    def test_contextual_zero_fill_and_same_scope_display_round_preserve_input_ranking(self):
        rewrite = review_rank_rewrite()
        rewrite["query_contract"]["metrics"] = [{
            "id": "movie_review_average", "source": {"nullability": {"movie_reviews.rating": "NO"}},
        }]
        sql = review_rank_sql().replace(
            "rv.viewer_average_rating,rv.review_count", "COALESCE(rv.viewer_average_rating,0) AS viewer_average_rating,rv.review_count",
        ).replace("COALESCE(v.net_revenue,0)", "COALESCE(COALESCE(v.net_revenue,0),0)").replace(
            "SELECT movie_id,title,viewer_average_rating,review_count,net_revenue, ROW_NUMBER()",
            "SELECT title,ROUND(viewer_average_rating,2) AS viewer_average_rating,review_count,ROUND(net_revenue,2) AS net_revenue, ROW_NUMBER()",
        ).replace("SELECT movie_id,title,viewer_average_rating,review_count,net_revenue,rating_rank,revenue_rank",
                  "SELECT title,viewer_average_rating,review_count,net_revenue,rating_rank,revenue_rank")
        validate_business_sql(sql, rewrite)
        error = self.assert_business_error(sql.replace("ORDER BY viewer_average_rating DESC",
                                                       "ORDER BY ROUND(viewer_average_rating,2) DESC"), rewrite)
        self.assertEqual(error.business_failure_reason, "rank_metric_mismatch")

    def test_count_star_can_bind_verified_many_to_one_fact_grain(self):
        rewrite = sales_count_rewrite()
        for source in [
            "sales s JOIN accounts a ON a.account_id=s.account_id JOIN sessions e ON e.session_id=s.session_id "
            "JOIN items i ON i.item_id=e.item_id",
            "sales s LEFT JOIN accounts a ON a.account_id=s.account_id",
            "accounts a RIGHT JOIN sales s ON a.account_id=s.account_id",
            "sales s JOIN item_languages i ON i.item_id=s.item_id AND i.language=s.language",
        ]:
            with self.subTest(source=source):
                validate_business_sql("SELECT COUNT(*) AS sale_count FROM " + source, rewrite)

    def test_count_star_requires_complete_keys_and_nonnullable_fact_carrier(self):
        rewrite = sales_count_rewrite()
        for source in [
            "sales s JOIN feedback f ON f.account_id=s.account_id",
            "sales s JOIN item_languages i ON i.item_id=s.item_id",
            "sales s JOIN item_languages i ON i.item_id=s.item_id OR i.language=s.language",
            "accounts a LEFT JOIN sales s ON a.account_id=s.account_id",
            "sales s FULL JOIN accounts a ON a.account_id=s.account_id",
            "sales s JOIN sales duplicate ON duplicate.sale_id=s.sale_id",
        ]:
            with self.subTest(source=source):
                error = self.assert_business_error("SELECT COUNT(*) AS sale_count FROM " + source, rewrite)
                self.assertEqual(error.business_failure_reason, "count_source_ambiguity")
        rewrite["query_contract"]["schema_keys"]["accounts"]["metadata_verified"] = False
        self.assert_business_error("SELECT COUNT(*) AS sale_count FROM sales s JOIN accounts a ON a.account_id=s.account_id", rewrite)

    def test_count_star_follows_only_proven_row_preserving_cte_projections(self):
        rewrite = sales_count_rewrite()
        validate_business_sql(
            "WITH joined AS (SELECT s.sale_id,s.account_id FROM sales s JOIN accounts a ON a.account_id=s.account_id), "
            "renamed AS (SELECT sale_id AS id,account_id FROM joined) SELECT COUNT(*) AS sale_count FROM renamed", rewrite,
        )
        for projection in [
            "SELECT account_id,COUNT(*) AS n FROM sales GROUP BY account_id",
            "SELECT DISTINCT account_id FROM sales",
            "SELECT s.sale_id FROM sales s JOIN feedback f ON f.account_id=s.account_id",
        ]:
            self.assert_business_error("WITH rows AS (" + projection + ") SELECT COUNT(*) AS sale_count FROM rows", rewrite)

    def test_count_zero_fill_is_identity_only_for_actual_nonnullable_count(self):
        rewrite = sales_count_rewrite()
        for sql in [
            "SELECT COALESCE(COUNT(*),0) AS sale_count FROM sales",
            "WITH counts AS (SELECT account_id,COUNT(*) AS n FROM sales GROUP BY account_id) "
            "SELECT COALESCE(COALESCE(c.n,0::numeric),0) AS sale_count FROM counts c JOIN accounts a ON a.account_id=c.account_id",
            "SELECT COALESCE(COUNT(s.sale_id),0) AS sale_count FROM accounts a LEFT JOIN sales s "
            "ON s.account_id=a.account_id GROUP BY a.account_id",
        ]:
            validate_business_sql(sql, rewrite)
        for sql in [
            "SELECT COALESCE(COUNT(*),5) AS sale_count FROM sales",
            "SELECT COALESCE(SUM(paid_amount),0) AS sale_count FROM sales",
            "SELECT COALESCE(SUM(paid_amount)/COUNT(*),0) AS sale_count FROM sales",
            "SELECT COALESCE(COUNT(score),0) AS sale_count FROM sales",
            "WITH counts AS (SELECT account_id,COUNT(*) AS n FROM sales GROUP BY account_id) "
            "SELECT COALESCE(c.n,0) AS sale_count FROM accounts a LEFT JOIN counts c ON c.account_id=a.account_id",
        ]:
            self.assert_business_error(sql, rewrite)

    def test_verified_joined_count_binding_applies_to_rank_and_population_gate(self):
        rewrite = sales_count_rewrite()
        rewrite["query_contract"]["analysis"] = [{
            "kind": "parallel_rankings", "policy": "ROW_NUMBER", "shared_population": True,
            "rankings": [{"output_alias": "count_rank", "metric_id": "sale_count", "direction": "desc", "partition_by": []}],
            "tie_keys": ["accounts.account_id"],
            "population_min_count": {"metric_id": "sale_count", "output_alias": "sale_count", "operator": "gte", "minimum": 2},
            "post_rank_filters": [{"output_alias": "count_rank", "operator": "lte", "maximum": 3}],
        }]
        validate_business_sql(
            "WITH ranked AS (SELECT a.account_id,COUNT(*) AS sale_count,ROW_NUMBER() OVER "
            "(ORDER BY COUNT(*) DESC,a.account_id) AS count_rank FROM sales s "
            "JOIN accounts a ON a.account_id=s.account_id GROUP BY a.account_id HAVING COUNT(*)>=2) "
            "SELECT sale_count,count_rank FROM ranked WHERE count_rank<=3", rewrite,
        )

    def test_unique_join_components_must_be_connected_to_the_fact_carrier(self):
        rewrite = sales_count_rewrite()
        sources = [
            "sales s CROSS JOIN accounts a JOIN sessions e ON e.session_id=a.account_id",
            "sales s CROSS JOIN accounts a JOIN sessions e ON e.session_id=a.account_id "
            "JOIN items i ON i.item_id=e.session_id WHERE a.account_id=i.item_id",
            "sales s JOIN item_languages i ON i.item_id=s.item_id CROSS JOIN accounts a "
            "JOIN sessions e ON e.session_id=a.account_id WHERE i.language=a.language",
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assert_business_error("SELECT COUNT(*) AS sale_count FROM " + source, rewrite)
                # Replacing COUNT(*) with COUNT(pk) cannot repair a disconnected fanout.
                error = self.assert_business_error("SELECT COUNT(s.sale_id) AS sale_count FROM " + source, rewrite)
                self.assertEqual(error.business_failure_reason, "join_fanout")
                total = {**metric("SUM(paid_amount-refunded_amount)", "received"), "table": "sales"}
                self.assert_business_error("SELECT SUM(s.paid_amount-s.refunded_amount) AS received FROM " + source,
                                           {**rewrite, "metrics": [total]})
        validate_business_sql(
            "SELECT COUNT(*) AS sale_count FROM sales s JOIN accounts a ON a.account_id=s.account_id "
            "JOIN item_languages i ON i.item_id=s.item_id AND i.language=a.language", rewrite,
        )

    def test_count_cte_lineage_cannot_borrow_a_different_scans_correct_foreign_key(self):
        rewrite = {
            "metrics": [metric("COUNT(*)", "order_count")],
            "dimensions": [dimension("customers", "membership_level")],
            "join_paths": [{"tables": ["ticket_orders", "customers"], "joins": [relation("ticket_orders", "customer_id", "customers")]}],
            "query_contract": {"schema_keys": {
                "ticket_orders": {"primary_key": ["order_id"], "metadata_verified": True},
                "customers": {"primary_key": ["customer_id"], "metadata_verified": True},
            }},
        }
        sql = (
            "WITH base AS (SELECT order_id,customer_id FROM ticket_orders), stats AS "
            "(SELECT c.customer_id,c.membership_level,COUNT(*) AS n FROM ticket_orders o JOIN customers c "
            "ON c.customer_id=o.customer_id GROUP BY c.customer_id,c.membership_level) "
            "SELECT stats.membership_level,COUNT(*) AS order_count FROM base b JOIN stats "
            "ON stats.customer_id=b.customer_id GROUP BY stats.membership_level"
        )
        validate_business_sql(sql, rewrite)
        self.assert_business_error(sql.replace("stats.customer_id=b.customer_id", "stats.customer_id=b.order_id"), rewrite)


if __name__ == "__main__":
    unittest.main()
