"""Replay actual member query candidates without a model or database connection."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.business_rewrite_service import rewrite_business_question
from app.services.business_sql_service import validate_business_sql
from app.services.schema_sql_validation import validate_schema_sql
from tests.test_recorded_business_candidates import demo_metadata


class RecordedMemberCandidatesTests(unittest.TestCase):
    def test_actual_multi_join_count_star_candidates_preserve_order_grain(self):
        records = json.loads((Path(__file__).parent / "fixtures/business_sql/recorded_member_candidates.json").read_text())
        schema = demo_metadata()
        for record in records:
            def observed(_, table, column):
                return record["observed_values"][f"{table}.{column}"]

            with patch("app.services.business_rewrite_service._observe_values", side_effect=observed), patch(
                "app.services.business_rewrite_service.execute_select_sql", side_effect=AssertionError("No database access")
            ):
                rewrite = rewrite_business_question(record["question"], schema, None)
            self.assertEqual(len(record["candidates"]), 3)
            for index, candidate in enumerate(record["candidates"]):
                with self.subTest(record=record["name"], candidate=index + 1):
                    validate_schema_sql(candidate["sql"], schema)
                    validate_business_sql(candidate["sql"], rewrite)


if __name__ == "__main__":
    unittest.main()
