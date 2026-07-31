"""The app context — one line about what the app is — must reach the API from the MCP tools too.

An agent working in a repo is the one caller that actually knows what the app is about, so
this is where the context is cheapest to supply and most valuable: "Bank" is Ufer in a
wildlife app and Bank in a banking one. It also has to enter the lock's settings signature,
so changing it retranslates the locale instead of mixing two contexts in one file.
"""
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
    """Records every submit — including the app_context that reached the wire."""
    calls = []

    def submit(self, filename, content, languages, glossary=None, tone="", icu=False,
               app_context="", **kw):
        FakeClient.calls.append({
            "content": content,
            "languages": list(languages),
            "app_context": app_context,
        })
        return f"job-{len(FakeClient.calls)}"

    def poll(self, job_id, on_progress=None, interval=1.0, max_wait=1800):
        return {"status": "done", "failed_langs": []}

    def download(self, job_id):
        idx = int(job_id.rsplit("-", 1)[1]) - 1
        call = FakeClient.calls[idx]
        return {f"{lang}.json": call["content"] for lang in call["languages"]}

    def key_info(self):
        return {"model_id": "test-model"}

    def preview(self, job_id):
        return {}


class _Base(unittest.TestCase):
    def setUp(self):
        FakeClient.calls = []
        self._real = s._client
        s._client = lambda *a, **k: FakeClient()

    def tearDown(self):
        s._client = self._real

    def _source(self, tmp):
        path = os.path.join(tmp, "en.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"bank": "Bank"}, f)
        return path


class TestContextReachesTheApi(_Base):
    def test_translate_file(self):
        with tempfile.TemporaryDirectory() as t:
            s.kaeris_translate_file(self._source(t), ["de"], out_dir=t,
                                    context="a wildlife app about rivers")
        self.assertEqual(FakeClient.calls[-1]["app_context"], "a wildlife app about rivers")

    def test_add_language(self):
        with tempfile.TemporaryDirectory() as t:
            s.kaeris_add_language(self._source(t), "de", out=t,
                                  context="a wildlife app about rivers")
        self.assertEqual(FakeClient.calls[-1]["app_context"], "a wildlife app about rivers")

    def test_translate_strings(self):
        s.kaeris_translate({"bank": "Bank"}, ["de"],
                           context="a wildlife app about rivers")
        self.assertEqual(FakeClient.calls[-1]["app_context"], "a wildlife app about rivers")

    def test_incremental_path(self):
        with tempfile.TemporaryDirectory() as t:
            s.kaeris_translate_file(self._source(t), ["de"], out_dir=t, only_new=True,
                                    context="a wildlife app about rivers")
        self.assertEqual(FakeClient.calls[-1]["app_context"], "a wildlife app about rivers")

    def test_absent_context_sends_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            s.kaeris_translate_file(self._source(t), ["de"], out_dir=t)
        self.assertEqual(FakeClient.calls[-1]["app_context"], "")


class TestContextIsPartOfTheLock(_Base):
    def _run(self, tmp, src, context):
        return s.kaeris_translate_file(src, ["de"], out_dir=tmp, only_new=True, context=context)

    def test_changed_context_retranslates_everything(self):
        with tempfile.TemporaryDirectory() as t:
            src = self._source(t)
            self._run(t, src, "a wildlife app about rivers")
            before = len(FakeClient.calls)
            self._run(t, src, "a wildlife app about rivers")
            self.assertEqual(len(FakeClient.calls), before,
                             "same context must not resend anything")
            self._run(t, src, "a mobile bank for teenagers")
            self.assertEqual(len(FakeClient.calls), before + 1,
                             "changed context must retranslate the locale")


if __name__ == "__main__":
    unittest.main()
