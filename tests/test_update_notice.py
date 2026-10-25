"""kaeris-mcp: the new-version hint goes to stderr only — stdout is the protocol channel."""
import io
import os
import sys
import tempfile
import unittest

from kaeris_mcp import server


class UpdateNotice(unittest.TestCase):
    def _run(self, **env):
        from kaeris import updatecheck as uc
        err, out = io.StringIO(), io.StringIO()
        real_out, real_err, real = sys.stdout, sys.stderr, uc.maybe_notify
        seen = {}

        def fake(package, current, **kw):
            seen.update(package=package, current=current, **kw)
            return real(package, current, stream=err, env=env, now=lambda: 1e6,
                        fetch=lambda u, t: "9.9.9", cache_dir=tempfile.mkdtemp(),
                        quiet=kw.get("quiet", False), require_tty=kw.get("require_tty", True))

        uc.maybe_notify = fake
        sys.stdout = out
        try:
            server._update_hint(wait=True)
        finally:
            uc.maybe_notify, sys.stdout, sys.stderr = real, real_out, real_err
        return out.getvalue(), err.getvalue(), seen

    def test_hint_goes_to_stderr_not_stdout(self):
        out, err, seen = self._run()
        self.assertEqual(out, "")
        self.assertIn(f"A new version of kaeris-mcp is available: {server.__version__} → 9.9.9", err)
        self.assertIn("pip install -U kaeris-mcp", err)
        self.assertEqual(seen["package"], "kaeris-mcp")
        self.assertFalse(seen["require_tty"])  # a client starts us with stderr piped

    def test_ci_and_opt_out_are_silent(self):
        for env in ({"CI": "1"}, {"KAERIS_NO_UPDATE_CHECK": "1"}):
            out, err, _ = self._run(**env)
            self.assertEqual((out, err), ("", ""))

    def test_main_help_and_version_do_not_check(self):
        from kaeris import updatecheck as uc
        real, called = uc.maybe_notify, []
        uc.maybe_notify = lambda *a, **k: called.append(1)
        argv = sys.argv
        try:
            for flag in ("--help", "--version"):
                sys.argv = ["kaeris-mcp", flag]
                with io.StringIO() as buf:
                    so, sys.stdout = sys.stdout, buf
                    try:
                        server.main()
                    finally:
                        sys.stdout = so
        finally:
            uc.maybe_notify, sys.argv = real, argv
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
