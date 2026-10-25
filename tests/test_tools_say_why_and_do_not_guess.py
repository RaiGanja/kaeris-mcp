"""Three ways the MCP tools used to mislead the agent calling them.

* A bad language code came back as a bare "Error executing tool" — the SDK hides an exception's
  text, so the agent could not tell a typo from an outage. The reason now travels in the answer,
  before anything is sent or charged.
* kaeris_scan_repo read any short folder name (src, api, ui, app) as a language.
* kaeris_status called a language missing when its file sat in <out>/<lang>/<name>.
"""
import json
import os
import sys
import tempfile
import types
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


class _Offering:
    """Offers de/ja/pt; anything past the language check would call submit."""
    submitted = False

    def languages(self):
        return {"de": "German", "ja": "Japanese", "pt": "Portuguese"}

    def submit(self, *a, **k):
        _Offering.submitted = True
        return "job-1"


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TestUnknownLanguage(unittest.TestCase):
    def setUp(self):
        _Offering.submitted = False
        self._real = s._client
        s._client = lambda *a, **k: _Offering()

    def tearDown(self):
        s._client = self._real

    def test_translate_file_answers_with_the_reason(self):
        with tempfile.TemporaryDirectory() as t:
            src = os.path.join(t, "en.json")
            _write(src, '{"a": "Hello"}')
            out = s.kaeris_translate_file(src, ["de", "xx"])
        self.assertFalse(out["ok"])
        self.assertIn("xx", out["error"])
        self.assertIn("kaeris_list_languages", out["error"])
        self.assertFalse(_Offering.submitted)

    def test_translate_answers_with_the_reason(self):
        out = s.kaeris_translate({"a": "Hello"}, ["qq"])
        self.assertFalse(out["ok"])
        self.assertIn("qq", out["error"])
        self.assertFalse(_Offering.submitted)

    def test_add_language_answers_with_the_reason(self):
        with tempfile.TemporaryDirectory() as t:
            src = os.path.join(t, "en.json")
            _write(src, '{"a": "Hello"}')
            out = s.kaeris_add_language(src, "xx")
        self.assertFalse(out["ok"])
        self.assertFalse(_Offering.submitted)

    def test_a_regional_spelling_is_not_refused(self):
        self.assertEqual(s._unknown_langs(_Offering(), ["pt-BR", "de", "ja"]), [])

    def test_nothing_is_refused_when_the_list_cannot_be_fetched(self):
        class Offline:
            def languages(self):
                raise RuntimeError("offline")
        self.assertEqual(s._unknown_langs(Offline(), ["xx"]), [])


class TestScanDoesNotReadFolderNamesAsLanguages(unittest.TestCase):
    def test_short_folder_names_are_not_languages(self):
        with tempfile.TemporaryDirectory() as t:
            _write(os.path.join(t, "locales", "en.json"), "{}")
            _write(os.path.join(t, "locales", "de.json"), "{}")
            for folder in ("src", "api", "ui", "app", "doc"):
                _write(os.path.join(t, folder, "settings.json"), "{}")
            _write(os.path.join(t, "app.json"), "{}")
            res = s.scan_repo(t)
        self.assertEqual(sorted(res["target_langs"] + [res["base_lang"]]), ["de", "en"])
        self.assertEqual(res["file_count"], 2)

    def test_real_language_folders_still_count(self):
        with tempfile.TemporaryDirectory() as t:
            _write(os.path.join(t, "locales", "en", "translation.json"), "{}")
            _write(os.path.join(t, "locales", "pt-BR", "translation.json"), "{}")
            res = s.scan_repo(t)
        self.assertEqual(sorted(res["target_langs"] + [res["base_lang"]]), ["en", "pt-BR"])


class TestStatusFindsPerLanguageFolders(unittest.TestCase):
    def test_a_language_in_its_own_folder_under_out_is_not_missing(self):
        with tempfile.TemporaryDirectory() as t:
            src = os.path.join(t, "t.json")
            _write(src, json.dumps({"a": "Hello"}))
            _write(os.path.join(t, "o", "de", "t.json"), json.dumps({"a": "Hallo"}))
            res = s.kaeris_status(src, langs=["de"], out=os.path.join(t, "o"))
        self.assertEqual(res["missing_files"], [])
        self.assertTrue(res["ok"])

    def test_a_truly_absent_language_is_still_missing(self):
        with tempfile.TemporaryDirectory() as t:
            src = os.path.join(t, "t.json")
            _write(src, json.dumps({"a": "Hello"}))
            res = s.kaeris_status(src, langs=["de"], out=os.path.join(t, "o"))
        self.assertEqual(res["missing_files"], ["de"])


if __name__ == "__main__":
    unittest.main()
