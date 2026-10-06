import json
import unittest
from unittest.mock import MagicMock, patch

from app.models.contracts import DataSourceConfig
from app.core.errors import AppError
from app.services import schema_cache_service, schema_service


def table(columns):
    return {
        "columns": [{"name": name, "nullable": nullable} for name, nullable in columns],
        "primary_key": [], "unique_keys": [], "foreign_keys": [],
    }


class SchemaRelationshipTests(unittest.TestCase):
    def test_complete_composite_foreign_key_is_preserved_in_order(self):
        grouped = {
            "transactions": table([("id", "NO"), ("region", "NO"), ("account", "NO")]),
            "accounts": table([("region", "NO"), ("account", "NO")]),
        }
        schema_service._attach_relationships(grouped, [
            ("transactions", "p", ["id"], None, None, [], "transactions_pkey"),
            ("accounts", "p", ["region", "account"], None, None, [], "accounts_pkey"),
            ("transactions", "f", ["region", "account"], "public", "accounts", ["region", "account"], "account_fk"),
        ], "public")
        relation = grouped["transactions"]["foreign_keys"][0]
        self.assertEqual(relation["columns"], ["region", "account"])
        self.assertEqual(relation["referenced_columns"], ["region", "account"])
        self.assertEqual(relation["cardinality"], "many_to_one")
        self.assertFalse(relation["nullable"])

    def test_unique_source_key_and_nullable_reference_are_recorded_separately(self):
        grouped = {"profiles": table([("user_id", "YES")]), "users": table([("id", "NO")])}
        schema_service._attach_relationships(grouped, [
            ("profiles", "f", ["user_id"], "public", "users", ["id"], "user_fk"),
            ("profiles", "u", ["user_id"], None, None, [], "profiles_user_key"),
            ("users", "p", ["id"], None, None, [], "users_pkey"),
        ], "public")
        relation = grouped["profiles"]["foreign_keys"][0]
        self.assertEqual(relation["cardinality"], "one_to_one")
        self.assertTrue(relation["nullable"])

    def test_hidden_columns_hidden_tables_and_other_schemas_do_not_enter_context(self):
        grouped = {"events": table([("user_id", "NO")]), "users": table([("id", "NO")])}
        schema_service._attach_relationships(grouped, [
            ("events", "u", ["user_id", "hidden"], None, None, [], "partial_unique"),
            ("events", "f", ["user_id"], "private", "users", ["id"], "private_fk"),
            ("events", "f", ["user_id"], "public", "hidden_table", ["id"], "hidden_fk"),
            ("events", "f", ["user_id"], "public", "users", ["hidden"], "hidden_column_fk"),
        ], "public")
        self.assertEqual(grouped["events"]["unique_keys"], [])
        self.assertEqual(grouped["events"]["foreign_keys"], [])

    def test_fetch_schema_is_readonly_and_binds_schema_and_table_names(self):
        source = DataSourceConfig(host="localhost", user="reader", password="test-only", database="sample", allowed_tables=["sales", "accounts"])
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.side_effect = [
            [("sales", "", "id", "integer", "NO", ""), ("sales", "", "account_id", "integer", "NO", ""), ("accounts", "", "id", "integer", "NO", "")],
            [("sales", "p", ["id"], None, None, [], "sales_pkey"), ("accounts", "p", ["id"], None, None, [], "accounts_pkey"), ("sales", "f", ["account_id"], "public", "accounts", ["id"], "sales_account_fk")],
        ]
        with patch.object(schema_service, "_connect", return_value=connection), patch.object(schema_service.settings, "pg_schema", "public"):
            schema = schema_service.fetch_schema(source)
        self.assertTrue(connection.read_only)
        connection.close.assert_called_once()
        calls = cursor.execute.call_args_list
        self.assertEqual(calls[0].args[0], "SET LOCAL statement_timeout = '30s'")
        self.assertEqual(calls[2].args[1], ["public", "sales", "accounts"])
        self.assertEqual(calls[3].args[1], ["public"])
        sales = next(item for item in schema if item["table_name"] == "sales")
        self.assertEqual(sales["primary_key"], ["id"])
        self.assertEqual(sales["schema_metadata_version"], 2)
        context = schema_service.build_schema_context(schema)
        self.assertIn("sales.account_id = accounts.id", context)
        self.assertIn("基数=many_to_one", context)

    def test_connection_closes_when_relationship_metadata_read_fails(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.side_effect = [[], RuntimeError("metadata unavailable")]
        source = DataSourceConfig(host="localhost", user="reader", password="test-only", database="sample")
        with patch.object(schema_service, "_connect", return_value=connection), self.assertRaises(AppError) as error:
            schema_service.fetch_schema(source)
        self.assertEqual(error.exception.code, 1009)
        self.assertIn("约束读取失败", error.exception.message)
        self.assertIsInstance(error.exception.__cause__, RuntimeError)
        connection.close.assert_called_once()

    def test_warm_legacy_cache_refreshes_relationships(self):
        old_schema = [{"table_name": "sales", "columns": []}]
        fresh = [{**old_schema[0], "schema_metadata_version": 2, "primary_key": ["id"]}]
        row = {"expires_at": 10**12, "schema_json": json.dumps(old_schema)}
        with patch("app.services.datasource_registry.get_datasource_config", return_value="source"), patch.object(schema_cache_service, "_cache_row", return_value=row), patch.object(schema_cache_service, "fetch_schema", return_value=fresh) as fetch, patch.object(schema_cache_service, "_save_cache") as save:
            source, schema = schema_cache_service.fetch_schema_with_cache("source-id")
        self.assertEqual(schema, fresh)
        fetch.assert_called_once_with("source")
        save.assert_called_once_with("source-id", fresh)

    def test_current_cache_does_not_refetch(self):
        schema = [{"table_name": "sales", "columns": [], "schema_metadata_version": 2}]
        row = {"expires_at": 10**12, "schema_json": json.dumps(schema)}
        with patch("app.services.datasource_registry.get_datasource_config", return_value="source"), patch.object(schema_cache_service, "_cache_row", return_value=row), patch.object(schema_cache_service, "fetch_schema") as fetch:
            _, actual = schema_cache_service.fetch_schema_with_cache("source-id")
        self.assertEqual(actual, schema)
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
