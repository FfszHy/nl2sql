import copy
import json
import unittest
from unittest.mock import MagicMock, patch

from app.core.errors import AppError
from app.services.chart_service import parse_chart_config, validate_chart_config
from app.services import llm_service


class ChartConfigTests(unittest.TestCase):
    def setUp(self):
        self.columns = ["city", "cinema", "revenue", "share"]
        self.config = {
            "version": 1,
            "type": "bar",
            "category": ["city", "cinema"],
            "series": [{"field": "revenue"}, {"field": "share", "axis": "secondary"}],
        }

    def assertInvalid(self, config, columns=None):
        with self.assertRaises(AppError) as caught:
            validate_chart_config(config, self.columns if columns is None else columns)
        self.assertEqual(caught.exception.code, 1014)
        self.assertEqual(caught.exception.error_type, "chart_validation_error")

    def test_chart_defaults_and_explicit_fields_are_normalized(self):
        parsed = parse_chart_config(json.dumps(self.config), self.columns)
        self.assertEqual(
            parsed,
            {
                "version": 1,
                "type": "bar",
                "category": ["city", "cinema"],
                "series": [
                    {"field": "revenue", "name": "revenue", "axis": "primary"},
                    {"field": "share", "name": "share", "axis": "secondary"},
                ],
                "orientation": "vertical",
            },
        )
        explicit = copy.deepcopy(self.config)
        explicit.update(title="各城市影院收入", orientation="horizontal")
        explicit["series"][0]["name"] = "收入"
        self.assertEqual(validate_chart_config(explicit, self.columns)["title"], "各城市影院收入")
        self.assertEqual(validate_chart_config(explicit, self.columns)["orientation"], "horizontal")

    def test_line_pie_and_scatter_use_only_supported_dimensions(self):
        line = copy.deepcopy(self.config)
        line["type"] = "line"
        self.assertNotIn("orientation", validate_chart_config(line, self.columns))
        for chart_type in ("pie", "scatter"):
            config = {
                "version": 1,
                "type": chart_type,
                "category": ["revenue" if chart_type == "scatter" else "city"],
                "series": [{"field": "share"}],
            }
            with self.subTest(chart_type=chart_type):
                self.assertEqual(validate_chart_config(config, self.columns)["type"], chart_type)
                for key, value in (
                    ("category", ["city", "cinema"]),
                    ("series", [{"field": "revenue"}, {"field": "share"}]),
                    ("series", [{"field": "share", "axis": "secondary"}]),
                    ("orientation", "vertical"),
                ):
                    invalid = copy.deepcopy(config)
                    invalid[key] = value
                    self.assertInvalid(invalid)

    def test_unknown_echarts_or_executable_fields_are_rejected(self):
        for key in ("formatter", "graphic", "dataset", "url", "__proto__", "option", "rows"):
            with self.subTest(key=key):
                invalid = copy.deepcopy(self.config)
                invalid[key] = "(()=>fetch('/leak'))()"
                self.assertInvalid(invalid)
                invalid = copy.deepcopy(self.config)
                invalid["series"][0][key] = {"formatter": "alert(1)"}
                self.assertInvalid(invalid)

    def test_wrong_types_lengths_and_values_are_rejected_without_coercion(self):
        for key, value in (
            ("version", True), ("version", 1.0), ("version", "1"), ("version", 2),
            ("type", "custom"), ("type", []),
            ("category", []), ("category", ["city"] * 2), ("category", ["city"] * 4),
            ("category", [["city"]]), ("category", "city"),
            ("series", []), ("series", [{"field": "revenue"}] * 9), ("series", [None]),
            ("title", 1), ("title", "标" * 201),
            ("orientation", "diagonal"), ("orientation", True),
        ):
            with self.subTest(key=key, value=value):
                invalid = copy.deepcopy(self.config)
                invalid[key] = value
                self.assertInvalid(invalid)
        for key, value in (("field", None), ("name", []), ("name", "a" * 101), ("axis", "custom")):
            with self.subTest(series_key=key):
                invalid = copy.deepcopy(self.config)
                invalid["series"][0][key] = value
                self.assertInvalid(invalid)
        for root in (None, [], "{}", True):
            self.assertInvalid(root)
        invalid = copy.deepcopy(self.config)
        invalid["type"] = "line"
        invalid["orientation"] = "vertical"
        self.assertInvalid(invalid)

    def test_only_existing_unambiguous_result_columns_can_be_bound(self):
        for key, value in (("category", ["missing"]), ("category", ["City"]), ("series", [{"field": "missing"}])):
            invalid = copy.deepcopy(self.config)
            invalid[key] = value
            self.assertInvalid(invalid)
        self.assertInvalid(self.config, self.columns + ["city"])
        self.assertInvalid(self.config, self.columns + ["revenue"])
        self.assertEqual(validate_chart_config(self.config, self.columns + ["unused", "unused"])["type"], "bar")

    def test_plain_json_and_json_fences_are_accepted(self):
        raw = json.dumps(self.config, ensure_ascii=False)
        for content in (raw, f"```json\n{raw}\n```", f"```\n{raw}\n```"):
            with self.subTest(content=content[:15]):
                self.assertEqual(parse_chart_config(content, self.columns)["type"], "bar")

    def test_valid_unicode_limits_and_unpaired_surrogates(self):
        config = copy.deepcopy(self.config)
        config["title"] = "🎬" * 200
        config["series"][0]["name"] = "🎬" * 100
        normalized = parse_chart_config(json.dumps(config), self.columns)
        self.assertEqual(normalized["title"], config["title"])
        json.dumps(normalized, ensure_ascii=False).encode("utf-8")
        for key, surrogate in (("title", "\ud800"), ("name", "\udfff")):
            with self.subTest(key=key):
                invalid = copy.deepcopy(self.config)
                if key == "name":
                    invalid["series"][0][key] = surrogate
                else:
                    invalid[key] = surrogate
                with self.assertRaises(AppError):
                    parse_chart_config(json.dumps(invalid), self.columns)

    def test_javascript_invalid_json_duplicate_keys_and_oversize_are_rejected(self):
        raw = json.dumps(self.config)
        invalid_inputs = [
            "", "null", "[]", "{unquoted: true}",
            "(()=>{globalThis.chartAttackExecuted=true;return {series:[]}})()",
            "({version:1, series:[{data:rows.map(r=>r[0])}]})",
            f"```javascript\n{raw}\n```",
            raw.replace('"version": 1', '"version": 1, "version": 1'),
            raw.replace('"version": 1', '"version": NaN'),
            "[" * 3000 + "]" * 3000,
            raw + " " * 16_000,
        ]
        for content in invalid_inputs:
            with self.subTest(content=content[:30]):
                with self.assertRaises(AppError) as caught:
                    parse_chart_config(content, self.columns)
                self.assertEqual(caught.exception.code, 1014)
                if content:
                    self.assertNotIn(content[:30], caught.exception.message)

    def test_non_object_provider_envelopes_become_safe_app_errors(self):
        for envelope in ([], None, "bad", True):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(envelope).encode("utf-8")
            with self.subTest(envelope=envelope), patch.object(llm_service.urllib.request, "urlopen", return_value=response):
                with self.assertRaises(AppError) as caught:
                    llm_service._call_generation(messages=[{"role": "user", "content": "fixture"}])
                self.assertEqual(caught.exception.code, 1007)
                self.assertEqual(caught.exception.error_type, "llm_error")


if __name__ == "__main__":
    unittest.main()
