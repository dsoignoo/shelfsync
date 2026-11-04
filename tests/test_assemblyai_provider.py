import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import wave

import httpx

from assemblyai_provider import AssemblyAIError, transcribe_segment


class AssemblyAIProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "ASSEMBLYAI_API_KEY": "test-secret-key",
            "ASSEMBLYAI_BASE_URL": "https://assemblyai.example/",
            "ASSEMBLYAI_TIMEOUT_SECONDS": "180",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.audio_path = Path(self.directory.name) / "segment.wav"
        with wave.open(str(self.audio_path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * 1600)

    async def _transcribe(self, responses, language="de", requests=None):
        pending = iter(responses)

        async def handle(request):
            if requests is not None:
                requests.append(request)
            return next(pending)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await transcribe_segment(client, self.audio_path, language)

    @staticmethod
    def _upload():
        return httpx.Response(200, json={"upload_url": "https://cdn.example/audio"})

    @staticmethod
    def _transcript(status="queued", **fields):
        return httpx.Response(200, json={"id": "job-123", "status": status, **fields})

    async def test_queued_processing_completed(self):
        with patch("assemblyai_provider.asyncio.sleep", new_callable=AsyncMock):
            text = await self._transcribe([
                self._upload(), self._transcript(),
                self._transcript("processing"),
                self._transcript("completed", text="  A finished passage.  "),
            ])
        self.assertEqual(text, "A finished passage.")

    async def test_completed_empty_text_is_no_speech_not_an_error(self):
        for text in (None, "", " \n "):
            with self.subTest(text=text):
                result = await self._transcribe([
                    self._upload(), self._transcript("completed", text=text),
                ])
                self.assertIsNone(result)

    async def test_terminal_provider_error_is_raised_and_key_is_redacted(self):
        with patch("assemblyai_provider.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(AssemblyAIError) as raised:
                await self._transcribe([
                    self._upload(), self._transcript("processing"),
                    self._transcript("error", error="Cannot decode audio: test-secret-key"),
                ])
        self.assertIn("Cannot decode audio", str(raised.exception))
        self.assertNotIn("test-secret-key", str(raised.exception))

    async def test_auth_submission_and_poll_errors_never_become_empty_success(self):
        cases = [
            ([httpx.Response(401, json={"error": "test-secret-key"})], "upload", "401"),
            ([self._upload(), httpx.Response(400, json={"error": "Unsupported language code zz"})],
             "submission", "Unsupported language"),
            ([self._upload(), self._transcript(), httpx.Response(403, text="test-secret-key")],
             "polling", "403"),
            ([self._upload(), self._transcript(), httpx.Response(429, json={"error": "Too many requests"})],
             "polling", "429"),
        ]
        for responses, operation, detail in cases:
            with self.subTest(operation=operation, detail=detail):
                with patch("assemblyai_provider.asyncio.sleep", new_callable=AsyncMock):
                    with self.assertRaises(AssemblyAIError) as raised:
                        await self._transcribe(responses, language="zz")
                self.assertNotIn("test-secret-key", str(raised.exception))

    async def test_invalid_transcript_is_not_treated_as_no_speech(self):
        for payload in (
            {"id": "job-123", "status": "completed"},
            {"id": "job-123", "status": "completed", "text": ["not text"]},
            {"id": "job-123", "status": "unexpected"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(AssemblyAIError):
                    await self._transcribe([self._upload(), httpx.Response(200, json=payload)])

    async def test_overall_deadline_bounds_each_http_stage(self):
        for blocked_stage in ("upload", "submission", "polling"):
            with self.subTest(stage=blocked_stage):
                os.environ["ASSEMBLYAI_TIMEOUT_SECONDS"] = "0.02"
                stages = iter(("upload", "submission", "polling"))
                reached = []

                async def handle(request):
                    stage = next(stages)
                    reached.append(stage)
                    if stage == blocked_stage:
                        await asyncio.Event().wait()
                    return self._upload() if stage == "upload" else self._transcript()

                with patch("assemblyai_provider.asyncio.sleep", new_callable=AsyncMock):
                    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                        with self.assertRaises(TimeoutError):
                            await transcribe_segment(client, self.audio_path, "de")
                self.assertEqual(reached[-1], blocked_stage)

    async def test_overall_deadline_includes_polling_sleep(self):
        os.environ["ASSEMBLYAI_TIMEOUT_SECONDS"] = "0.02"
        requests = []
        with self.assertRaises(TimeoutError):
            await self._transcribe([self._upload(), self._transcript()], requests=requests)
        self.assertEqual(len(requests), 2)

    async def test_cancellation_propagates_during_upload_and_polling(self):
        for blocked_stage in ("upload", "polling"):
            with self.subTest(stage=blocked_stage):
                reached = asyncio.Event()
                stages = iter(("upload", "submission", "polling"))

                async def handle(request):
                    stage = next(stages)
                    if stage == blocked_stage:
                        reached.set()
                        await asyncio.Event().wait()
                    return self._upload() if stage == "upload" else self._transcript()

                with patch("assemblyai_provider.asyncio.sleep", new_callable=AsyncMock):
                    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                        task = asyncio.create_task(transcribe_segment(client, self.audio_path, "de"))
                        try:
                            await asyncio.wait_for(reached.wait(), timeout=2)
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                        finally:
                            if not task.done():
                                task.cancel()
                                try:
                                    await task
                                except asyncio.CancelledError:
                                    pass


if __name__ == "__main__":
    unittest.main()
