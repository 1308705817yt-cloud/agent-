import copy
import unittest

from agentdoctor.citations import checked_source
from agentdoctor.contracts import validate
from agentdoctor.core import DoctorError, digest
from agentdoctor.model import analysis_schema
from agentdoctor.source_context import source_variants


class SourceCitationTest(unittest.TestCase):
    def setUp(self):
        self.text = "def calculate(value):\r\n    total = value + 3\r\n    return total\r\n"
        self.manifest = {"calc.py": digest(self.text)}
        self.sources = source_variants("calc.py", self.manifest["calc.py"], self.text, [])[0]

    def quote(self, text):
        return {"source_id": self.sources[0]["source_id"], "text": text}

    def test_line_endings_and_optional_final_ending_preserve_canonical_bytes(self):
        for ending in ("\n", "\r\n", "\r"):
            for final in ("", ending):
                quoted = "    total = value + 3" + ending + "    return total" + final
                with self.subTest(ending=repr(ending), final=repr(final)):
                    canonical, audit = checked_source(self.quote(quoted), self.manifest, self.sources)
                    self.assertEqual((canonical["start_line"], canonical["end_line"]), (2, 3))
                    self.assertEqual(canonical["text"], "    total = value + 3\r\n    return total\r\n")
                    self.assertEqual(audit["submitted_source"]["text"], quoted)

    def test_no_newline_at_eof_also_resolves(self):
        text = self.text.rstrip("\r\n")
        manifest = {"calc.py": digest(text)}
        sources = source_variants("calc.py", manifest["calc.py"], text, [])[0]
        canonical, _ = checked_source({"source_id": sources[0]["source_id"], "text": "    return total\n"}, manifest, sources)
        self.assertEqual(canonical["text"], "    return total")

    def test_whitespace_code_and_blank_line_changes_remain_invalid(self):
        for text in ("return total", "\treturn total", "    return total ", "    return totals", "    return total\n\n", "\n", "    total = value + 3\n\n    return total"):
            with self.subTest(text=text), self.assertRaises(DoctorError):
                checked_source(self.quote(text), self.manifest, self.sources)

    def test_unknown_stale_and_unseen_source_ids_are_rejected(self):
        for variant in ("unknown", "missing", "stale_hash", "different_range"):
            sources = copy.deepcopy(self.sources)
            quote = self.quote("    return total")
            if variant == "unknown":
                quote["source_id"] = "src_invented"
            elif variant == "missing":
                sources = []
            elif variant == "stale_hash":
                sources[0]["sha256"] = "old"
            else:
                sources[0]["start_line"] += 1
            with self.subTest(variant=variant), self.assertRaises(DoctorError):
                checked_source(quote, self.manifest, sources)

    def test_same_text_in_another_file_is_not_a_citation(self):
        wrong = source_variants("elsewhere.py", digest(self.text), self.text, [])[0]
        with self.assertRaises(DoctorError):
            checked_source(self.quote("    return total"), {**self.manifest, "elsewhere.py": digest(self.text)}, wrong)

    def test_repeated_quote_needs_more_context(self):
        text = "def first():\n    return 7\n\ndef second():\n    return 7\n"
        manifest = {"calc.py": digest(text)}
        sources = source_variants("calc.py", manifest["calc.py"], text, [])[0]
        quote = {"source_id": sources[0]["source_id"], "text": "    return 7"}
        with self.assertRaisesRegex(DoctorError, "unique"):
            checked_source(quote, manifest, sources)
        quote["text"] = "def second():\n    return 7"
        canonical, _ = checked_source(quote, manifest, sources)
        self.assertEqual((canonical["start_line"], canonical["end_line"]), (4, 5))

    def test_cannot_join_source_fragments_across_a_gap(self):
        text = "first = 1\nsecret_middle = 2\nlast = 3\n"
        manifest = {"calc.py": digest(text)}
        _, sources = source_variants("calc.py", manifest["calc.py"], text,
                                     [{"start_line": 1, "end_line": 1}, {"start_line": 3, "end_line": 3}])
        with self.assertRaises(DoctorError):
            checked_source({"source_id": sources[0]["source_id"], "text": "first = 1\nlast = 3"}, manifest, sources)

    def test_legacy_lines_are_not_silently_relocated(self):
        quote = {"path": "calc.py", "start_line": 2, "end_line": 3, "text": "    total = value + 3\n    return total"}
        source, audit = checked_source(quote, self.manifest, self.sources)
        self.assertEqual(audit["protocol"], "legacy_range_quote")
        self.assertEqual(source["start_line"], 2)
        quote.update(start_line=1, end_line=2)
        with self.assertRaises(DoctorError):
            checked_source(quote, self.manifest, self.sources)

    def test_unicode_separators_and_literal_backslash_n_are_not_newlines(self):
        for literal in ("line\u2028inside", r"line\ninside"):
            text = 'value = "' + literal + '"\n'
            manifest = {"calc.py": digest(text)}
            sources = source_variants("calc.py", manifest["calc.py"], text, [])[0]
            canonical, _ = checked_source({"source_id": sources[0]["source_id"], "text": text[:-1]}, manifest, sources)
            self.assertEqual(canonical["end_line"], 1)
            with self.assertRaises(DoctorError):
                checked_source({"source_id": sources[0]["source_id"], "text": text.replace(literal, "line\ninside")}, manifest, sources)

    def test_online_schema_requires_supplied_ids_and_explicit_classification(self):
        context = {"spans": [{"evidence_id": "r/s"}], "findings": [{"id": "f_1"}], "sources": self.sources}
        schema = analysis_schema(context)
        cause = {"summary": "cause", "evidence": ["r/s"], "source": self.quote("    return total")}
        result = {"findings": [], "hypotheses": [], "attributions": [{"finding_id": "f_1", "root_cause": cause, "classification": None}]}
        validate(result, schema, "response")
        for variant in ("id", "legacy", "classification"):
            bad = copy.deepcopy(result)
            if variant == "id":
                bad["attributions"][0]["root_cause"]["source"]["source_id"] = "src_absent"
            elif variant == "legacy":
                bad["attributions"][0]["root_cause"]["source"] = {"path": "calc.py", "start_line": 3, "end_line": 3, "text": "    return total"}
            else:
                del bad["attributions"][0]["classification"]
            with self.subTest(variant=variant), self.assertRaises(DoctorError):
                validate(bad, schema, "response")
