import json
import tempfile
import unittest
from pathlib import Path

from agentdoctor.analysis import model_context
from agentdoctor.benchmark_collect import collect
from agentdoctor.core import DoctorError, digest, read_json, read_text, write_json
from agentdoctor.detectors import detect, diagnose
from agentdoctor.engine import run
from agentdoctor.project import snapshot


class LocatedSourceContextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.manifest, self.spans = {}, []
        self.config = {"tools": {"charge": {"input_schema": {"type": "object", "properties": {"quantity": {"type": "integer"}}}}},
                       "checks": {"loop_threshold": 3, "preserve_state_keys": []}, "source_paths": ["*.py"],
                       "policy": {"protected_paths": [], "max_context_chars": 200000}}
        self.function = "def run(quantity):\n    return charge(str(quantity))\n"
        self.content = "# padding text\n" * 960 + self.function
        self.content += "#" + "x" * (14882 - len(self.content) - 2) + "\n"

    def add_source(self, content, relative="worker.py", location=None):
        (self.project / relative).write_bytes(content.encode("utf-8"))
        self.manifest[relative] = digest(content)
        self.spans.append({"run_id": "job", "span_id": str(len(self.spans)), "kind": "tool", "node": "run",
                           "tool": "charge", "input": {"quantity": "3"},
                           "source": {"path": relative, **(location if location is not None else {"function": "run", "line": 961})}})

    def context(self, budget=200000, diagnosis=None):
        self.config["policy"]["max_context_chars"] = budget
        diagnosis = diagnosis or diagnose(detect(self.spans, self.config, self.project, self.manifest), self.spans)
        return model_context(self.project, self.manifest, self.spans, diagnosis, self.config, [])

    def assert_exact_excerpts(self, context, originals):
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), self.config["policy"]["max_context_chars"])
        for source in context["sources"]:
            lines = originals[source["path"]].splitlines(keepends=True)
            self.assertEqual(source["text"], "".join(lines[source["start_line"] - 1:source["end_line"]]))
            self.assertEqual(source["sha256"], self.manifest[source["path"]])

    def test_late_function_is_sent_with_full_file_when_budget_allows(self):
        self.add_source(self.content)
        context = self.context()
        self.assertEqual(context["findings"][0]["category"], "tool_argument_schema")
        self.assertEqual(len(self.content), 14882)
        self.assertEqual(self.content.splitlines()[960], "def run(quantity):")
        self.assertEqual(context["sources"][0]["text"], self.content)
        self.assertFalse(context["sources"][0]["truncated"])
        self.assert_exact_excerpts(context, {"worker.py": self.content})

    def test_tight_budget_preserves_whole_function_by_name_or_line(self):
        for location in ({"function": "run"}, {"function": "worker.run"}, {"line": 962}, {"function": "run", "line": 961}):
            self.spans.clear()
            self.add_source(self.content, location=location)
            with self.subTest(location=location):
                context = self.context(6000)
                self.assertEqual(context["sources"][0]["text"], self.function)
                self.assertTrue(context["sources"][0]["truncated"])
                self.assertEqual(context["sources"][0]["start_line"], 961)
                self.assert_exact_excerpts(context, {"worker.py": self.content})

    def test_all_located_files_survive_file_count_and_budget_trimming(self):
        for i in range(13):
            self.add_source("def run(quantity):\n    return charge(str(quantity))\n", f"worker{i:02}.py", {"function": "run"})
        context = self.context()
        self.assertEqual({s["path"] for s in context["sources"]}, set(self.manifest))
        self.assertEqual(context["omitted_sources"], 0)
        with self.assertRaisesRegex(DoctorError, "Essential model context"):
            self.context(5000)

    def test_multiple_locations_keep_distinct_exact_ranges_without_duplication(self):
        first = "def first(quantity):\n    return charge(str(quantity))\n"
        second = "def second(quantity):\n    return charge(str(quantity))\n"
        content = first + "# padding text\n" * 960 + second
        self.add_source(content, location={"function": "first"})
        self.add_source(content, location={"function": "second"})
        self.add_source(content, location={"function": "second", "line": 964})
        context = self.context(7000)
        self.assertEqual([s["text"] for s in context["sources"]], [first, second])
        self.assertEqual(context["omitted_sources"], 0)
        self.assert_exact_excerpts(context, {"worker.py": content})

    def test_qualified_async_method_keeps_decorator_unicode_and_crlf(self):
        target = "    @decorate(\r\n        '中文'\r\n    )\r\n    async def run(self, quantity):\r\n        return charge(str(quantity))\r\n"
        content = "class First:\r\n    def run(self):\r\n        return 'unrelated'\r\n" + "# padding text\r\n" * 960 + "class Second:\r\n" + target
        self.add_source(content, location={"function": "Second.run"})
        context = self.context(6000)
        self.assertEqual(context["sources"][0]["text"], target)
        self.assert_exact_excerpts(context, {"worker.py": content})

    def test_ambiguous_method_name_keeps_both_until_line_disambiguates(self):
        first = "    def run(self):\n        return 'first'\n"
        second = "    def run(self):\n        return 'second'\n"
        content = "class First:\n" + first + "# padding text\n" * 960 + "class Second:\n" + second
        self.add_source(content, location={"function": "run"})
        context = self.context(6000)
        self.assertEqual([s["text"] for s in context["sources"]], [first, second])
        self.spans[0]["source"]["line"] = 965
        context = self.context(6000)
        self.assertEqual([s["text"] for s in context["sources"]], [second])

    def test_unresolved_or_file_only_location_does_not_guess_a_prefix(self):
        for location in ({}, {"function": "missing"}, {"function": "Missing.run"}, {"line": 5000}):
            self.spans.clear()
            self.add_source(self.content, location=location)
            with self.subTest(location=location):
                self.assertEqual(self.context()["sources"][0]["text"], self.content)
                with self.assertRaisesRegex(DoctorError, "Essential model context"):
                    self.context(6000)

    def test_large_required_function_is_not_dropped_for_a_smaller_file(self):
        self.add_source("def run(quantity):\n    return '" + "x" * 14000 + "'\n", "z_large.py", {"function": "run"})
        self.add_source("def run(quantity):\n    return quantity\n", "a_small.py", {"function": "run"})
        with self.assertRaisesRegex(DoctorError, "Essential model context"):
            self.context(6000)

    def test_line_window_covers_late_syntax_errors_and_non_python_source(self):
        self.config["source_paths"].append("*.txt")
        for relative, tail in (("broken.py", "def broken(:\n"), ("prompt.txt", "Return exactly one object.\n")):
            self.spans.clear()
            self.manifest.clear()
            content = "# padding text\n" * 960 + tail
            self.add_source(content, relative, {"line": 961})
            with self.subTest(relative=relative):
                context = self.context(8000)
                self.assertIn(tail, context["sources"][0]["text"])
                self.assertTrue(context["sources"][0]["truncated"])
                self.assert_exact_excerpts(context, {relative: content})

    def test_evidence_and_model_hypothesis_sources_also_receive_priority(self):
        self.add_source(self.content)
        diagnosis = diagnose(detect(self.spans, self.config, self.project, self.manifest), self.spans)
        # Location is still present in cited evidence, even if an aggregate finding lost it.
        diagnosis["findings"][0]["source"] = {}
        diagnosis["hypotheses"][0]["source_path"] = None
        extra = "PROMPT = 'retain the response contract'\n"
        (self.project / "z_prompt.py").write_bytes(extra.encode())
        self.manifest["z_prompt.py"] = digest(extra)
        diagnosis["hypotheses"].append({"id": "h_model", "origin": "model", "source_path": "z_prompt.py",
                                       "finding_ids": [], "evidence": ["job/0"]})
        context = self.context(6500, diagnosis)
        self.assertEqual({s["path"] for s in context["sources"]}, {"worker.py", "z_prompt.py"})
        self.assertIn(self.function, [s["text"] for s in context["sources"]])


class LocatedSourceRepairTest(unittest.TestCase):
    def test_formal_repair_model_receives_late_function_and_verifies_patch(self):
        fixture = Path(__file__).resolve().parents[1] / "agentdoctor/benchmark_data/cases/argument_type"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "project"
            snapshot(fixture, project)
            content = "# padding text\n" * 960 + read_text(project / "subject.py")
            (project / "subject.py").write_bytes(content.encode())
            config = read_json(project / "doctor.json")
            trace = root / "trace.jsonl"
            collect(project, trace)
            owner = self

            class ContextDrivenModel:
                provider, name, calls, max_calls = "test", "fixture", 0, 5
                usage = []

                def complete(self, instruction, context, schema):
                    self.calls += 1
                    source = next((s["text"] for s in context["sources"] if s["path"] == "subject.py" and 'arguments = {"quantity": str(quantity)}' in s["text"]), "")
                    owner.assertTrue(source, "Located function is missing from model source context")
                    if "findings" in schema["properties"]:
                        return {"findings": [], "hypotheses": []}
                    before = next(line for line in source.splitlines(keepends=True) if 'arguments = {"quantity": str(quantity)}' in line)
                    return {"summary": "Pass an integer to price", "finding_ids": [context["findings"][0]["id"]],
                            "edits": [{"path": "subject.py", "before": before, "after": before.replace("str(quantity)", "quantity"),
                                       "reason": "The observed string violates the integer contract"}]}

            for budget in (200000, 12000):
                config["policy"]["max_context_chars"] = budget
                write_json(project / "doctor.json", config)
                with self.subTest(budget=budget):
                    report = run(project, trace, mode="repair", model=ContextDrivenModel(), executor="local", output=root / "runs")
                    self.assertEqual(report["status"], "verified", report)
                    self.assertEqual(report["model"]["calls"], 2)
                    self.assertEqual(report["attempts"][0]["origin"], "model")
                    self.assertEqual(read_text(project / "subject.py"), content)
