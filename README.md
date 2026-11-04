# ShelfSync — Codex + ElevenLabs

Self-hosted audiobook/eBook position matching for [Audiobookshelf](https://www.audiobookshelf.org/). This is a fork of [tobiasredel/shelfsync](https://github.com/tobiasredel/shelfsync).

**No OpenAI API key is required.** Recaps and book-photo OCR run through the official Codex CLI using your ChatGPT subscription login. Audio calibration uses ElevenLabs Scribe's speech-to-text API.

The interface and user-facing API messages are in English. `SUMMARY_LANGUAGE` controls only generated recaps; choose the audiobook's spoken language separately in Audio-Sync. Book text and metadata are not translated.

## Features

- **Audio → page / page → audio:** estimate the corresponding reading or listening position.
- **Text → audio:** find a passage in the EPUB and jump to it in Audiobookshelf.
- **Photo OCR:** extract a readable paragraph from a Kindle/book photo with Codex image input.
- **Recaps:** summarize a missed section from EPUB text, in concise, detailed, or bullet style.
- **Audio-Sync:** extract short audio clips with ffmpeg, transcribe them with ElevenLabs, match them to the EPUB, and persist calibration anchors.
- **Kindle calibration:** save your page count and calibration points.
- **Currently reading:** pin books for quick access from the mobile-friendly interface.

Without provider credentials, basic position estimation, text search, and manual calibration still work. Precise alignment requires a matching audiobook/EPUB edition and good audio anchors. Before Audio-Sync, position estimates are proportional; the UI marks them as unsynced.

## Prerequisites

- Docker with Compose, or Python 3.12+ and ffmpeg for a local installation.
- Audiobookshelf URL and API token.
- A matching, DRM-free EPUB attached to the audiobook in Audiobookshelf.
- A ChatGPT plan with Codex access, signed in through **ChatGPT**, not an API key.
- An [ElevenLabs API key](https://elevenlabs.io/app/developers/api-keys) for Audio-Sync.

For Audiobookshelf, place the EPUB beside the audio files and rescan the book:

```text
/audiobooks/Author/Book/
  chapter01.mp3
  chapter02.mp3
  book.epub
  cover.jpg
```

## Docker setup

```bash
git clone https://github.com/dsoignoo/shelfsync.git
cd shelfsync
cp .env.example .env
```

Edit `.env`: set `AUDIOBOOKSHELF_URL`, `AUDIOBOOKSHELF_TOKEN`, and `ELEVENLABS_API_KEY`. Set `SUMMARY_LANGUAGE=en` or `de`.

```bash
docker compose build

# Follow the displayed link/code to sign in with your ChatGPT account.
docker compose run --rm shelfsync codex login --device-auth
docker compose run --rm shelfsync codex login status

# The status must say "Logged in using ChatGPT".
docker compose up -d
```

Open <http://localhost:8765>. The provided Compose file binds only to loopback. For remote access, use an authenticated HTTPS reverse proxy or a private tunnel; do not publish the app unauthenticated. `AUTH_USER` and `AUTH_PASS` enable built-in Basic auth. `/api/health` remains unauthenticated.

The image includes **Codex CLI 0.160.1** and ffmpeg, and runs as UID 1000. Compose uses two named volumes:

- `shelfsync-data`: calibration, currently-reading state, and EPUB cache.
- `codex-home`: Codex login and refreshed credentials, writable by the application user.

Keep the login volume private. Do not commit, publish, or share its `auth.json`, and do not mount your entire personal home directory. Log in once per instance. Use one application worker and one instance per credential directory: Codex calls are serialized in-process to avoid concurrent token refreshes.

If device login is unavailable, enable device-code authentication in your ChatGPT account settings or use the official [headless login guidance](https://developers.openai.com/codex/auth/). The CLI manages token refresh; ShelfSync never extracts subscription tokens or sends them to OpenAI's public API. Subscription limits still apply.

GitHub Actions builds `ghcr.io/dsoignoo/shelfsync:latest` for amd64 and arm64 after pushes to `main`. Source builds do not depend on registry access.

## Local setup

Install Codex CLI 0.160.1 and ffmpeg, then:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
codex login
codex login status

# Export your .env settings into this shell; uvicorn can load them below.
DATA_DIR="$PWD/data" .venv/bin/uvicorn app:app --env-file .env --host 127.0.0.1 --port 8765
```

Codex uses `CODEX_HOME`, or `~/.codex` when unset. Use a dedicated login directory if another application is also running Codex against your personal credential store.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AUDIOBOOKSHELF_URL` | `http://localhost:13378` (Compose: `http://audiobookshelf:13378`) | Reachable Audiobookshelf origin |
| `AUDIOBOOKSHELF_TOKEN` | empty | Audiobookshelf API bearer token |
| `ELEVENLABS_API_KEY` | empty | Required only for Audio-Sync |
| `ELEVENLABS_BASE_URL` | `https://api.elevenlabs.io` | Provider origin; regional residency URLs require account support |
| `ELEVENLABS_TIMEOUT_SECONDS` | `180` | Deadline for each clip's file read and synchronous transcription request |
| `CODEX_HOME` | `~/.codex` (image: `/home/shelfsync/.codex`) | Writable Codex credential/state directory |
| `CODEX_MODEL` | empty | Codex CLI default; optionally select a vision-capable model available on your subscription |
| `CODEX_TIMEOUT_SECONDS` | `180` | Deadline per Codex call, including queue time |
| `SUMMARY_LANGUAGE` | `de` | Recap language (`de` or `en`) |
| `DATA_DIR` | `/data` | Calibration and cache directory |
| `EPUB_MAX_SIZE_MB` | `100` | EPUB download size limit |
| `AUTH_USER`, `AUTH_PASS` | empty | Set both for Basic authentication |

`OPENAI_API_KEY` and `LLM_MODEL` are not used. The OpenAI Python SDK has been removed; there is no API-key fallback.

## Usage

1. Open the library, add a book to Currently Reading, then open its card.
2. In **Position**, enter your Kindle page count if desired.
3. In **Audio-Sync**, select the book's language and start calibration. German, English, French, and Spanish are offered. This uses ElevenLabs Scribe v2, uploads only extracted clips, and stores matched anchors locally.
4. In **Find**, enter a passage, a page number, or upload a JPEG/PNG/WEBP book-page photo.
5. In **Recap**, select up to 120 minutes and choose a summary style. Long excerpts are split into bounded chunks, summarized sequentially, and merged.

Audio-Sync uploads each clip as multipart audio and receives a synchronous transcript. Provider failures and timeouts are errors, not successful empty calibrations. If recalibration fails or no clips match, existing anchors are preserved. The UI reports subscription usage rather than estimating OpenAI API charges.

## Costs and data handling

| Feature | Provider / billing | Data sent |
|---|---|---|
| Position, manual calibration, text search | No AI provider calls | Audiobookshelf requests only |
| Recaps | Codex subscription allowance | Selected EPUB excerpt |
| Photo OCR | Codex subscription allowance | Uploaded photo |
| Audio-Sync | Your ElevenLabs account; current plan/rates apply | Extracted audio clips |

Codex requests use an ephemeral directory and stdin for source text. User config, project instructions, hooks, and execution/search capabilities are disabled; the read-only sandbox is retained. Temporary text/image/output files are removed on completion, failure, timeout, or cancellation. No sandbox bypass or privileged container is required by the integration. Host restrictions can still prevent sandbox operation: do not work around failures by granting unrestricted execution.

## API

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/books` | GET | Library items |
| `/api/books/{id}/details` | GET | EPUB and calibration status |
| `/api/position` | POST | Audio timestamp → page estimate |
| `/api/find-text` | POST | Passage → audio timestamp |
| `/api/page-to-audio` | POST | Page → audio timestamp |
| `/api/recap` | POST | EPUB excerpt → Codex summary (`provider: "codex"`) |
| `/api/ocr` | POST | Base64 JPEG/PNG/WEBP → text via Codex |
| `/api/audio-sync/{id}` | POST / DELETE | Create / remove audio calibration |
| `/api/calibration/{id}` | GET / DELETE | Calibration status / reset |
| `/api/chapter-mapping/{id}` | GET | Audio anchors and mapping quality |
| `/api/books/{id}/kindle-pages` | GET / PUT | Kindle page count and saved points |
| `/api/books/{id}/kindle-calibrate` | POST / DELETE | Manual Kindle calibration |
| `/api/sync-progress` | POST | Update Audiobookshelf listening position |
| `/api/health` | GET | Health check |

This fork makes a clean provider cutover: upstream `/api/whisper-sync` clients must use `/api/audio-sync`; persisted anchors use `audio_anchors` and `transcript_text`. Existing upstream Whisper anchors are not imported automatically—back up your old data and rerun Audio-Sync. Other page-count/calibration data can be retained in the data volume. Recap responses use `provider` instead of the old API-price estimate.

## Verification and troubleshooting

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests cover provider deadlines/cancellation, failure handling, credential isolation, partial-output rejection, and preservation of previous calibration. Live ElevenLabs verification requires your own key; simulated API tests do not prove account access.

| Problem | Check |
|---|---|
| Codex failed / login expired | Run `docker compose run --rm shelfsync codex login status`; re-login with ChatGPT if needed |
| Codex model unavailable | Leave `CODEX_MODEL` empty or select a model available to your plan |
| Codex timed out | Shorten the excerpt or increase `CODEX_TIMEOUT_SECONDS`; queued calls share the deadline |
| Sandbox failure | Check the host's sandbox/user-namespace restrictions; never disable sandboxing or use privileged mode |
| ElevenLabs 401/403 | Check the key, account quota, and Speech to Text permission |
| ElevenLabs timeout | Check service availability or increase `ELEVENLABS_TIMEOUT_SECONDS` |
| No audio/EPUB matches | Check the selected language and audiobook/EPUB edition |
| No EPUB found | Attach a DRM-free EPUB to the audiobook and rescan |
| Connection error | Ensure the Audiobookshelf URL is reachable from inside the container |

## License

MIT (upstream project).
