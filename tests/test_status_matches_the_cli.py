"""`kaeris_status` promises "same as `kaeris check --json`". It has to mean it.

Found on 03.08.2026 by using the published server the way an agent would. A German locale
that turned "Delete 5 files" into "50 Dateien löschen" — a number drift, RED in the CLI,
exit 1, merge blocked — came back from `kaeris_status` as `"ok": true`, nothing found.

`_status` computed key diffs and placeholder mismatches and stopped there: none of the
detectors ran. So the tool that an agent calls when asked "check this repo's i18n" gave a
clean bill of health to a repository our own firewall rejects. A false all-clear is worse
than no tool: the human trusts it and stops looking.

The test compares the two verdicts on one corpus rather than asserting a hand-written
expectation, so the day a detector is added to the CLI, the MCP server has to carry it too.
"""
import json
import os
import sys
import tempfile
import types
import unittest

# Stub the MCP SDK so the server imports without it (pure-logic tests).
# tests/test_real_sdk_import.py covers the real import — this stub must never be the only path.
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
from kaeris import check as chk  # noqa: E402

SOURCE = {
    "greeting": "Hello, {name}!",
    "delete": "Delete 5 files",
    "bold": "Read our <b>terms</b> before you continue",
    "brand": "Powered by KAERIS",
    "maxUsers": 100,          # non-string values live in real locale files
    "enabled": True,
}

# Each case is one defect a customer would want caught, with the same source.
BROKEN = {
    "number drift": dict(SOURCE, greeting="Hallo, {name}!", delete="50 Dateien löschen",
                         bold="Lesen Sie unsere <b>AGB</b>", brand="Powered by KAERIS"),
    "lost inline tag": dict(SOURCE, greeting="Hallo, {name}!", delete="5 Dateien löschen",
                            bold="Lesen Sie unsere AGB", brand="Powered by KAERIS"),
    "lost placeholder": dict(SOURCE, greeting="Hallo!", delete="5 Dateien löschen",
                             bold="Lesen Sie unsere <b>AGB</b>", brand="Powered by KAERIS"),
}
CLEAN = dict(SOURCE, greeting="Hallo, {name}!", delete="5 Dateien löschen",
             bold="Lesen Sie unsere <b>AGB</b>", brand="Powered by KAERIS")


def _write(d, name, obj):
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    return path


class StatusAgreesWithTheFirewall(unittest.TestCase):
    def _status(self, target):
        with tempfile.TemporaryDirectory() as d:
            src = _write(d, "en.json", SOURCE)
            _write(d, "de.json", target)
            return s.kaeris_status(src, ["de"])

    def _cli(self, target):
        return chk.check_locales(SOURCE, ["de"], lambda _l: target, [])

    def test_a_clean_locale_passes_in_both(self):
        self.assertTrue(self._cli(CLEAN)["ok"])
        self.assertTrue(self._status(CLEAN)["ok"], "MCP rejects a locale the CLI accepts")

    def test_every_defect_the_cli_blocks_is_reported_by_the_server(self):
        for name, target in BROKEN.items():
            with self.subTest(defect=name):
                cli = self._cli(target)
                self.assertFalse(cli["ok"], f"corpus error: the CLI does not flag {name}")
                mcp = self._status(target)
                self.assertFalse(
                    mcp["ok"],
                    f"{name}: the CLI blocks this merge and the agent is told everything "
                    "is fine",
                )

    def test_the_faults_themselves_reach_the_agent(self):
        """`ok: false` alone leaves the agent guessing. It needs what and where."""
        result = self._status(BROKEN["number drift"])
        self.assertIn("faults", result, "no `faults` key — an agent cannot act on a bare flag")
        blob = json.dumps(result, ensure_ascii=False)
        self.assertIn("delete", blob, "the offending key must be named")

    def test_non_string_values_do_not_crash_the_tool(self):
        """A crash inside an MCP tool reaches the user as a broken assistant, not a verdict."""
        result = self._status(CLEAN)
        self.assertTrue(result["ok"])
        self.assertEqual(result["missing"], {})


if __name__ == "__main__":
    unittest.main()
