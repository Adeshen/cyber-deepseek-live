"""Streaming contracts and cancellation, without real API keys or generations."""
import asyncio
from contextlib import aclosing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

import httpx

from src.tts.breezeblue import BreezeBlueTTS, BreezeBlueTTSError, OFFICIAL_BASE


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class ClientMock:
    def __init__(self, handler):
        self.handler = handler
        self.real_client = httpx.AsyncClient
        self.options = []

    def __call__(self, **kwargs):
        self.options.append(kwargs)
        return self.real_client(transport=httpx.MockTransport(self.handler), **kwargs)


class StreamingContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key_file = Path(self.tmp.name) / "fake.key"
        self.key = "fake-STREAM-secret+/only"
        self.key_file.write_text(self.key)
        self.preset = {"voice_id": "voc_stream_test", "params": {
            "model_id": "breeze-tts-2", "language_code": "zh"}}
        self.pcm = b"\x00\x00\x01\x00\xff\xff" * 16
        self.adapter = BreezeBlueTTS(OFFICIAL_BASE, str(self.key_file))

    async def collect(self):
        async with aclosing(self.adapter.stream_pcm("来了", self.preset)) as stream:
            return b"".join([chunk async for chunk in stream])

    async def test_request_contract_and_odd_network_boundaries_preserve_samples(self):
        seen = []
        body = Chunks([self.pcm[:1], self.pcm[1:19], self.pcm[19:50], self.pcm[50:]])

        def handler(request):
            seen.append(request)
            return httpx.Response(200, stream=body, headers={"content-type": "audio/pcm; rate=24000"})

        client = ClientMock(handler)
        delivered = []
        with patch("src.tts.breezeblue.httpx.AsyncClient", side_effect=client):
            async for chunk in self.adapter.stream_pcm("来了", self.preset):
                delivered.append(chunk)
        self.assertEqual(b"".join(delivered), self.pcm)
        self.assertTrue(all(len(chunk) % 2 == 0 for chunk in delivered))
        self.assertTrue(body.closed)
        self.assertEqual(len(seen), 1)
        request = seen[0]
        self.assertEqual(str(request.url), OFFICIAL_BASE + "/text-to-speech/voc_stream_test/stream?output_format=pcm")
        self.assertEqual(request.headers["xi-api-key"], self.key)
        self.assertEqual(json.loads(request.content), {"text": "来了", "model_id": "breeze-tts-2", "language_code": "zh"})
        self.assertIs(client.options[0]["follow_redirects"], False)
        self.assertIs(client.options[0]["trust_env"], False)

    async def test_first_pcm_is_delivered_before_response_finishes(self):
        release, eof = asyncio.Event(), asyncio.Event()
        pcm = self.pcm

        class SlowTail(Chunks):
            async def __aiter__(self):
                yield pcm[:32]
                await release.wait()
                yield pcm[32:]
                eof.set()

        body = SlowTail([])
        client = ClientMock(lambda request: httpx.Response(200, stream=body, headers={"content-type": "audio/pcm"}))
        with patch("src.tts.breezeblue.httpx.AsyncClient", side_effect=client):
            async with aclosing(self.adapter.stream_pcm("来了", self.preset)) as stream:
                self.assertEqual(await asyncio.wait_for(anext(stream), 1), pcm[:32])
                self.assertFalse(eof.is_set())
                release.set()
                remainder = b"".join([chunk async for chunk in stream])
        self.assertEqual(remainder, pcm[32:])
        self.assertTrue(eof.is_set())

    async def test_aclose_discards_full_queue_and_holds_lock_until_upstream_eof(self):
        queued, draining, release, eof = (asyncio.Event() for _ in range(4))
        requests = []
        pcm = self.pcm

        class BlockedTail(Chunks):
            async def __aiter__(self):
                for i in range(24):
                    if i == 9:
                        queued.set()  # One consumed + eight queued, then put blocks.
                    yield pcm
                draining.set()
                await release.wait()
                eof.set()

        body = BlockedTail([])

        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, stream=body, headers={"content-type": "audio/pcm"})
            self.assertTrue(eof.is_set(), "A subsequent request overlapped upstream inference")
            return httpx.Response(200, content=pcm, headers={"content-type": "audio/pcm"})

        with patch("src.tts.breezeblue.httpx.AsyncClient", side_effect=ClientMock(handler)):
            stream = self.adapter.stream_pcm("一", self.preset)
            self.assertEqual(await anext(stream), pcm)
            await asyncio.wait_for(queued.wait(), 1)
            close = asyncio.create_task(stream.aclose())
            await asyncio.wait_for(draining.wait(), 1)
            second = asyncio.create_task(self.adapter.synth_pcm("二", self.preset))
            await asyncio.sleep(0)
            self.assertEqual(len(requests), 1)
            self.assertFalse(close.done())
            release.set()
            await asyncio.wait_for(close, 1)
            self.assertEqual(await asyncio.wait_for(second, 1), pcm)
        self.assertTrue(body.closed)
        self.assertEqual(len(requests), 2)

    async def test_cancelled_waiter_drains_repeated_cancellation_without_overlap(self):
        started, release, eof = asyncio.Event(), asyncio.Event(), asyncio.Event()
        requests = []
        pcm = self.pcm

        class PendingAudio(Chunks):
            async def __aiter__(self):
                started.set()
                await release.wait()
                yield pcm
                eof.set()

        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, stream=PendingAudio([]), headers={"content-type": "audio/pcm"})
            self.assertTrue(eof.is_set())
            return httpx.Response(200, content=pcm, headers={"content-type": "audio/pcm"})

        with patch("src.tts.breezeblue.httpx.AsyncClient", side_effect=ClientMock(handler)):
            first = asyncio.create_task(self.collect())
            await asyncio.wait_for(started.wait(), 1)
            first.cancel()
            await asyncio.sleep(0)
            second = asyncio.create_task(self.adapter.synth_pcm("二", self.preset))
            first.cancel()
            await asyncio.sleep(0)
            self.assertEqual(len(requests), 1)
            self.assertFalse(first.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(first, 1)
            self.assertEqual(await asyncio.wait_for(second, 1), pcm)

    async def test_split_non_pcm_prefixes_and_odd_tail_are_rejected(self):
        invalid = [b'{}', b'{"detail": "not really PCM"}', b'[1, 2, 3]',
                   b"RIFF1234WAVEdataaaa", b"ID3notrawpcm!", b"<html>error</html>",
                   b"", self.pcm + b"\x01"]
        for content in invalid:
            body = Chunks([content[:1], content[1:5], content[5:]])
            client = ClientMock(lambda request: httpx.Response(200, stream=body, headers={"content-type": "audio/pcm"}))
            with self.subTest(prefix=content[:12]), patch("src.tts.breezeblue.httpx.AsyncClient", side_effect=client):
                with self.assertRaises(BreezeBlueTTSError) as exc:
                    await self.collect()
            self.assertEqual(exc.exception.code, "audio")
            self.assertNotIn(self.key, str(exc.exception))

    async def test_invalid_container_is_discarded_and_drained_before_error(self):
        waiting, release, eof = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class InvalidContainer(Chunks):
            async def __aiter__(self):
                yield b"RIFF1234WAVEdata"
                waiting.set()
                await release.wait()
                yield b"unused container payload"
                eof.set()

        body = InvalidContainer([])
        client = ClientMock(lambda request: httpx.Response(200, stream=body, headers={"content-type": "audio/pcm"}))
        with patch("src.tts.breezeblue.httpx.AsyncClient", side_effect=client):
            read = asyncio.create_task(self.collect())
            await asyncio.wait_for(waiting.wait(), 1)
            self.assertFalse(read.done(), "A container prefix must never be yielded as PCM")
            release.set()
            with self.assertRaises(BreezeBlueTTSError):
                await asyncio.wait_for(read, 1)
        self.assertTrue(eof.is_set())
        self.assertTrue(body.closed)

    async def test_http_and_mime_errors_are_redacted_without_retries(self):
        for status, mime, code in [(401, "application/json", "auth"),
                                   (429, "application/json", "rate_limit"),
                                   (503, "application/json", "http"),
                                   (200, "application/json", "response")]:
            seen = []
            error = json.dumps({"detail": self.key + " / " + quote(self.key, safe="")}).encode()

            def handler(request):
                seen.append(request)
                return httpx.Response(status, stream=Chunks([error[:2], error[2:]]), headers={"content-type": mime})

            with self.subTest(status=status), patch("src.tts.breezeblue.httpx.AsyncClient", side_effect=ClientMock(handler)):
                with self.assertRaises(BreezeBlueTTSError) as exc:
                    await self.collect()
            self.assertEqual(exc.exception.code, code)
            self.assertNotIn(self.key, str(exc.exception))
            self.assertNotIn(quote(self.key, safe=""), str(exc.exception))
            self.assertIn("[REDACTED]", str(exc.exception))
            self.assertEqual(len(seen), 1)

    async def test_redirect_and_transport_errors_do_not_expose_headers_or_chain(self):
        for kind in ("redirect", "connection", "timeout", "unexpected"):
            seen = []

            def handler(request):
                seen.append(request)
                if kind == "redirect":
                    return httpx.Response(307, headers={"location": "https://evil.invalid/collect"})
                if kind == "connection":
                    raise httpx.ConnectError(self.key, request=request)
                if kind == "timeout":
                    raise httpx.ReadTimeout(self.key, request=request)
                raise RuntimeError(self.key)

            with self.subTest(kind=kind), patch("src.tts.breezeblue.httpx.AsyncClient", side_effect=ClientMock(handler)):
                with self.assertRaises(BreezeBlueTTSError) as exc:
                    await self.collect()
            self.assertEqual(exc.exception.code, "response" if kind == "unexpected" else kind)
            self.assertNotIn(self.key, str(exc.exception))
            self.assertTrue(exc.exception.__suppress_context__)
            self.assertEqual(len(seen), 1)

    async def test_empty_text_does_not_generate_and_invalid_settings_fail_before_request(self):
        with patch("src.tts.breezeblue.httpx.AsyncClient") as client:
            self.assertEqual([chunk async for chunk in self.adapter.stream_pcm(" ", self.preset)], [])
            self.adapter.timeout_sec = float("nan")
            with self.assertRaises(BreezeBlueTTSError) as exc:
                await self.collect()
            self.assertEqual(exc.exception.code, "config")
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
