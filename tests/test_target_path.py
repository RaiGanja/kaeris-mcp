"""Regression (2026-07-19 audit #10): kaeris_add_language / kaeris_translate_file wrote a
new locale to <out>/<lang>.json even for i18next namespace (locales/<lang>/translation.json)
and Flutter ARB (app_<lang>.arb) sources — the wrong path, so `kaeris translate`'s output
and the MCP's output diverged and status/list_missing couldn't find namespace locales.
_target_locale_path now mirrors the source layout (the MCP analogue of the CLI's _target_path).
"""
import os
import sys
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


def _norm(p):
    return p.replace(os.sep, "/")


class TestTargetLocalePath(unittest.TestCase):
    def test_i18next_namespace_mirrors_lang_dir(self):
        src = "locales/en/translation.json"
        out = s._resolve_out_dir(src, "")            # default: beside the source
        dest = s._target_locale_path(src, "de", out, "de.json")
        self.assertEqual(_norm(dest), "locales/de/translation.json")

    def test_flutter_arb_prefix_mirrors_stem(self):
        src = "lib/l10n/app_en.arb"
        out = s._resolve_out_dir(src, "")
        dest = s._target_locale_path(src, "de", out, "de.arb")
        self.assertEqual(_norm(dest), "lib/l10n/app_de.arb")

    def test_flat_layout_unchanged(self):
        src = "locales/en.json"
        out = s._resolve_out_dir(src, "")
        dest = s._target_locale_path(src, "de", out, "de.json")
        self.assertEqual(_norm(dest), _norm(os.path.join(out, "de.json")))

    def test_explicit_custom_out_stays_flat(self):
        # An explicit out overrides layout-mirroring — predictable flat output there.
        src = "locales/en/translation.json"
        dest = s._target_locale_path(src, "de", "/tmp/custom_out", "de.json")
        self.assertEqual(_norm(dest), "/tmp/custom_out/de.json")


if __name__ == "__main__":
    unittest.main()
