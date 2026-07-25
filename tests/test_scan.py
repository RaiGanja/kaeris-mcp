"""Repo-scan unit tests for the KAERIS MCP server.

The Model Context Protocol SDK is only needed for the @mcp.tool decorator, so we stub it here
and unit-test the pure repo-discovery logic (kaeris_scan_repo / _classify_locale_file) directly.
"""
import os
import sys
import types
import tempfile
import unittest

# Stub the MCP SDK so importing the server never requires it for these pure-logic tests.
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


class TestScanRepo(unittest.TestCase):
    def _classify(self, root, rel):
        dirpath = os.path.join(root, os.path.dirname(rel))
        return s._classify_locale_file(root, dirpath, os.path.basename(rel))

    def test_fluent_ftl_is_discovered(self):
        # Fluent (.ftl) is a KAERIS-owned format — the scanner must not skip it.
        with tempfile.TemporaryDirectory() as d:
            info = self._classify(d, "en.ftl")
            self.assertIsNotNone(info, ".ftl must be recognized as a locale file")
            self.assertEqual(info["lang"], "en")

    def test_flutter_arb_lang_is_locale_not_app_prefix(self):
        # app_en.arb -> en (not "app_en"); app_en_US.arb -> en_US (not "US"); bare en.arb -> en.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self._classify(d, "lib/l10n/app_en.arb")["lang"], "en")
            self.assertEqual(self._classify(d, "lib/l10n/app_de.arb")["lang"], "de")
            self.assertEqual(self._classify(d, "lib/l10n/app_en_US.arb")["lang"], "en_US")
            self.assertEqual(self._classify(d, "l10n/en.arb")["lang"], "en")

    def test_plain_stem_and_dir_langs_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self._classify(d, "locales/en.json")["lang"], "en")
            self.assertEqual(self._classify(d, "messages/pt-BR.json")["lang"], "pt-BR")
            # i18next namespace file inside a lang-named dir
            self.assertEqual(self._classify(d, "locales/de/translation.json")["lang"], "de")


if __name__ == "__main__":
    unittest.main()
