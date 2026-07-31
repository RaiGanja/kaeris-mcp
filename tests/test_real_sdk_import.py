"""The server must import and register its tools against the REAL MCP SDK — every version
of it we claim to support.

Every other test in this directory installs a fake `mcp.server.fastmcp` before importing
the server, because that keeps them fast and offline. The cost of that convenience is that
they cannot see the one failure that takes the whole product down: the SDK moving the class
we import. MCP 2.0 did exactly that — `mcp.server.fastmcp` is gone, `FastMCP` is now
`mcp.server.mcpserver.MCPServer` — so `pip install kaeris-mcp` in a fresh environment
resolved to mcp 2.0.0 and died on line 16 before serving a single tool.

This test refuses the fake. It imports the server against whatever SDK is actually
installed and asserts the tools are there, so a rename can never again be invisible to us.
"""
import importlib
import sys
import unittest


def _real_sdk_version():
    try:
        import importlib.metadata as md
        return md.version("mcp")
    except Exception:
        return None


@unittest.skipIf(_real_sdk_version() is None, "MCP SDK not installed in this environment")
class TestRealSdk(unittest.TestCase):
    def setUp(self):
        # Drop anything a sibling test faked, so the import below hits the real SDK.
        for name in [m for m in sys.modules if m == "mcp" or m.startswith("mcp.")]:
            if getattr(sys.modules[name], "__file__", None) is None:
                del sys.modules[name]
        sys.modules.pop("kaeris_mcp.server", None)

    def test_server_imports_and_exposes_its_tools(self):
        server = importlib.import_module("kaeris_mcp.server")
        self.assertTrue(hasattr(server, "mcp"),
                        f"no server object built against mcp {_real_sdk_version()}")

    def test_tools_are_registered(self):
        import asyncio
        server = importlib.import_module("kaeris_mcp.server")
        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        for expected in ("kaeris_translate", "kaeris_translate_file", "kaeris_add_language",
                         "kaeris_scan_repo", "kaeris_status", "kaeris_list_languages"):
            self.assertIn(expected, names)


@unittest.skipIf(_real_sdk_version() is None, "MCP SDK not installed in this environment")
class TestServerAnnouncesItsVersion(unittest.TestCase):
    """A client shows what the server says about itself in the initialize handshake. Ours
    said nothing — serverInfo.version was "" in Claude Desktop and in every registry probe.
    Asserted over the real stdio protocol, because the two SDKs carry the field differently
    and only the wire says which one we actually got right."""

    def _handshake(self):
        import json
        import os
        import subprocess

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        request = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}},
        })
        proc = subprocess.run(
            [sys.executable, "-m", "kaeris_mcp"], input=request + "\n",
            capture_output=True, text=True, timeout=60, env=env, cwd=root,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("{"):
                msg = json.loads(line)
                if msg.get("id") == 1:
                    return msg["result"]["serverInfo"]
        self.fail(f"no initialize response; stderr: {proc.stderr[:400]}")

    def test_version_matches_the_package(self):
        import kaeris_mcp
        self.assertEqual(self._handshake().get("version"), kaeris_mcp.__version__)

    def test_name_is_unchanged(self):
        # The name is how existing configs and registries identify us — a fix to the
        # version must not quietly rename the server.
        self.assertEqual(self._handshake().get("name"), "kaeris-i18n")


if __name__ == "__main__":
    unittest.main()
