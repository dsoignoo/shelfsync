"""AssemblyAI pre-recorded transcription for short calibration audio segments."""

import asyncio
import math
import os
from pathlib import Path
from urllib.parse import quote

import httpx


class AssemblyAIError(RuntimeError):
    """AssemblyAI configuration, transport, or transcription failure."""


def _error_detail(value: object, api_key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return "No error detail returned by provider."
    return value.replace(api_key, "[redacted]").strip()[:500]


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    api_key: str,
    operation: str,
    timeout: float,
    **kwargs,
) -> dict:
    headers = {"authorization": api_key, **kwargs.pop("headers", {})}
    try:
        response = await client.request(
            method, url, headers=headers, timeout=timeout,
            follow_redirects=False, **kwargs,
        )
    except httpx.TimeoutException:
        raise TimeoutError(f"AssemblyAI {operation} timed out.") from None
    except (httpx.RequestError, httpx.InvalidURL):
        raise AssemblyAIError(
            f"AssemblyAI {operation} could not reach the provider; "
            "check ASSEMBLYAI_BASE_URL and network connectivity."
        ) from None

    if not response.is_success:
        if response.status_code in (401, 403):
            detail = "Check ASSEMBLYAI_API_KEY and the account/project permissions."
        else:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            detail = _error_detail(
                payload.get("error") if isinstance(payload, dict) else response.text,
                api_key,
            )
        raise AssemblyAIError(
            f"AssemblyAI {operation} failed (HTTP {response.status_code}): {detail}"
        )

    try:
        payload = response.json()
    except ValueError:
        raise AssemblyAIError(
            f"AssemblyAI {operation} returned invalid JSON."
        ) from None
    if not isinstance(payload, dict):
        raise AssemblyAIError(f"AssemblyAI {operation} returned an invalid response.")
    return payload


async def transcribe_segment(
    client: httpx.AsyncClient, audio_path: Path, language: str,
) -> str | None:
    """Upload and transcribe audio; return None only for completed empty text.

    ASSEMBLYAI_TIMEOUT_SECONDS (default 180) bounds the entire operation,
    including local file reading, upload, submission, polling and sleeps.
    Timeouts raise TimeoutError; caller cancellation is never swallowed.
    Other failures raise AssemblyAIError. No requests are retried.
    """
    api_key = os.environ.get("ASSEMBLYAI_API_KEY", "").strip()
    if not api_key:
        raise AssemblyAIError("Set ASSEMBLYAI_API_KEY to enable audio calibration.")
    base_url = os.environ.get(
        "ASSEMBLYAI_BASE_URL", "https://api.assemblyai.com",
    ).rstrip("/")
    try:
        timeout = float(os.environ.get("ASSEMBLYAI_TIMEOUT_SECONDS", "180"))
    except ValueError:
        raise AssemblyAIError(
            "ASSEMBLYAI_TIMEOUT_SECONDS must be a finite positive number."
        ) from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise AssemblyAIError(
            "ASSEMBLYAI_TIMEOUT_SECONDS must be a finite positive number."
        )

    try:
        async with asyncio.timeout(timeout):
            try:
                audio = await asyncio.to_thread(audio_path.read_bytes)
            except OSError:
                raise AssemblyAIError(
                    "Cannot read the local audio segment for AssemblyAI upload."
                ) from None
            upload = await _request(
                client, "POST", f"{base_url}/v2/upload", api_key, "upload", timeout,
                headers={"content-type": "application/octet-stream"}, content=audio,
            )
            upload_url = upload.get("upload_url")
            if not isinstance(upload_url, str) or not upload_url.strip():
                raise AssemblyAIError("AssemblyAI upload returned no upload_url.")
            transcript = await _request(
                client, "POST", f"{base_url}/v2/transcript", api_key,
                "transcript submission", timeout,
                json={
                    "audio_url": upload_url,
                    "language_code": language,
                    "speech_models": ["universal-2"],
                },
            )
            transcript_id = transcript.get("id")
            if not isinstance(transcript_id, str) or not transcript_id.strip():
                raise AssemblyAIError("AssemblyAI submission returned no transcript id.")
            poll_url = f"{base_url}/v2/transcript/{quote(transcript_id, safe='')}"
            while True:
                status = transcript.get("status")
                if status == "completed":
                    if "text" not in transcript:
                        raise AssemblyAIError("AssemblyAI completed response has no text field.")
                    text = transcript["text"]
                    if text is None:
                        return None
                    if not isinstance(text, str):
                        raise AssemblyAIError("AssemblyAI returned an invalid transcript text.")
                    return text.strip() or None
                if status == "error":
                    raise AssemblyAIError(
                        "AssemblyAI transcription failed: "
                        + _error_detail(transcript.get("error"), api_key)
                    )
                if status not in ("queued", "processing"):
                    raise AssemblyAIError("AssemblyAI returned an unknown transcript status.")
                # At most 60 polls per minute per transcript, including queued jobs.
                await asyncio.sleep(1)
                transcript = await _request(
                    client, "GET", poll_url, api_key, "transcript polling", timeout,
                )
    except TimeoutError:
        raise TimeoutError(
            f"AssemblyAI transcription timed out (overall limit {timeout:g} seconds)."
        ) from None
