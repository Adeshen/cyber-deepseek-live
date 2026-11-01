"""Local deployment regression tests; all configuration and network data are fake."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from src import config, tts
from src.tts.breeze import BreezeTTS


def mode(path):
    return path.stat().st_mode & 0o777


class PrivateConfigurationTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX mode bits; Windows deployment uses NTFS ACLs")
    def test_write_private_new_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            config._write_private(path, '{"example": true}')
            self.assertEqual(mode(path), 0o600)
            self.assertEqual(json.loads(path.read_text()), {"example": True})

    @unittest.skipIf(os.name == "nt", "POSIX mode bits; Windows deployment uses NTFS ACLs")
    def test_write_private_repairs_existing_readable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json.tmp"
            path.write_text("old")
            path.chmod(0o644)
            config._write_private(path, "new")
            self.assertEqual(mode(path), 0o600)
            self.assertEqual(path.read_text(), "new")

    def test_save_new_configuration_is_private_and_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            conf = {"tts": {"engine": "breeze"}, "example": "no credentials"}
            with patch.object(config, "CONF_PATH", path), patch.object(config, "_conf", None):
                config.save_conf(conf)
                self.assertEqual(config.load(), conf)
            if os.name != "nt":
                self.assertEqual(mode(path), 0o600)
            self.assertEqual(json.loads(path.read_text()), conf)
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_save_repairs_readable_config_temporary_file_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            temporary = path.with_suffix(".json.tmp")
            backup = path.with_suffix(".json.bak")
            previous = {"example": "previous harmless configuration"}
            current = {"example": "updated harmless configuration"}
            for target, content in ((path, json.dumps(previous)),
                                    (temporary, "stale temporary"), (backup, "old backup")):
                target.write_text(content)
                target.chmod(0o644)
            with patch.object(config, "CONF_PATH", path), patch.object(config, "_conf", None):
                config.save_conf(current)
            if os.name != "nt":
                self.assertEqual(mode(path), 0o600)
                self.assertEqual(mode(backup), 0o600)
            self.assertEqual(json.loads(path.read_text()), current)
            self.assertEqual(json.loads(backup.read_text()), previous)
            self.assertFalse(temporary.exists())

    def test_write_private_without_posix_fchmod(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            with patch.object(config.os, "fchmod", None, create=True):
                config._write_private(path, '{"example": true}')
            self.assertEqual(json.loads(path.read_text()), {"example": True})


class SpeechFactoryTests(unittest.TestCase):
    def test_breeze_factory_uses_selected_endpoint_and_timeout(self):
        engine = tts.create_tts({"engine": "breeze", "breeze_base": "http://127.0.0.1:17860/",
                                 "breeze_timeout_sec": 42})
        self.assertIsInstance(engine, BreezeTTS)
        self.assertEqual(engine.active_base(), "http://127.0.0.1:17860")
        self.assertEqual(engine.timeout_sec, 42)
        with self.assertRaises(RuntimeError):
            tts.create_tts({"engine": "unknown"})

    def test_breeze_scan_ignores_omnivoice_design_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            breeze = base / "breeze-design"
            breeze.mkdir()
            (breeze / "breeze.json").write_text(json.dumps(
                {"label": "Breeze test", "kind": "design",
                 "params": {"instruction": "清亮的普通话", "seed": 202}}), encoding="utf-8")
            omni = base / "omni-design"
            omni.mkdir()
            (omni / "design.json").write_text('{"gender": "Female"}')
            presets = tts.scan_voices({"engine": "breeze", "voices_dir": tmp})
            self.assertEqual(list(presets), ["breeze-design"])
            self.assertEqual(presets["breeze-design"]["provider"], "breeze")
            self.assertEqual(presets["breeze-design"]["params"]["instruction"], "清亮的普通话")


class FailedConnectionCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_signaling_connect_failure_closes_assigned_signaling(self):
        # Importing the engine does not instantiate it or load a real config.
        from src.engine import Engine

        fake_config = {"tts": {"engine": "breeze", "breeze_base": "http://127.0.0.1:17860"},
                       "vidu": {"host": "example.invalid", "api_key": "fake-test-only"},
                       "avatar": {"avatar_id": "test-avatar"}, "rtc": {"mode": "managed"}}
        signaling = AsyncMock()
        signaling.connect.side_effect = RuntimeError("simulated connection failure")
        with patch("src.engine.appconf.load", return_value=fake_config), \
                patch("src.engine.vu.configure"), \
                patch("src.engine.vu.get_avatar", new=AsyncMock(return_value={"status": "success"})), \
                patch("src.engine.vu.create_component", new=AsyncMock(
                    return_value={"live": {"id": "fake-test-live"}})), \
                patch("src.engine.signaling.ComponentSignaling", return_value=signaling):
            engine = Engine()
            with self.assertRaisesRegex(RuntimeError, "simulated connection failure"):
                await engine.start()
        signaling.connect.assert_awaited_once()
        signaling.hangup.assert_awaited_once_with("startup_failed")
        signaling.close.assert_awaited_once()
        self.assertIsNone(engine.sig)
        self.assertEqual(engine.phase, "idle")

    async def test_unprepared_avatar_does_not_create_a_billable_session(self):
        from src.engine import Engine
        fake_config = {"tts": {"engine": "breeze"}, "vidu": {},
                       "avatar": {"avatar_id": "fake-avatar"}, "rtc": {"mode": "managed"}}
        for status in ["created", "processing", "failed", "1", "6", "12"]:
            with self.subTest(status=status), \
                    patch("src.engine.appconf.load", return_value=fake_config), \
                    patch("src.engine.vu.configure"), \
                    patch("src.engine.vu.get_avatar", new=AsyncMock(return_value={"status": status})), \
                    patch("src.engine.vu.create_component", new=AsyncMock()) as create:
                engine = Engine()
                with self.assertRaises(RuntimeError):
                    await engine.start()
                create.assert_not_awaited()
                self.assertEqual(engine.phase, "idle")


if __name__ == "__main__":
    unittest.main()
