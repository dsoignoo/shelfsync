"""ElevenLabs Scribe transcription for short audiobook calibration clips."""

import asyncio
import json
import math
import os
from pathlib import Path

import httpx


class ElevenLabsError(RuntimeError):
    """A safe-to-display ElevenLabs configuration or transcription failure."""


def _error_detail(response: httpx.Response, api_key: str) -> str:
    try:
        payload = response.json()
        detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("status") or json.dumps(detail)
        elif not isinstance(detail, str):
            detail = json.dumps(detail)
    except ValueError:
        detail = "No error detail returned by provider."
    return detail.replace(api_key, "[redacted]").strip()[:500]


async def transcribe_segment(
    client: httpx.AsyncClient, audio_path: Path, language: str,
) -> str | None:
    """Transcribe with Scribe v2; return None only for completed empty speech.

    The deadline covers file reading and the synchronous multipart API request.
    Caller cancellation propagates. No requests are retried or redirected.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        raise ElevenLabsError("Set ELEVENLABS_API_KEY to enable audio calibration.")
    base_url = os.environ.get("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io").rstrip("/")
    try:
        timeout = float(os.environ.get("ELEVENLABS_TIMEOUT_SECONDS", "180"))
    except ValueError:
        raise ElevenLabsError("ELEVENLABS_TIMEOUT_SECONDS must be a finite positive number.") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ElevenLabsError("ELEVENLABS_TIMEOUT_SECONDS must be a finite positive number.")

    try:
        async with asyncio.timeout(timeout):
            try:
                audio = await asyncio.to_thread(audio_path.read_bytes)
            except OSError:
                raise ElevenLabsError("Cannot read the local audio clip for ElevenLabs upload.") from None
            try:
                response = await client.post(
                    f"{base_url}/v1/speech-to-text",
                    headers={"xi-api-key": api_key},
                    data={
                        "model_id": "scribe_v2",
                        "language_code": language,
                        "tag_audio_events": "false",
                        "diarize": "false",
                        "webhook": "false",
                    },
                    files={"file": (audio_path.name, audio, "application/octet-stream")},
                    timeout=timeout,
                    follow_redirects=False,
                )
            except httpx.TimeoutException:
                raise TimeoutError("ElevenLabs transcription timed out.") from None
            except (httpx.RequestError, httpx.InvalidURL):
                raise ElevenLabsError(
                    "Cannot reach ElevenLabs; check ELEVENLABS_BASE_URL and network connectivity."
                ) from None
            if response.status_code != 200:
                hint = " Check ELEVENLABS_API_KEY and Speech to Text permissions." if response.status_code in (401, 403) else ""
                raise ElevenLabsError(
                    f"ElevenLabs transcription failed (HTTP {response.status_code}): "
                    + _error_detail(response, api_key) + hint
                )
            try:
                result = response.json()
            except ValueError:
                raise ElevenLabsError("ElevenLabs returned invalid JSON.") from None
            if not isinstance(result, dict) or not isinstance(result.get("text"), str):
                raise ElevenLabsError("ElevenLabs did not return a final transcript text.")
            return result["text"].strip() or None
    except TimeoutError:
        raise TimeoutError(
            f"ElevenLabs transcription timed out (overall limit {timeout:g} seconds)."
        ) from None
