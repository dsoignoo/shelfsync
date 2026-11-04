import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import app
from assemblyai_provider import AssemblyAIError


class AudioSyncTests(unittest.TestCase):
    def test_failed_recalibration_preserves_previous_anchors(self):
        old = [{"audio_seconds": 15, "char_position": 30, "confidence": 0.9}]
        for failure, status in ((AssemblyAIError("Provider unavailable"), 502),
                                (TimeoutError(), 504), (None, 422)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                data = Path(directory)
                (data / "calibration.json").write_text(json.dumps({
                    "book": {"audio_anchors": old, "kindle_pages": 200},
                }))
                transcripts = AsyncMock(side_effect=failure) if failure else AsyncMock(return_value=[None] * 3)
                with patch.object(app, "DATA_DIR", data), \
                     patch.object(app, "ASSEMBLYAI_API_KEY", "test-key"), \
                     patch.object(app.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                     patch.object(app, "get_epub_chapters", AsyncMock(return_value=(
                         [{"text": "A letter arrived at the lighthouse."}], [], 60))), \
                     patch.object(app, "get_item_details", AsyncMock(return_value={
                         "media": {"audioFiles": [{"index": 0, "duration": 60}]}})), \
                     patch.object(app, "_extract_audio_segment", AsyncMock(return_value=data / "audio.mp3")), \
                     patch.object(app, "_transcribe_all", transcripts), \
                     patch.object(app, "_cleanup_audio_temp"), \
                     TestClient(app.app) as client:
                    response = client.post('/api/audio-sync/book', json={
                        "n_samples": 3, "language": "en", "force": True,
                    })
                    self.assertEqual(response.status_code, status)
                    calibration = client.get('/api/calibration/book').json()
                    self.assertTrue(calibration['has_audio_sync'])
                saved = json.loads((data / "calibration.json").read_text())['book']
                self.assertEqual(saved['audio_anchors'], old)
                self.assertEqual(saved['kindle_pages'], 200)


class AudioBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_failure_cancels_other_uploads_before_returning(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def transcribe(client, path, language):
            if path.name == 'fail.mp3':
                await started.wait()
                raise AssemblyAIError("Provider unavailable")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(app, "_client", return_value=object()), \
             patch.object(app, "transcribe_segment", side_effect=transcribe):
            with self.assertRaises(AssemblyAIError):
                await app._transcribe_all([Path('fail.mp3'), Path('waiting.mp3')], 'en')
        self.assertTrue(cancelled.is_set())


if __name__ == '__main__':
    unittest.main()
