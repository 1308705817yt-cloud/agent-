import copy
import tempfile
import unittest
from pathlib import Path

from agentdoctor.contracts import CONFIG_SCHEMA, TRACE_SCHEMA, violations
from agentdoctor.core import read_json
from agentdoctor.export_schemas import main as export_schemas

ROOT = Path(__file__).resolve().parents[1]


class PublicSchemaTest(unittest.TestCase):
    def test_generator_check_detects_stale_or_missing_files_without_overwriting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "doctor.schema.json"
            self.assertEqual(export_schemas(["--output", folder]), 0)
            self.assertEqual(export_schemas(["--output", folder, "--check"]), 0)
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(export_schemas(["--output", folder, "--check"]), 1)
            self.assertEqual(path.read_text(encoding="utf-8"), "{}")
            path.unlink()
            self.assertEqual(export_schemas(["--output", folder, "--check"]), 1)
            self.assertFalse(path.exists())

    def test_published_schemas_match_runtime_definitions(self):
        for filename, runtime in (("doctor.schema.json", CONFIG_SCHEMA), ("trace.schema.json", TRACE_SCHEMA)):
            with self.subTest(filename=filename):
                public = read_json(ROOT / "schemas" / filename)
                self.assertEqual(public.pop("$schema"), "https://json-schema.org/draft/2020-12/schema")
                self.assertEqual(public, runtime, "Regenerate public schemas from agentdoctor.contracts")

    def test_all_bundled_configs_pass_both_public_and_runtime_schema(self):
        public = read_json(ROOT / "schemas/doctor.schema.json")
        paths = [ROOT / "agentdoctor/demo_project/doctor.json", *(ROOT / "agentdoctor/benchmark_data/cases").glob("*/doctor.json")]
        for path in paths:
            with self.subTest(path=path):
                config = read_json(path)
                self.assertEqual(violations(config, CONFIG_SCHEMA), [])
                self.assertEqual(violations(config, public), [])

    def test_expected_cases_constraints_match(self):
        public = read_json(ROOT / "schemas/doctor.schema.json")
        config = read_json(ROOT / "agentdoctor/demo_project/doctor.json")
        for cases in ([], ["same", "same"], [""], [1], "a-string"):
            with self.subTest(cases=cases):
                value = copy.deepcopy(config)
                value["validation"]["commands"][0]["expected_cases"] = cases
                self.assertTrue(violations(value, CONFIG_SCHEMA))
                self.assertEqual(violations(value, public), violations(value, CONFIG_SCHEMA))
