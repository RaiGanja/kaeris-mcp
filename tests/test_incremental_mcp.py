"""The MCP's incremental translate must be reproducible like the CLI: an EDITED source string is
re-translated (via kaeris.lock), not silently left stale, and unchanged keys are not resent."""
import os
import sys
import json
import types
import tempfile
import unittest

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


class FakeClient:
    """Echoes the submitted subset back as the translation, recording each submit."""
    calls = []

    def submit(self, filename, content, languages, glossary=None, tone="", icu=False, **kw):
        FakeClient.calls.append({"content": content, "languages": list(languages)})
        return f"job-{len(FakeClient.calls)}"

    def poll(self, job_id, **kw):
        return {"status": "done", "failed_langs": []}

    def download(self, job_id):
        idx = int(job_id.rsplit("-", 1)[1]) - 1
        call = FakeClient.calls[idx]
        return {f"{lang}.json": call["content"] for lang in call["languages"]}


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


class TestMcpIncrementalReproducible(unittest.TestCase):
    def setUp(self):
        FakeClient.calls = []

    def test_edited_source_key_retranslated_via_lock(self):
        with tempfile.TemporaryDirectory() as d:
            en = os.path.join(d, "en.json")
            _write(en, {"a": "Hello", "b": "Bye"})

            # First run: no target yet, so the whole locale is translated — and THAT is what
            # puts an honest baseline in the lock. (Until 16.08.2026 this test started from a
            # ready-made de.json with no lock and expected the next run to retranslate the
            # edited key. It did — by stamping translations nothing vouched for as current,
            # which is the very damage the CLI's unverified-keys guard exists to prevent. The
            # parity test with the CLI covers that case now.)
            r1 = s._translate_file_incremental(FakeClient(), en, d, ["de"])
            self.assertTrue(r1["written"])
            self.assertTrue(os.path.isfile(os.path.join(d, "kaeris.lock")))

            # Edit ONE source string; a re-run must retranslate only that key (missing_keys missed this).
            _write(en, {"a": "Hello there", "b": "Bye"})
            FakeClient.calls = []
            r2 = s._translate_file_incremental(FakeClient(), en, d, ["de"])
            self.assertTrue(r2["written"])
            sent = json.loads(FakeClient.calls[-1]["content"].decode("utf-8"))
            self.assertEqual(set(sent), {"a"})           # only the edited key, not "b"

    def test_settings_change_retranslates_all(self):
        with tempfile.TemporaryDirectory() as d:
            en = os.path.join(d, "en.json")
            _write(en, {"a": "Hello", "b": "Bye"})
            # First run (neutral tone) fills de + writes lock with neutral settings.
            s._translate_file_incremental(FakeClient(), en, d, ["de"], tone="")
            FakeClient.calls = []
            # Same settings -> nothing resent.
            s._translate_file_incremental(FakeClient(), en, d, ["de"], tone="")
            self.assertEqual(FakeClient.calls, [])
            # Tone change -> whole locale retranslated.
            s._translate_file_incremental(FakeClient(), en, d, ["de"], tone="formal")
            self.assertEqual(len(FakeClient.calls), 1)
            sent = json.loads(FakeClient.calls[-1]["content"].decode("utf-8"))
            self.assertEqual(set(sent), {"a", "b"})


if __name__ == "__main__":
    unittest.main()
