"""Contract tests for the real gateway schema, using no remote synthesis calls."""
import asyncio
import io
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave

import httpx

from src.tts.breeze import BreezeTTS, BreezeTTSError, scan_voices


def make_wav(rate=24000, channels=1, frames=2400):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(struct.pack("<h", 3000) * channels * frames)
    return buf.getvalue()


class ClientMock:
    def __init__(self, handler):
        self.handler = handler
        self.real_client = httpx.AsyncClient
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.real_client(transport=httpx.MockTransport(self.handler), **kwargs)


class BreezeTests(unittest.IsolatedAsyncioTestCase):
    async def test_design_request_and_base_update(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=make_wav(), headers={"content-type": "audio/wav"})

        client_mock = ClientMock(handler)
        tts = BreezeTTS("http://127.0.0.1:17860/")
        preset = {"kind": "design", "params": {"instruction": "清亮少女音", "seed": 202,
                                                 "guidance_scale": 1.0, "do_split": False}}
        with patch("src.tts.breeze.httpx.AsyncClient", side_effect=client_mock), \
                patch("src.tts.breeze.audio.decode_to_pcm", return_value=b"\x00\x00" * 2400):
            result = await tts.synth_pcm("你好", preset)
            self.assertEqual(len(result), 4800)
            tts.cfg_base = "http://127.0.0.1:27860/"
            self.assertEqual(tts.active_base(), "http://127.0.0.1:27860")
            await tts.synth_pcm("第二句", preset)
        self.assertEqual(str(requests[0].url), "http://127.0.0.1:17860/api/tts/design")
        payload = json.loads(requests[0].content)
        self.assertEqual(payload, {"text": "你好", "instruction": "清亮少女音", "language": "Chinese",
                                  "model": "breeze-tts-2", "seed": 202, "guidance_scale": 1.0,
                                  "do_split": False})
        self.assertEqual(requests[1].url.port, 27860)
        self.assertTrue(all(call["trust_env"] is False for call in client_mock.calls))

    async def test_clone_upload_sends_audio_bytes_not_local_path(self):
        requests = []

        async def handler(request):
            await request.aread()
            requests.append(request)
            return httpx.Response(200, content=make_wav())

        with tempfile.TemporaryDirectory() as tmp:
            ref = Path(tmp) / "reference.wav"
            ref.write_bytes(make_wav(frames=123))
            tts = BreezeTTS("http://127.0.0.1:17860")
            with patch("src.tts.breeze.httpx.AsyncClient", side_effect=ClientMock(handler)), \
                    patch("src.tts.breeze.audio.decode_to_pcm", return_value=b"\x00\x00"):
                await tts.synth_pcm("新文本", {"kind": "clone", "ref_audio": str(ref),
                                            "ref_text": "参考原文", "params": {"seed": 202,
                                                                            "do_split": False}})
            request = requests[0]
            self.assertEqual(request.url.path, "/api/tts/clone/upload")
            self.assertIn("multipart/form-data", request.headers["content-type"])
            self.assertIn(b'name="voice_ref"; filename="reference.wav"', request.content)
            self.assertIn(ref.read_bytes(), request.content)
            self.assertIn("参考原文".encode(), request.content)
            self.assertIn(b'name="do_split"\r\n\r\nfalse', request.content)
            self.assertNotIn(tmp.encode(), request.content)

    async def test_errors_are_structured_and_never_retried(self):
        scenarios = [(httpx.Response(422, json={"detail": "instruction required"}), "http", 422),
                     (httpx.Response(200, json={"error": "model not loaded"}), "response", 200),
                     (httpx.Response(200, content=b"<html>error</html>"), "response", 200),
                     (httpx.Response(200, content=b""), "response", 200)]
        for response, code, status in scenarios:
            calls = []

            def handler(request):
                calls.append(request)
                return response

            with self.subTest(code=code, status=status), \
                    patch("src.tts.breeze.httpx.AsyncClient", side_effect=ClientMock(handler)):
                with self.assertRaises(BreezeTTSError) as exc:
                    await BreezeTTS("http://127.0.0.1:17860").synth_pcm(
                        "你好", {"kind": "design", "params": {"instruction": "声音"}})
                self.assertEqual(exc.exception.code, code)
                self.assertEqual(exc.exception.status_code, status)
                self.assertTrue(exc.exception.detail)
                self.assertEqual(len(calls), 1)

    async def test_timeout_has_no_automatic_retry(self):
        calls = []

        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout("test", request=request)

        with patch("src.tts.breeze.httpx.AsyncClient", side_effect=ClientMock(handler)):
            with self.assertRaises(BreezeTTSError) as exc:
                await BreezeTTS("http://127.0.0.1:17860").synth_pcm(
                    "你好", {"kind": "design", "params": {"instruction": "声音"}})
        self.assertEqual(exc.exception.code, "timeout")
        self.assertEqual(len(calls), 1)

    async def test_bad_audio_reports_decode_error(self):
        with patch("src.tts.breeze.httpx.AsyncClient",
                   side_effect=ClientMock(lambda request: httpx.Response(200, content=make_wav()))), \
                patch("src.tts.breeze.audio.decode_to_pcm", side_effect=RuntimeError("invalid wav")):
            with self.assertRaises(BreezeTTSError) as exc:
                await BreezeTTS("http://127.0.0.1:17860").synth_pcm(
                    "你好", {"kind": "design", "params": {"instruction": "声音"}})
        self.assertEqual(exc.exception.code, "audio")
        self.assertIn("invalid wav", exc.exception.detail)

    async def test_blank_text_never_calls_api(self):
        with patch("src.tts.breeze.httpx.AsyncClient") as client:
            self.assertEqual(await BreezeTTS("").synth_pcm("  ", {}), b"")
            client.assert_not_called()

    async def test_cancellation_drains_request_before_next_synthesis(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def handler(request):
            calls.append(request)
            if len(calls) == 1:
                started.set()
                await release.wait()
            return httpx.Response(200, content=make_wav())

        preset = {"kind": "design", "params": {"instruction": "声音"}}
        tts = BreezeTTS("http://127.0.0.1:17860")
        with patch("src.tts.breeze.httpx.AsyncClient", side_effect=ClientMock(handler)), \
                patch("src.tts.breeze.audio.decode_to_pcm", return_value=b"\x00\x00"):
            first = asyncio.create_task(tts.synth_pcm("一", preset))
            await asyncio.wait_for(started.wait(), 1)
            first.cancel()
            second = asyncio.create_task(tts.synth_pcm("二", preset))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(first.done())
            self.assertEqual(len(calls), 1)
            first.cancel()  # Repeated cancellation must not break the drain.
            await asyncio.sleep(0)
            self.assertEqual(len(calls), 1)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual(await asyncio.wait_for(second, 2), b"\x00\x00")
            self.assertEqual(len(calls), 2)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for PCM normalization")
    async def test_real_decoder_normalizes_stereo_48k_to_mono_24k_pcm(self):
        source = make_wav(rate=48000, channels=2, frames=4800)
        with patch("src.tts.breeze.httpx.AsyncClient",
                   side_effect=ClientMock(lambda request: httpx.Response(200, content=source))):
            pcm = await BreezeTTS("http://127.0.0.1:17860").synth_pcm(
                "你好", {"kind": "design", "params": {"instruction": "声音"}})
        self.assertEqual(len(pcm), 4800)  # .1 sec * 24k samples * int16 mono
        self.assertGreater(max(struct.unpack("<2400h", pcm)), 2900)
        self.assertFalse(pcm.startswith(b"RIFF"))


class PresetTests(unittest.TestCase):
    def test_only_breeze_marker_is_loaded_with_relative_audio_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            clone = base / "breeze02"
            clone.mkdir()
            (clone / "ref_audio.wav").write_bytes(make_wav())
            (clone / "breeze.json").write_text(json.dumps(
                {"label": "Breeze02", "kind": "clone", "ref_audio": "ref_audio.wav",
                 "ref_text": "参考文本", "params": {"seed": 202}}), encoding="utf-8")
            omni = base / "omni"
            omni.mkdir()
            (omni / "design.json").write_text('{"gender":"Female"}')
            broken = base / "bad"
            broken.mkdir()
            (broken / "breeze.json").write_text('{"kind":"design","params":{}}')
            voices = scan_voices(tmp)
            self.assertEqual(list(voices), ["breeze02"])
            self.assertEqual(voices["breeze02"]["ref_audio"], str((clone / "ref_audio.wav").resolve()))
            self.assertEqual(voices["breeze02"]["provider"], "breeze")


if __name__ == "__main__":
    unittest.main()
