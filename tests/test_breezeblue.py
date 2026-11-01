"""Official API contracts, authenticated entirely with fake keys and MockTransport."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from src.tts.breezeblue import BreezeBlueTTS, BreezeBlueTTSError, OFFICIAL_BASE, scan_voices


class ClientMock:
    def __init__(self, handler):
        self.handler = handler
        self.real_client = httpx.AsyncClient
        self.options = []

    def __call__(self, **kwargs):
        self.options.append(kwargs)
        return self.real_client(transport=httpx.MockTransport(self.handler), **kwargs)


class HostedTTSContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.key_file = Path(self.temporary.name) / "fake.key"
        self.fake_key = "fake-TEST-secret+/only"
        self.key_file.write_text(self.fake_key + "\n")
        self.preset = {"voice_id": "voc_test_202", "kind": "saved", "provider": "breezeblue",
                       "params": {"instructions": "温柔地说", "voice_settings": {
                           "guidance_scale": 4.0, "speed": 1.0, "volume": 0.9,
                           "use_speaker_boost": False}}}
        self.pcm = b"\x00\x00\x01\x00\xff\xff" * 100

    def adapter(self):
        return BreezeBlueTTS(OFFICIAL_BASE, str(self.key_file))

    async def test_exact_authenticated_request_returns_unchanged_raw_pcm(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, content=self.pcm, headers={"content-type": "audio/pcm; rate=24000"})

        client = ClientMock(handler)
        with patch("src.tts.breezeblue.httpx.AsyncClient", side_effect=client):
            result = await self.adapter().synth_pcm("你好呀", self.preset)
        self.assertEqual(result, self.pcm)
        self.assertEqual(len(seen), 1)
        request = seen[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), OFFICIAL_BASE + "/text-to-speech/voc_test_202?output_format=pcm_24000&delivery=sync")
        self.assertEqual(request.headers["xi-api-key"], self.fake_key)
        self.assertEqual(json.loads(request.content), {
            "text": "你好呀", "model_id": "breeze-tts-2", "language_code": "zh",
            "instructions": "温柔地说", "voice_settings": {
                "guidance_scale": 4.0, "speed": 1.0, "volume": 0.9, "use_speaker_boost": False}})
        self.assertNotIn(self.fake_key.encode(), request.content)
        self.assertIs(client.options[0]["follow_redirects"], False)
        self.assertIs(client.options[0]["trust_env"], False)

    async def test_key_file_and_timeout_updates_are_used_on_next_request(self):
        keys = []

        def handler(request):
            keys.append(request.headers["xi-api-key"])
            return httpx.Response(200, content=self.pcm,
                                  headers={"content-type": "application/octet-stream"})

        adapter = self.adapter()
        client = ClientMock(handler)
        second_file = Path(self.temporary.name) / "second-fake.key"
        second_file.write_text("fake-second-key")
        with patch("src.tts.breezeblue.httpx.AsyncClient", side_effect=client):
            await adapter.synth_pcm("一", self.preset)
            adapter.key_file = str(second_file)
            adapter.timeout_sec = 42
            adapter.cfg_base = OFFICIAL_BASE + "/"
            await adapter.synth_pcm("二", self.preset)
        self.assertEqual(keys, [self.fake_key, "fake-second-key"])
        self.assertEqual(client.options[1]["timeout"], 42)
        self.assertEqual(adapter.active_base(), OFFICIAL_BASE)

    async def test_non_official_hosts_and_paths_are_rejected_before_key_read(self):
        candidates = ["http://api.breeze.blue/v1", "https://evil.invalid/v1",
                      "https://api.breeze.blue.evil.invalid/v1", "https://api.breeze.blue/v2",
                      "https://api.breeze.blue/v1?destination=evil", "https://api.breeze.blue@evil.invalid/v1"]
        with patch("src.tts.breezeblue.httpx.AsyncClient") as client:
            for base in candidates:
                with self.subTest(base=base), self.assertRaises(BreezeBlueTTSError):
                    BreezeBlueTTS(base, "missing-fake-key")
            client.assert_not_called()
        with self.assertRaises(BreezeBlueTTSError):
            self.adapter().cfg_base = "https://evil.invalid/v1"

    async def test_error_statuses_redact_keys_and_never_retry(self):
        for status, code in [(401, "auth"), (403, "auth"), (429, "rate_limit"), (503, "http")]:
            seen = []

            def handler(request):
                seen.append(request)
                return httpx.Response(status, json={"detail": "bad key " + self.fake_key},
                                      headers={"retry-after": "1"})

            with self.subTest(status=status), patch("src.tts.breezeblue.httpx.AsyncClient",
                                                  side_effect=ClientMock(handler)):
                with self.assertRaises(BreezeBlueTTSError) as exc:
                    await self.adapter().synth_pcm("你好", self.preset)
                self.assertEqual(exc.exception.code, code)
                self.assertEqual(exc.exception.status_code, status)
                self.assertNotIn(self.fake_key, str(exc.exception))
                self.assertIn("[REDACTED]", exc.exception.detail)
                self.assertEqual(len(seen), 1)

    async def test_redirect_never_forwards_credentials(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(307, headers={"location": "https://evil.invalid/collect"})

        with patch("src.tts.breezeblue.httpx.AsyncClient", side_effect=ClientMock(handler)):
            with self.assertRaises(BreezeBlueTTSError) as exc:
                await self.adapter().synth_pcm("你好", self.preset)
        self.assertEqual(exc.exception.code, "redirect")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].url.host, "api.breeze.blue")

    async def test_json_html_wav_empty_and_partial_samples_are_rejected(self):
        responses = [httpx.Response(200, json={"detail": "unexpected " + self.fake_key}),
                     httpx.Response(202, json={"job_id": "async-job"}),
                     httpx.Response(200, content=b"<html>error</html>", headers={"content-type": "text/html"}),
                     httpx.Response(200, content=b'{}', headers={"content-type": "audio/pcm"}),
                     httpx.Response(200, content=b"<html>error</html>", headers={"content-type": "audio/pcm"}),
                     httpx.Response(200, content=b"RIFF1234WAVEdata", headers={"content-type": "audio/pcm"}),
                     httpx.Response(200, content=b"", headers={"content-type": "audio/pcm"}),
                     httpx.Response(200, content=b"\x00", headers={"content-type": "audio/pcm"}),
                     httpx.Response(200, content=self.pcm, headers={"content-type": "audio/wav"})]
        for response in responses:
            with self.subTest(status=response.status_code, content=response.content[:12]), \
                    patch("src.tts.breezeblue.httpx.AsyncClient",
                          side_effect=ClientMock(lambda request: response)):
                with self.assertRaises(BreezeBlueTTSError) as exc:
                    await self.adapter().synth_pcm("你好", self.preset)
                self.assertNotIn(self.fake_key, str(exc.exception))

    async def test_transport_error_redaction_has_no_sensitive_exception_chain(self):
        for error_type, code in [(httpx.ConnectError, "connection"), (httpx.ReadTimeout, "timeout")]:
            seen = []

            def handler(request):
                seen.append(request)
                raise error_type("echoed sensitive key: " + self.fake_key, request=request)

            with self.subTest(error=code), patch("src.tts.breezeblue.httpx.AsyncClient",
                                               side_effect=ClientMock(handler)):
                with self.assertRaises(BreezeBlueTTSError) as exc:
                    await self.adapter().synth_pcm("你好", self.preset)
                self.assertEqual(exc.exception.code, code)
                self.assertNotIn(self.fake_key, str(exc.exception))
                self.assertTrue(exc.exception.__suppress_context__)
                self.assertEqual(len(seen), 1)

    async def test_cancelled_request_is_drained_before_second_request(self):
        first_started, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def handler(request):
            requests.append(request)
            if len(requests) == 1:
                first_started.set()
                await release.wait()
            return httpx.Response(200, content=self.pcm, headers={"content-type": "audio/pcm"})

        adapter = self.adapter()
        with patch("src.tts.breezeblue.httpx.AsyncClient", side_effect=ClientMock(handler)):
            first = asyncio.create_task(adapter.synth_pcm("一", self.preset))
            await asyncio.wait_for(first_started.wait(), 1)
            first.cancel()
            second = asyncio.create_task(adapter.synth_pcm("二", self.preset))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(first.done())
            self.assertEqual(len(requests), 1)
            first.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual(await asyncio.wait_for(second, 1), self.pcm)
            self.assertEqual(len(requests), 2)

    async def test_missing_key_and_invalid_voice_settings_make_no_request(self):
        with patch("src.tts.breezeblue.httpx.AsyncClient") as client:
            self.key_file.unlink()
            with self.assertRaises(BreezeBlueTTSError) as exc:
                await self.adapter().synth_pcm("你好", self.preset)
            self.assertEqual(exc.exception.code, "config")
            self.key_file.write_text(self.fake_key)
            with self.assertRaises(BreezeBlueTTSError) as exc:
                await self.adapter().synth_pcm("你好", {"voice_id": "voc_fake", "params": {
                    "voice_settings": {"guidance_scale": 0}}})
            self.assertEqual(exc.exception.code, "preset")
            client.assert_not_called()


class OfficialVoicePresets(unittest.TestCase):
    def test_only_official_marker_is_loaded_without_credential_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for name, marker, contents in [
                ("official", "breezeblue.json", {"label": "Official test", "voice_id": "voc_test",
                                                "params": {"instructions": "轻声说"}}),
                ("local", "breeze.json", {"kind": "design", "params": {"instruction": "轻声说"}}),
                ("omni", "design.json", {"gender": "Female"}),
                ("wrong", "breezeblue.json", {"voice_id": "voc_wrong", "api_key": "fake-not-real"}),
            ]:
                directory = base / name
                directory.mkdir()
                (directory / marker).write_text(json.dumps(contents), encoding="utf-8")
            presets = scan_voices(tmp)
            self.assertEqual(list(presets), ["official"])
            self.assertEqual(presets["official"], {"name": "official", "label": "Official test",
                                                  "voice_id": "voc_test", "kind": "saved",
                                                  "provider": "breezeblue", "params": {
                                                      "model_id": "breeze-tts-2", "language_code": "zh",
                                                      "instructions": "轻声说"}})


if __name__ == "__main__":
    unittest.main()
