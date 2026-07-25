"""kaeris_status / kaeris_list_missing_keys must work for EVERY format: JSON locally, and any
other format via the backend /api/parse endpoint (stubbed here with a fake client)."""
import os
import sys
import types
import tempfile
import unittest

# Stub the MCP SDK so the server imports without it (pure-logic tests).
if "mcp.server.fastmcp" not in sys.modules:
    _fake = types.ModuleType("mcp.server.fastmcp")

    class _FastMCP:
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            def deco(fn):
                return fn
            return deco

        def run(self, *a, **k):
            pass

    _fake.FastMCP = _FastMCP
    sys.modules.setdefault("mcp", types.ModuleType("mcp"))
    sys.modules.setdefault("mcp.server", types.ModuleType("mcp.server"))
    sys.modules["mcp.server.fastmcp"] = _fake

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kaeris_mcp.server as s  # noqa: E402


class _FakeClient:
    """Stands in for KaerisClient: parse() returns a preset flat dict keyed by filename,
    so non-JSON formats are exercised without a real backend call."""
    def __init__(self, table):
        self.table = table

    def parse(self, filename, content):
        return dict(self.table[filename])


def _write(d, name, text):
    path = os.path.join(d, name)
    os.makedirs(os.path.dirname(path) or d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class TestCheckAllFormats(unittest.TestCase):
    def test_status_json_is_local(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "en.json", '{"a":"A","b":"B"}')
            _write(d, "de.json", '{"a":"A-de"}')
            r = s.kaeris_status(os.path.join(d, "en.json"), ["de"])
            self.assertFalse(r["ok"])
            self.assertEqual(r["missing"]["de"], ["b"])

    def test_status_non_json_via_backend_parse(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "en.strings", '"a" = "A";\n"b" = "B";\n')
            _write(d, "de.strings", '"a" = "A-de";\n')
            fake = _FakeClient({"en.strings": {"a": "A", "b": "B"}, "de.strings": {"a": "A-de"}})
            orig, s._client = s._client, lambda *a, **k: fake
            try:
                r = s.kaeris_status(os.path.join(d, "en.strings"), ["de"])
            finally:
                s._client = orig
            self.assertFalse(r["ok"])
            self.assertEqual(r["missing"]["de"], ["b"])   # non-JSON source is now checkable

    def test_list_missing_keys_non_json(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "en.arb", "{}")
            _write(d, "de.arb", "{}")
            fake = _FakeClient({"en.arb": {"greet": "Hi", "bye": "Bye"}, "de.arb": {"greet": "Hallo"}})
            orig, s._client = s._client, lambda *a, **k: fake
            try:
                r = s.kaeris_list_missing_keys(os.path.join(d, "en.arb"), "de")
            finally:
                s._client = orig
            self.assertEqual(r["missing"], ["bye"])
            self.assertEqual(r["missing_values"], {"bye": "Bye"})
            self.assertFalse(r["missing_file"])


if __name__ == "__main__":
    unittest.main()
