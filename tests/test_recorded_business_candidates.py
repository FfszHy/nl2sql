"""Real generated SQL shapes, replayed locally against repository demo metadata."""
import json
import unittest
from pathlib import Path

from sqlglot import exp, parse_one

from app.core.errors import AppError
from app.models.contracts import DataSourceConfig
from app.services.business_rewrite_service import rewrite_business_question
from app.services.business_sql_service import validate_business_sql
from app.services.schema_sql_validation import validate_schema_sql
from scripts.create_nl2sql_postgres_mock_db import DDL_LIST


def demo_metadata():
    tables = []
    for ddl in DDL_LIST:
        definition = parse_one(ddl, read="postgres").this
        table = {"table_name": definition.this.name, "columns": [], "primary_key": [],
                 "unique_keys": [], "foreign_keys": [], "schema_metadata_version": 2}
        for item in definition.expressions:
            if isinstance(item, exp.ColumnDef):
                primary = bool(list(item.find_all(exp.PrimaryKeyColumnConstraint)))
                not_null = primary or bool(list(item.find_all(exp.NotNullColumnConstraint)))
                table["columns"].append({"name": item.name, "nullable": "NO" if not_null else "YES"})
                if primary:
                    table["primary_key"].append(item.name)
            elif isinstance(item, exp.Constraint):
                for foreign_key in item.find_all(exp.ForeignKey):
                    target = foreign_key.args["reference"].this
                    table["foreign_keys"].append({
                        "columns": [column.name for column in foreign_key.expressions],
                        "referenced_schema": "public", "referenced_table": target.this.name,
                        "referenced_columns": [column.name for column in target.expressions],
                        "constraint_name": item.name, "cardinality": "many_to_one", "nullable": False,
                    })
        table["unique_keys"] = [table["primary_key"]]
        tables.append(table)
    return tables


class RecordedBusinessCandidatesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = json.loads((Path(__file__).parent / "fixtures/business_sql/recorded_rank_candidates.json").read_text())
        cls.schema = demo_metadata()
        cls.source = DataSourceConfig(host="fixture.invalid", user="fixture", password="fixture-only", database="fixture")

    def test_actual_candidate_sequences_reject_fanout_accept_proven_wrappers(self):
        for record in self.records:
            rewrite = rewrite_business_question(record["question"], self.schema, self.source)
            for index, candidate in enumerate(record["candidates"]):
                with self.subTest(record=record["name"], candidate=index + 1):
                    validate_schema_sql(candidate["sql"], self.schema)
                    if candidate["expect_business_accept"]:
                        validate_business_sql(candidate["sql"], rewrite)
                    else:
                        with self.assertRaises(AppError) as error:
                            validate_business_sql(candidate["sql"], rewrite)
                        self.assertEqual(error.exception.code, 1027)

    def test_actual_correct_candidates_also_accept_changed_population_threshold(self):
        for record in self.records:
            question = record["question"].replace("至少有20条", "至少有80条")
            rewrite = rewrite_business_question(question, self.schema, self.source)
            for candidate in record["candidates"][1:]:
                parsed = parse_one(candidate["sql"], read="postgres")
                changed = 0
                for comparison in parsed.find_all(exp.GTE):
                    if isinstance(comparison.this, exp.Count) and comparison.expression == exp.Literal.number(20):
                        comparison.set("expression", exp.Literal.number(80))
                        changed += 1
                self.assertEqual(changed, 1)
                with self.subTest(record=record["name"], sql=parsed.sql(dialect="postgres")):
                    validate_business_sql(parsed.sql(dialect="postgres"), rewrite)


if __name__ == "__main__":
    unittest.main()
