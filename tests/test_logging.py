import logging
import sys
import unittest

from core.logging import SafeFormatter, log_context


class LoggingTests(unittest.TestCase):
    def test_traceback_keeps_locations_without_exception_contents(self):
        token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
        try:
            raise RuntimeError(f"secret https://cdn.example/file?token={token}")
        except RuntimeError:
            record = logging.LogRecord("test", logging.ERROR, __file__, 1, "failure", (), sys.exc_info())
        formatted = SafeFormatter("%(message)s %(context)s").format(record)
        self.assertIn("test_logging.py:", formatted)
        self.assertIn("exception=RuntimeError", formatted)
        self.assertNotIn(token, formatted)
        self.assertNotIn("cdn.example", formatted)
        self.assertIsNotNone(record.exc_info)

    def test_context_redaction_and_no_log_injection(self):
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "url=https://example.org/secret\nforged", (), None
        )
        formatter = SafeFormatter("%(message)s %(context)s")
        with log_context(job=42, stage="download"):
            first = formatter.format(record)
            self.assertEqual(first, formatter.format(record))
            self.assertIn("job=42 stage=download", first)
            self.assertIn("\\n", first)
            self.assertNotIn("example.org", first)
        self.assertNotIn("job=42", formatter.format(record))
