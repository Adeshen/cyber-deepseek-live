"""Launcher dependency selection with temporary config and no live services."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import call, MagicMock, patch


LAUNCHER = Path(__file__).resolve().parents[1] / "tools" / "run_local.py"


@unittest.skipIf(os.name == "nt", "Mac launcher uses fcntl; Windows uses service.ps1")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        module = runpy.run_path(str(LAUNCHER), run_name="launcher_test")
        self.main = module["main"]
        self.globals = self.main.__globals__
        self.temp = tempfile.TemporaryDirectory(prefix="launcher-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.python = self.root / ".venv" / "bin" / "python"
        self.python.parent.mkdir(parents=True)
        self.python.touch()
        self.secret_marker = "fixture-only-must-not-be-printed"

    def _run(self, engine, *, gateway_ready=False):
        (self.root / "config.json").write_text(json.dumps({
            "tts": {"engine": engine}, "vidu": {"api_key": self.secret_marker}
        }))
        service = MagicMock()
        service.poll.side_effect = [None, 0]
        service.wait.return_value = 0
        gateway = MagicMock(return_value=gateway_ready)
        tunnel = MagicMock()
        stop_tunnel = MagicMock()
        listen = MagicMock(return_value=False)
        output = io.StringIO()
        with patch.dict(self.globals, {
            "ROOT": self.root, "LOCAL": self.root / "assets" / "local",
            "PYTHON": self.python,
            "_project_health": MagicMock(side_effect=[False, False, True]),
            "_listening": listen, "_breeze_health": gateway,
            "_start_tunnel": tunnel, "_stop_tunnel": stop_tunnel,
            "_open_stage": MagicMock(),
        }), patch.dict("os.environ", {"VIDU_CONF": ""}), \
                patch.object(self.globals["subprocess"], "Popen", return_value=service), \
                patch.object(self.globals["subprocess"], "run", side_effect=AssertionError("unexpected SSH")), \
                patch("os.chdir"), redirect_stdout(output):
            result = self.main()
        self.assertEqual(result, 0)
        self.assertNotIn(self.secret_marker, output.getvalue())
        return gateway, tunnel, stop_tunnel, listen, output.getvalue()

    def test_official_and_omnivoice_skip_gateway_entirely(self):
        for engine in ("breezeblue", "omnivoice"):
            with self.subTest(engine=engine):
                gateway, tunnel, cleanup, listen, output = self._run(engine)
                gateway.assert_not_called()
                tunnel.assert_not_called()
                cleanup.assert_called_once_with(None)
                self.assertEqual(listen.call_args_list, [call(8795), call(8795)])
                if engine == "breezeblue":
                    self.assertIn("本地桥 + BreezeBlue 官方 API", output)

    def test_legacy_gateway_is_created_and_owned(self):
        gateway, tunnel, cleanup, listen, _ = self._run("breeze")
        gateway.assert_called_once_with()
        tunnel.assert_called_once()
        control = tunnel.call_args.args[0]
        self.assertEqual(control.parent, self.root / "assets" / "local")
        cleanup.assert_called_once_with(control)
        self.assertIn(call(17860), listen.call_args_list)

    def test_existing_project_reuses_before_config_or_dependencies(self):
        open_stage = MagicMock()
        with patch.dict(self.globals, {
            "_project_health": lambda: True, "_open_stage": open_stage,
            "_configured_tts_engine": MagicMock(side_effect=AssertionError("config should not be read")),
            "_listening": MagicMock(side_effect=AssertionError("port should not be probed")),
            "_start_tunnel": MagicMock(side_effect=AssertionError("SSH should not start")),
        }), patch("os.chdir"), redirect_stdout(io.StringIO()):
            self.assertEqual(self.main(), 0)
        open_stage.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
