"""Frozen acceptance checks; AgentDoctor is not allowed to modify this file."""
import sys
import unittest

from agent import lookup_customer, lookup_customer_by_email, tool_lookup


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
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(test_class))
    raise SystemExit(0 if result.wasSuccessful() else 1)
