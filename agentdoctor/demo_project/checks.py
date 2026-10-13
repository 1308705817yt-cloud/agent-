"""Frozen acceptance checks; AgentDoctor is not allowed to modify this file."""
import json
import os
import sys
import unittest

INVOCATION = os.environ.get("AGENTDOCTOR_VALIDATION_ID", "standalone")


def emit(results, completed, error=None):
    print("AGENTDOCTOR_RESULT " + json.dumps({"schema_version": 1, "invocation_id": INVOCATION,
          "completed": completed, "executed_count": len(results), "results": results, "suite_error": error}))


try:
    from agent import lookup_customer, lookup_customer_by_email, tool_lookup
except BaseException as exc:
    emit([], False, f"Import failed: {type(exc).__name__}: {exc}")
    raise SystemExit(1)


class Target(unittest.TestCase):
    def test_customer_one(self):
        self.assertEqual(lookup_customer("C001")["name"], "Ada")

    def test_customer_two(self):
        self.assertEqual(lookup_customer("C002")["email"], "lin@example.test")

    def test_unknown_customer(self):
        with self.assertRaises(KeyError):
            lookup_customer("C999")

    def test_invalid_identifier(self):
        with self.assertRaises(ValueError):
            lookup_customer("invalid")


class Regression(unittest.TestCase):
    def test_direct_tool(self):
        self.assertEqual(tool_lookup("C001")["id"], "C001")

    def test_email_lookup(self):
        self.assertEqual(lookup_customer_by_email("lin@example.test")["id"], "C002")

    def test_missing_email(self):
        self.assertIsNone(lookup_customer_by_email("missing@example.test"))

    def test_return_value_does_not_mutate_storage(self):
        customer = tool_lookup("C001")
        customer["name"] = "Modified"
        self.assertEqual(tool_lookup("C001")["name"], "Ada")


if __name__ == "__main__":
    suites = {"target": Target, "regression": Regression}
    test_class = suites[sys.argv[1]]
    outcomes = []
    for test in unittest.defaultTestLoader.loadTestsFromTestCase(test_class):
        try:
            result = unittest.TextTestRunner(verbosity=2).run(test)
            status = "skipped" if result.skipped else "passed" if result.wasSuccessful() and result.testsRun == 1 else "failed"
            outcomes.append({"id": f"{test_class.__name__}.{test._testMethodName}", "status": status})
        except BaseException as exc:
            outcomes.append({"id": f"{test_class.__name__}.{test._testMethodName}", "status": "error", "detail": f"{type(exc).__name__}: {exc}"})
    emit(outcomes, True)
    raise SystemExit(0 if outcomes and all(r["status"] == "passed" for r in outcomes) else 1)
