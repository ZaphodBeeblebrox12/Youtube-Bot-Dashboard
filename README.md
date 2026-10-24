# YouTube Bot Dashboard

A private, local-first livestream co-pilot with a curated answer queue and low-noise community memory. It keeps moderation, viewer recognition, and historical comment cleanup separate.

## Safety levers

- **Lever 1 — Session Shield (initial mode):** internally excludes selected users' messages from the Reply Finder AI input for the remainder of the current livestream session. It does not mute, timeout, ban, or restrict the user on YouTube.
- **Lever 2 — Active Moderation (future, disabled):** actual enforcement is out of scope until a separate rulebook is approved and the owner explicitly enables it.
- **Past Videos Cleanup:** scans comments on the most recent 20 uploads, stores discovered comments, and classifies only newly discovered comments. Initial deletion workflow must be preview-first and owner-approved.

## Comment cleanup policy core

The first implementation slice is a deterministic policy gate around AI classifications. It separates target, sentiment, and conduct type. Negative sentiment alone is never enough to propose deletion. Genuine criticism such as "This lesson is terrible and your explanation makes no sense" is kept by default. High-confidence personal abuse or another explicitly mapped violation can become a deletion candidate, but still requires owner approval.

The Streamlit panel, durable live queue, optional Gemini/Qwen adapters, authenticated JSON ingestion bridge, read-only YouTube OAuth setup, and direct `liveChatMessages.streamList` listener code are present. The direct connector still needs a real OAuth/active-broadcast test; it is not claimed as end-to-end verified. Provider-specific prompt caching, PostgreSQL production migrations, and historical comment deletion calls remain outstanding. Session Shield does not call YouTube ban/mute/timeout/delete APIs.

## Development

Requires Python 3.11 or newer. For local configuration, copy `.env.example` to `.env`, set a long random `INGEST_API_KEY`, and configure at least one AI provider. The `.env` file is ignored by Git.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev,ai,youtube]"
pytest
streamlit run app.py
# in a second terminal, accept normalized chat JSON from a trusted source
uvicorn youtube_bot_dashboard.ingest_api:app --host 127.0.0.1 --port 8000
# optional: authorize once after configuring a Desktop OAuth client
python -m youtube_bot_dashboard.youtube_auth
# optional: receive YouTube live chat over streamList in a separate terminal
python -m youtube_bot_dashboard.youtube_listener
# in another terminal, start the live queue worker
python -m youtube_bot_dashboard.live_worker
```

## Current implementation status

- [x] Project scaffold and test setup
- [x] Conservative negative-comment decision gate
- [x] Structured comment-cleanup prompt with mandatory keep examples for genuine criticism
- [x] SQLAlchemy persistence models for monitored videos, comments, and text versions
- [x] Incremental comment upsert (new / unchanged / edited) and classification queue primitives
- [x] Persistent live message/task/batch records for separate moderation and Reply Finder pipelines
- [x] Configurable live batch settings persisted to the database (2-second delay, 100-message cap, approximate token budget)
- [x] Round-robin Gemini/Qwen batch routing state, with per-pipeline rotation and fixed-provider options
- [x] Session Shield filtering before Reply Finder payload creation while moderation tasks continue independently
- [x] Initial Streamlit settings/queue-status dashboard with authenticated normalized-JSON ingest bridge
- [x] Live moderation and Reply Finder system-prompt templates
- [ ] PostgreSQL production configuration, migrations, and concurrency hardening
- [x] Background worker and Gemini/Qwen provider adapters with response-schema validation
- [x] Environment-based provider registry (model IDs/API credentials supplied outside source)
- [x] Authenticated normalized-JSON ingestion endpoint for a trusted chat source
- [x] Read-only YouTube OAuth setup and direct streamList listener code (live-broadcast verification pending)
- [ ] Provider-specific prompt caching and usage reconciliation
- [x] One-at-a-time private Reply Finder card UI and two-sentence answer briefs
- [x] Community memory profiles based on stable channel ID, visit history, and optional member badge
- [x] Meaningful memory suggestions within the Reply Finder pass, owner review, two-saved-memory cap, forget controls, and glanceable returning-viewer cue
- [ ] Preview/approval-based comment deletion integration
- [ ] Active Moderation enforcement (future phase only)

The project decision log in `docs/PROJECT_DECISIONS.md` is the source of truth for product decisions and future work.

**Verification:** GitHub Actions run [40127347324](https://github.com/ZaphodBeeblebrox12/Youtube-Bot-Dashboard/actions/runs/40127347324) passed **67 tests in 2.98 seconds** on commit `72f311456e6c307ea658dc7773c611eeb42f6c1a`. This is automated test coverage, not a live YouTube OAuth/broadcast smoke test.


## External JSON source format

The ingest bridge accepts a normalized batch at `POST /api/v1/live/ingest` with the secret in `X-Ingest-Key`. For example:

```json
{
  "session_id": "your-live-session-id",
  "live_chat_id": "youtube-live-chat-id",
  "broadcast_id": "youtube-broadcast-id",
  "messages": [
    {
      "message_id": "youtube-message-id",
      "user_id": "youtube-channel-id",
      "username": "Viewer",
      "is_member": true,
      "text": "Why can a breakout fail?",
      "published_at": "2026-10-25T12:00:00Z"
    }
  ]
}
```

Each message is stored once and queued independently for moderation and Reply Finder. The optional `is_member` value must come from the source; the system never guesses membership. Community Memory tracks basic visit history by stable channel ID but stores no ordinary chat text as a memory. Reply Finder can suggest a short follow-up when someone shares an ongoing challenge, goal, or progress; the owner must approve it, with at most two saved memories per viewer. During the stream, the queue shows a brief “returning viewer” cue, not another inbox. Session Shield messages still enter moderation but are removed from the Reply Finder payload before its AI call. Treat all message text as untrusted input. Do not expose `INGEST_API_KEY` to viewers or include it in client-side code.
