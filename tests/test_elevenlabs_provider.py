import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from elevenlabs_provider import ElevenLabsError, transcribe_segment


class ElevenLabsProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "ELEVENLABS_API_KEY": "test-secret-key",
            "ELEVENLABS_BASE_URL": "https://elevenlabs.example",
            "ELEVENLABS_TIMEOUT_SECONDS": "180",
        })
        environment.start()
        self.addCleanup(environment.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.audio_path = Path(directory.name) / "clip.mp3"
        self.audio_path.write_bytes(b"short audio fixture")

    async def _transcribe(self, response):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
            return await transcribe_segment(client, self.audio_path, "en")

    async def test_completed_empty_text_is_silence(self):
        self.assertIsNone(await self._transcribe(httpx.Response(200, json={"text": " \n"})))

    async def test_provider_errors_are_not_empty_success_and_never_expose_key(self):
        for status in (401, 403, 422, 429, 500):
            with self.subTest(status=status):
                with self.assertRaises(ElevenLabsError) as raised:
                    await self._transcribe(httpx.Response(status, json={
                        "detail": {"status": "provider_error", "message": "Rejected test-secret-key"},
                    }))
                self.assertNotIn("test-secret-key", str(raised.exception))

    async def test_missing_invalid_or_async_transcript_is_not_silence(self):
        responses = [
            httpx.Response(200, json={}),
            httpx.Response(200, json={"text": None}),
            httpx.Response(200, json={"text": []}),
            httpx.Response(200, json=[]),
            httpx.Response(200, text="not json"),
            httpx.Response(202, json={"request_id": "queued"}),
        ]
        for response in responses:
            with self.subTest(response=response):
                with self.assertRaises(ElevenLabsError):
                    await self._transcribe(response)

    async def test_redirect_does_not_forward_credentials(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(307, headers={"Location": "https://other.example/upload"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
            with self.assertRaises(ElevenLabsError):
                await transcribe_segment(client, self.audio_path, "en")
        self.assertEqual(len(requests), 1)

    async def test_deadline_cancels_inflight_request(self):
        cancelled = asyncio.Event()

        async def handle(request):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.dict(os.environ, {"ELEVENLABS_TIMEOUT_SECONDS": "0.1"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                with self.assertRaises(TimeoutError):
                    await transcribe_segment(client, self.audio_path, "en")
        self.assertTrue(cancelled.is_set())

    async def test_caller_cancellation_propagates(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def handle(request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            task = asyncio.create_task(transcribe_segment(client, self.audio_path, "en"))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(cancelled.is_set())


if __name__ == "__main__":
    unittest.main()
