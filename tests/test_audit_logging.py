import io
import json
import logging
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch

from app.core import logging as audit


class AuditLoggingTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("app.audit.query")
        self.original_handlers = self.logger.handlers[:]
        self.original_level, self.original_propagate = self.logger.level, self.logger.propagate
        self.logger.handlers = []
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "query-audit.jsonl"

    def tearDown(self):
        for handler in self.logger.handlers:
            handler.close()
        self.logger.handlers = self.original_handlers
        self.logger.setLevel(self.original_level)
        self.logger.propagate = self.original_propagate

    def configured(self, stack, path=None, size=4096):
        stream = io.StringIO()
        stack.enter_context(patch.object(audit.settings, "query_audit_path", str(self.path) if path is None else path))
        stack.enter_context(patch.object(audit.settings, "query_audit_max_bytes", size))
        stack.enter_context(patch.object(audit.settings, "query_audit_backup_count", 3))
        stack.enter_context(patch("sys.stderr", stream))
        return audit.get_logger("app.audit.query"), stream

    def test_jsonl_and_console_keep_diagnostics_but_exclude_rows_and_credentials(self):
        with ExitStack() as stack:
            logger, stream = self.configured(stack)
            audit.audit_log(
                logger, "database_query_failed", trace_id="trace-replay", sql="SELECT city_name FROM cities",
                error_code=1003, error_type="db_error", phase="preflight", sqlstate="42703",
                error_message="failed password='test-password' Bearer test-token postgres://reader:test-password@host/db",
                rows=[["private-result-value"]], rows_preview=[["private-preview"]],
                password="top-level-password", datasource={"password": "datasource-password"},
                safety_checks={"is_select_only": True, "password_cipher": "nested-cipher"},
            )
            self.assertIs(audit.get_logger("app.audit.query"), logger)
            self.assertEqual(len(logger.handlers), 2)
            self.assertIn("database_query_failed", stream.getvalue())
        raw = self.path.read_text()
        payload = json.loads(raw)
        self.assertEqual(payload["trace_id"], "trace-replay")
        self.assertEqual(payload["sqlstate"], "42703")
        self.assertEqual(payload["phase"], "preflight")
        self.assertEqual(payload["sql"], "SELECT city_name FROM cities")
        datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
        self.assertTrue(payload["timestamp"].endswith("Z"))
        for secret in ("private-result-value", "private-preview", "top-level-password", "datasource-password", "nested-cipher", "test-password", "test-token"):
            self.assertNotIn(secret, raw)
        self.assertNotIn("rows", payload)
        self.assertNotIn("datasource", payload)
        self.assertIn("[REDACTED]", payload["error_message"])

    def test_rotation_bounds_file_count_and_record_size(self):
        with ExitStack() as stack:
            logger, _ = self.configured(stack)
            for index in range(80):
                audit.audit_log(logger, "sql_validation_failed", trace_id=f"trace-{index}", sql="SELECT " + "x" * 300)
            audit.audit_log(logger, "oversized", trace_id="large", sql="汉" * 100000)
            files = list(self.path.parent.glob("query-audit.jsonl*"))
            self.assertGreater(len(files), 1)
            self.assertLessEqual(len(files), 4)
            self.assertTrue(all(path.stat().st_size <= 4096 for path in files))
            records = [json.loads(line) for path in files for line in path.read_text().splitlines()]
        oversized = next(record for record in records if record["event"] == "oversized")
        self.assertIn("sql", oversized["truncated_fields"])
        self.assertEqual(oversized["trace_id"], "large")

    def test_chart_decision_and_business_failure_keep_reason_without_result_values(self):
        with ExitStack() as stack:
            logger, _ = self.configured(stack)
            audit.audit_log(logger, "chart_selected", trace_id="chart-trace", chart_type="line",
                            chart_intent="trend", chart_reason={"code": "time_series", "message": "时间趋势"},
                            rows=[["private-result"]], chart_profile={"min": "private-statistic"})
            audit.audit_log(logger, "business_validation_failed", trace_id="count-trace",
                            business_failure_reason="count_source_ambiguity",
                            business_failure_details={"output_alias": "order_count", "rows": [["private-result"]]})
        records = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(records[0]["chart_type"], "line")
        self.assertEqual(records[0]["chart_reason"]["code"], "time_series")
        self.assertEqual(records[1]["business_failure_reason"], "count_source_ambiguity")
        self.assertEqual(records[1]["business_failure_details"], {"output_alias": "order_count"})
        self.assertNotIn("private-", self.path.read_text())

    def test_http_events_keep_endpoint_and_status(self):
        with ExitStack() as stack:
            logger, _ = self.configured(stack)
            for event in ("http_request", "app_error"):
                audit.audit_log(
                    logger, event, trace_id="http-trace", method="POST", path="/api/query",
                    status_code=422, rows=[["private-result"]], credentials={"password": "secret"},
                )
        records = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([record["event"] for record in records], ["http_request", "app_error"])
        for record in records:
            self.assertEqual(record["method"], "POST")
            self.assertEqual(record["path"], "/api/query")
            self.assertEqual(record["status_code"], 422)
            self.assertNotIn("rows", record)
            self.assertNotIn("credentials", record)

    def test_empty_path_disables_file_logging(self):
        with ExitStack() as stack:
            logger, _ = self.configured(stack, path="")
            audit.audit_log(logger, "console_only")
            self.assertFalse(any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers))
        self.assertFalse(self.path.exists())

    def test_file_initialization_failure_falls_back_to_console(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(audit, "RotatingFileHandler", side_effect=OSError("storage unavailable")))
            logger, stream = self.configured(stack)
            audit.audit_log(logger, "query_succeeded", trace_id="still-works")
            self.assertEqual(len(logger.handlers), 1)
            self.assertIn("audit_file_unavailable", stream.getvalue())
            self.assertIn("query_succeeded", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
