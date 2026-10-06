import copy
import unittest

from app.core.errors import AppError
from app.services.query_contract_service import build_query_contract


def contract_schema():
    return [
        {
            "table_name": "events", "schema_metadata_version": 2,
            "columns": [{"name": name, "nullable": "NO"} for name in ["event_id", "account_id", "amount"]],
            "primary_key": ["event_id"], "unique_keys": [["event_id"]],
            "foreign_keys": [{
                "columns": ["account_id"], "referenced_schema": "business",
                "referenced_table": "accounts", "referenced_columns": ["account_id"],
                "constraint_name": "events_account_fk", "cardinality": "many_to_one", "nullable": False,
            }],
        },
        {
            "table_name": "accounts", "schema_metadata_version": 2,
            "columns": [{"name": name, "nullable": "NO"} for name in ["account_id", "label"]],
            "primary_key": ["account_id"], "unique_keys": [["account_id"]], "foreign_keys": [],
        },
    ]


def resolved_rewrite():
    return {
        "database_schema": "business", "original_question": "arbitrary natural language",
        "metrics": [
            {"id": "mean_value", "table": "events", "output_alias": "mean_value", "expression": "AVG(amount)", "alternatives": [], "grain_keys": ["events.event_id"]},
            {"id": "total_value", "table": "events", "output_alias": "total_value", "expression": "SUM(amount)", "alternatives": [], "grain_keys": ["events.event_id"]},
            {"id": "event_count", "table": "events", "output_alias": "event_count", "expression": "COUNT(event_id)", "alternatives": ["COUNT(*)"], "grain_keys": ["events.event_id"]},
        ],
        "dimensions": [{"id": "account", "table": "accounts", "column": "label", "group_by": ["account_id", "label"], "entity_keys": ["accounts.account_id"], "require_output": True}],
        "join_paths": [{"joins": [{"left_table": "events", "left_column": "account_id", "right_table": "accounts", "right_column": "account_id", "cardinality": "many_to_one"}]}],
        "value_mappings": [],
        "analysis_constraints": [{
            "id": "independent_metrics", "minimum_events": 80,
            "rank_contract": {
                "kind": "parallel_rankings", "policy": "ROW_NUMBER", "shared_population": True,
                "rankings": [
                    {"output_alias": "average_position", "metric_id": "mean_value", "direction": "desc", "partition_by": []},
                    {"output_alias": "total_position", "metric_id": "total_value", "direction": "desc", "partition_by": []},
                ],
                "tie_keys": ["accounts.account_id"], "population_entity_id": "account",
                "population_min_count": {"metric_id": "event_count", "threshold_key": "minimum_events"},
            },
        }],
    }


class QueryContractTests(unittest.TestCase):
    def setUp(self):
        self.schema = contract_schema()
        self.rewrite = resolved_rewrite()

    def test_contract_carries_explicit_metric_sources_grains_and_entities(self):
        contract = build_query_contract(self.rewrite, self.schema)
        self.assertEqual(contract["version"], 1)
        self.assertEqual(contract["database_schema"], "business")
        metric = contract["metrics"][0]
        self.assertEqual(metric["expression"], "AVG(amount)")
        self.assertEqual(metric["source"], {"schema": "business", "table": "events", "columns": ["events.amount"], "nullability": {"events.amount": "NO"}})
        self.assertEqual(metric["grain_keys"], ["events.event_id"])
        self.assertEqual(metric["grain_evidence"], "configured")
        self.assertEqual(metric["database_primary_key"], ["events.event_id"])
        self.assertTrue(contract["entities"][0]["require_output"])
        self.assertTrue(contract["entities"][0]["database_unique"])

    def test_missing_configured_grain_is_filled_from_actual_primary_key(self):
        self.rewrite["metrics"][0].pop("grain_keys")
        self.rewrite["dimensions"][0].pop("entity_keys")
        contract = build_query_contract(self.rewrite, self.schema)
        self.assertEqual(contract["metrics"][0]["grain_keys"], ["events.event_id"])
        self.assertEqual(contract["metrics"][0]["grain_evidence"], "database_primary_key")
        self.assertEqual(contract["entities"][0]["entity_keys"], ["accounts.account_id"])
        self.assertEqual(contract["entities"][0]["key_evidence"], "database_primary_key")

    def test_metric_visualization_semantics_are_explicit_and_unknown_is_not_additive(self):
        self.rewrite["metrics"][0].update(unit="rating", display_name="平均评分", additive=False)
        metrics = build_query_contract(self.rewrite, self.schema)["metrics"]
        self.assertEqual((metrics[0]["unit"], metrics[0]["display_name"], metrics[0]["additive"]),
                         ("rating", "平均评分", False))
        self.assertIsNone(metrics[1]["unit"])
        self.assertIsNone(metrics[1]["additive"])

    def test_real_fk_evidence_is_distinct_from_configuration(self):
        join = build_query_contract(self.rewrite, self.schema)["joins"][0]
        self.assertEqual(join["database_cardinality"], "many_to_one")
        self.assertEqual(join["evidence"]["kind"], "database_foreign_key")
        self.assertTrue(join["evidence"]["foreign_key_verified"])
        self.assertTrue(join["evidence"]["referenced_key_unique"])
        self.assertEqual(join["evidence"]["constraint_name"], "events_account_fk")

    def test_configured_join_without_metadata_is_not_claimed_as_observed_fk(self):
        self.schema[0].pop("foreign_keys")
        join = build_query_contract(self.rewrite, self.schema)["joins"][0]
        self.assertIsNone(join["database_cardinality"])
        self.assertEqual(join["configured_cardinality"], "many_to_one")
        self.assertFalse(join["evidence"]["foreign_key_verified"])
        self.assertEqual(join["evidence"]["kind"], "configured_relationship")

    def test_fk_in_other_schema_does_not_verify_this_join(self):
        self.schema[0]["foreign_keys"][0]["referenced_schema"] = "unrelated"
        join = build_query_contract(self.rewrite, self.schema)["joins"][0]
        self.assertFalse(join["evidence"]["foreign_key_verified"])

    def test_duplicate_path_edges_are_deduplicated(self):
        self.rewrite["join_paths"].append(copy.deepcopy(self.rewrite["join_paths"][0]))
        self.assertEqual(len(build_query_contract(self.rewrite, self.schema)["joins"]), 1)

    def test_parallel_ranks_are_generic_and_use_dynamic_count_threshold(self):
        analysis = build_query_contract(self.rewrite, self.schema)["analysis"][0]
        self.assertEqual(analysis["kind"], "parallel_rankings")
        self.assertEqual([rank["output_alias"] for rank in analysis["rankings"]], ["average_position", "total_position"])
        self.assertEqual(analysis["rankings"][0]["metric_output_alias"], "mean_value")
        self.assertEqual(analysis["population_entity_keys"], ["accounts.account_id"])
        self.assertEqual(analysis["population_min_count"], {"metric_id": "event_count", "output_alias": "event_count", "operator": "gte", "minimum": 80})
        self.rewrite["analysis_constraints"][0]["minimum_events"] = 20
        changed = build_query_contract(self.rewrite, self.schema)["analysis"][0]
        self.assertEqual(changed["population_min_count"]["minimum"], 20)

    def test_absent_threshold_does_not_invent_a_population_gate(self):
        self.rewrite["analysis_constraints"][0].pop("minimum_events")
        analysis = build_query_contract(self.rewrite, self.schema)["analysis"][0]
        self.assertNotIn("population_min_count", analysis)
        self.assertEqual(analysis["post_rank_filters"], [])

    def test_post_rank_filters_preserve_configured_aliases_directions_and_thresholds(self):
        conditions = [
            {"output_alias": "average_position", "operator": "lte", "maximum": 10},
            {"output_alias": "total_position", "operator": "gt", "maximum": 30},
        ]
        self.rewrite["analysis_constraints"][0]["resolved_rank_filters"] = conditions
        analysis = build_query_contract(self.rewrite, self.schema)["analysis"][0]
        self.assertEqual(analysis["post_rank_filters"], conditions)

    def test_post_rank_filter_cannot_reference_an_unmatched_rank(self):
        self.rewrite["analysis_constraints"][0]["resolved_rank_filters"] = [{"output_alias": "missing_rank", "operator": "lte", "maximum": 10}]
        with self.assertRaises(AppError) as caught:
            build_query_contract(self.rewrite, self.schema)
        self.assertEqual(caught.exception.code, 1026)

    def test_unstructured_analysis_remains_explicitly_description_only(self):
        self.rewrite["analysis_constraints"] = [{"id": "grouped_comparison", "description": "Rank within each requested group."}]
        self.assertEqual(build_query_contract(self.rewrite, self.schema)["analysis"], [{
            "id": "grouped_comparison", "kind": "described_analysis",
            "description": "Rank within each requested group.", "validation_scope": "description_only",
        }])

    def test_filter_only_entity_does_not_require_output(self):
        self.rewrite["dimensions"][0]["require_output"] = False
        self.assertFalse(build_query_contract(self.rewrite, self.schema)["entities"][0]["require_output"])

    def test_unmatched_rank_metric_is_rejected(self):
        self.rewrite["analysis_constraints"][0]["rank_contract"]["rankings"][0]["metric_id"] = "missing_metric"
        with self.assertRaises(AppError) as caught:
            build_query_contract(self.rewrite, self.schema)
        self.assertEqual(caught.exception.code, 1026)

    def test_missing_grain_field_is_rejected_without_fabricating_pk(self):
        self.rewrite["metrics"][0]["grain_keys"] = ["events.invented_id"]
        with self.assertRaises(AppError) as caught:
            build_query_contract(self.rewrite, self.schema)
        self.assertIn("events.invented_id", caught.exception.message)

    def test_builder_does_not_parse_question_or_modify_input(self):
        original = copy.deepcopy(self.rewrite)
        first = build_query_contract(self.rewrite, self.schema)
        self.assertEqual(self.rewrite, original)
        self.rewrite["original_question"] = "a completely different question with a domain-specific name"
        self.assertEqual(build_query_contract(self.rewrite, self.schema), first)

    def test_empty_semantics_still_return_a_well_formed_v1_contract(self):
        contract = build_query_contract({"database_schema": "business"}, self.schema)
        self.assertEqual(contract["version"], 1)
        self.assertEqual(contract["metrics"], [])
        self.assertEqual(contract["analysis"], [])
        self.assertEqual(contract["schema_metadata_versions"], {"events": 2, "accounts": 2})

    def test_schema_key_evidence_keeps_composite_keys_whole_and_visible(self):
        schema = [{
            "table_name": "ledger", "schema_metadata_version": 2,
            "columns": [{"name": name} for name in ["tenant_id", "entry_id", "code"]],
            "primary_key": ["tenant_id", "entry_id"],
            "unique_keys": [["tenant_id", "entry_id"], ["tenant_id", "code"], ["tenant_id", "hidden_column"]],
        }]
        evidence = build_query_contract({}, schema)["schema_keys"]["ledger"]
        self.assertEqual(evidence, {
            "primary_key": ["tenant_id", "entry_id"],
            "unique_keys": [["tenant_id", "entry_id"], ["tenant_id", "code"]],
            "metadata_verified": True,
        })
        self.assertNotIn(["tenant_id"], evidence["unique_keys"])
        schema[0]["primary_key"] = ["tenant_id", "hidden_column"]
        hidden_primary = build_query_contract({}, schema)["schema_keys"]["ledger"]
        self.assertEqual(hidden_primary["primary_key"], [])

    def test_configured_entity_key_never_becomes_verified_schema_unique_key(self):
        for table in self.schema:
            table.pop("schema_metadata_version")
            table.pop("primary_key")
            table.pop("unique_keys")
        contract = build_query_contract(self.rewrite, self.schema)
        self.assertEqual(contract["entities"][0]["entity_keys"], ["accounts.account_id"])
        self.assertEqual(contract["schema_keys"]["accounts"], {
            "primary_key": [], "unique_keys": [], "metadata_verified": False,
        })

    def test_metric_nullability_maps_only_expression_columns_from_schema(self):
        next(column for column in self.schema[0]["columns"] if column["name"] == "amount")["nullable"] = "YES"
        metric = build_query_contract(self.rewrite, self.schema)["metrics"][0]
        self.assertEqual(metric["source"]["nullability"], {"events.amount": "YES"})
        self.assertNotIn("events.account_id", metric["source"]["nullability"])

    def test_metric_nullability_does_not_guess_nonnull_from_primary_key(self):
        next(column for column in self.schema[0]["columns"] if column["name"] == "event_id").pop("nullable")
        metric = next(item for item in build_query_contract(self.rewrite, self.schema)["metrics"] if item["id"] == "event_count")
        self.assertEqual(metric["source"]["nullability"], {"events.event_id": "UNKNOWN"})


if __name__ == "__main__":
    unittest.main()
