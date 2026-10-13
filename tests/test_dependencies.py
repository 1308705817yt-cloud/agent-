import json
import tempfile
import unittest
from pathlib import Path

from agentdoctor.analysis import model_context
from agentdoctor.core import DoctorError, digest
from agentdoctor.dependencies import dependency_locations
from agentdoctor.detectors import diagnose, finding


class DependencyContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = {}
        self.config = {"tools": {}, "checks": {}, "source_paths": ["*.py", "pkg/**"],
                       "policy": {"protected_paths": ["evaluator.py"], "max_context_chars": 12000}}

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode())
        self.manifest[path] = digest(text)

    def context(self):
        spans = [{"run_id": "r", "span_id": "s", "kind": "node", "node": "run", "source": {"path": "subject.py", "function": "run"}}]
        diagnosis = diagnose([finding("other", "high", "incorrect output", spans)], spans)
        return model_context(self.root, self.manifest, spans, diagnosis, self.config, [])

    def test_multifile_constants_helpers_aliases_and_reexports_survive_budget(self):
        padding = "# irrelevant filler\n" * 1500
        self.write("subject.py", "from pkg import helpers as h\n" + padding + "def run(value):\n    return h.scale(value)\n")
        self.write("pkg/__init__.py", "")
        self.write("pkg/helpers.py", "from .exports import RATE as factor\n" + padding + "def scale(value):\n    return normalize(value) * factor\n\ndef normalize(value):\n    return int(value)\n")
        self.write("pkg/exports.py", "from .settings import RATE\n")
        self.write("pkg/settings.py", padding + "RATE = 7\n")
        context = self.context()
        source = "\n".join(s["text"] for s in context["sources"])
        for text in ("def scale", "def normalize", "RATE = 7", "from .exports import RATE as factor"):
            self.assertIn(text, source)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 12000)
        self.assertTrue(any(e["path"] == "pkg/settings.py" for e in context["dependency_context"]["resolved"]))

    def test_dependency_cycles_terminate(self):
        self.write("subject.py", "import other\ndef run(value):\n    return other.call(value)\n")
        self.write("other.py", "import subject\ndef call(value):\n    return subject.run(value)\n")
        context = self.context()
        self.assertLess(context["dependency_context"]["definitions_included"], 10)
        self.assertEqual({s["path"] for s in context["sources"]}, {"subject.py", "other.py"})

    def test_local_parameter_does_not_resolve_to_unrelated_global(self):
        self.write("subject.py", "def helper():\n    return 'unrelated'\ndef run(helper):\n    return helper()\n")
        expanded, report = dependency_locations(self.root, self.manifest, {"subject.py": [{"function": "run"}]}, self.config)
        self.assertFalse(any(e["reference"] == "helper" for e in report["resolved"]))
        self.assertTrue(any(e["reference"] == "helper" and e["reason"] == "runtime_binding" for e in report["unresolved"]))

    def test_protected_imports_are_reported_without_source_disclosure(self):
        self.write("subject.py", "from evaluator import expected\ndef run(value):\n    return expected(value)\n")
        self.write("evaluator.py", "PRIVATE_EVALUATION_VALUE = 'hidden'\ndef expected(value):\n    return PRIVATE_EVALUATION_VALUE\n")
        context = self.context()
        self.assertNotIn("PRIVATE_EVALUATION_VALUE", json.dumps(context))
        self.assertTrue(any(e["reason"] == "outside_allowed_sources" for e in context["dependency_context"]["unresolved"]))

    def test_static_discovery_never_imports_or_executes_target(self):
        self.write("subject.py", "from helper import scale\ndef run(value):\n    return scale(value)\n")
        self.write("helper.py", "raise RuntimeError('must never execute')\nFACTOR = 3\ndef scale(value):\n    return value * FACTOR\n")
        context = self.context()
        self.assertIn("FACTOR = 3", "\n".join(s["text"] for s in context["sources"]))

    def test_limits_and_dynamic_imports_are_explicit(self):
        self.write("subject.py", "import importlib\nfrom helper import scale\ndef run(value):\n    return scale(value) + getattr(importlib.import_module('plugin'), 'value')\n")
        self.write("helper.py", "def scale(value):\n    return value\n")
        _, report = dependency_locations(self.root, self.manifest, {"subject.py": [{"function": "run"}]}, self.config, max_files=1)
        self.assertEqual(report["status"], "partial_static_subset")
        self.assertTrue(any(e["reason"] == "file_limit" for e in report["unresolved"]))
        self.assertTrue(any(e["reference"] == "getattr" for e in report["unresolved"]))

    def test_required_dependency_cannot_be_dropped_to_fit_budget(self):
        self.write("subject.py", "from helper import PAYLOAD\ndef run(value):\n    return PAYLOAD\n")
        self.write("helper.py", "PAYLOAD = '" + "x" * 13000 + "'\n")
        with self.assertRaisesRegex(DoctorError, "Essential model context"):
            self.context()
