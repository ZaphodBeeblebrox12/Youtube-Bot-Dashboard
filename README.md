# YouTube Bot Dashboard

A private, local-first livestream co-pilot with a curated answer queue, low-noise community memory, and owner-controlled YouTube live polls. It keeps moderation, viewer recognition, and historical comment cleanup separate.

## Reply Finder response rules

- Easy questions receive no AI hint, answer angle, or drafted response. Medium and Hard questions receive a concise AI hint plus a separate two-sentence spoken response; malformed outputs are flagged for review.

## Live-message text retention

- Settings provides a 0–365 day retention dial for live-chat message text. **0 means keep indefinitely.**
- For a positive window, raw live-message text older than the chosen number of days is redacted automatically when the live worker starts and then hourly. Settings also provides an immediate manual cleanup action.
- The operation scrubs the expired message's text from persisted AI request payloads and excludes pending/retryable/in-flight tasks for that expired content. It retains message IDs/metadata, completed AI task results, moderation history, community viewer profiles, approved memory summaries, and historical-video comment records.
- This retention dial is specifically for the `live_messages` live-chat stream, not the separate `comments` / `comment_text_versions` historical-video cleanup store. Rows are redacted rather than deleted so stable foreign keys and saved-memory links survive.

## Safety levers

- **Lever 1 — Session Shield:** once a viewer is shielded, subsequent messages are excluded before both moderation and Reply Finder AI calls for the remainder of that livestream session. Requests already dispatched cannot be recalled; completed moderation records that triggered the shield remain in the audit trail. This is an internal AI-input filter, not a YouTube ban, timeout, or mute action.
- **Lever 2 — Active Moderation (future, disabled):** actual enforcement is out of scope until a separate rulebook is approved and the owner explicitly enables it.
- **Past Videos Cleanup:** scans comments on the most recent 20 uploads, stores discovered comments, and classifies only newly discovered comments. Initial deletion workflow must be preview-first and owner-approved.

## Comment cleanup policy core

The first implementation slice is a deterministic policy gate around AI classifications. It separates target, sentiment, and conduct type. Negative sentiment alone is never enough to propose deletion. Genuine criticism such as "This lesson is terrible and your explanation makes no sense" is kept by default. High-confidence personal abuse or another explicitly mapped violation can become a deletion candidate, but still requires owner approval.

The Streamlit panel, durable live queue, optional Gemini, Qwen, DeepSeek, and Kimi adapters, authenticated JSON bridge, direct `liveChatMessages.streamList` listener, source-confirmed Super Chat detection/priority, and Live Polls workspace are present. The poll workspace saves drafts offline, explicitly inserts a poll into a live chat, closes it, and saves YouTube's returned tallies. Direct OAuth and real-broadcast actions still need an owner-authorized smoke test; they are not claimed as end-to-end verified. Provider-managed prompt caching is used where supported by the configured model; cache hit/write usage is reported when the provider API exposes it. PostgreSQL production migrations, concurrency hardening, and historical comment deletion calls remain outstanding. Session Shield does not call YouTube ban/mute/timeout/delete APIs.

## AI providers and usage analytics

Gemini, Qwen, DeepSeek, and Kimi can be enabled independently. The existing Settings default remains Gemini plus Qwen, so adding credentials does not silently change live routing. Select additional providers under Settings only after their API credentials and model IDs are configured.

- DeepSeek: set `DEEPSEEK_API_KEY` and `DEEPSEEK_MODEL`; `DEEPSEEK_BASE_URL` optionally overrides the official endpoint.
- Kimi: set `KIMI_API_KEY` (or `MOONSHOT_API_KEY`) and `KIMI_MODEL`; `KIMI_BASE_URL` optionally overrides the global endpoint.
- Qwen remains endpoint-configurable through `QWEN_API_KEY`, `QWEN_BASE_URL`, and `QWEN_MODEL`. Gemini uses `GEMINI_API_KEY` and `GEMINI_MODEL`.
- Provider-managed prompt caching is used where supported. The app records reported cache-hit/cache-write usage; a missing field is treated as unknown.
- Analytics is grouped by provider and pipeline. Estimated cost is shown only with a saved rate card for the exact configured model. Enter the provider's official USD-per-million-token prices in Settings; rates are not hard-coded into application code.

The live-message queue still rotates at batch level, independently for Moderation and Reply Finder. In round-robin mode, retryable batches keep their assigned provider while it remains enabled; if that provider is disabled or unavailable, the task can rejoin normal routing. The application does not automatically switch providers just because a request failed.

## Development

Requires Python 3.11 or newer. For local configuration, copy `.env.example` to `.env`, set a long random `INGEST_API_KEY`, configure at least one AI provider, and place a Google OAuth Desktop client JSON at the configured path. The `.env` file and OAuth token are ignored by Git. OAuth now requests `youtube.force-ssl` so the owner can create/end live polls as well as read chat; re-run the OAuth setup and approve the updated scope if you previously created a read-only token.

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
- [x] Round-robin Gemini/Qwen/DeepSeek/Kimi batch routing state, with per-pipeline rotation and fixed-provider options
- [x] Session Shield filtering before either AI payload is created; shielded viewers' later messages are not sent to moderation or Reply Finder providers
- [x] Initial Streamlit settings/queue-status dashboard with authenticated normalized-JSON ingest bridge
- [x] Live moderation and Reply Finder system-prompt templates
- [ ] PostgreSQL production configuration, migrations, and concurrency hardening
- [x] Background worker and Gemini/Qwen/DeepSeek/Kimi provider adapters with response-schema validation
- [x] Environment-based provider registry (model IDs/API credentials supplied outside source)
- [x] Authenticated normalized-JSON ingestion endpoint for a trusted chat source
- [x] YouTube OAuth setup and direct streamList listener code (live-broadcast verification pending)
- [x] Live Polls draft, explicit launch/end actions, final tally persistence, and dashboard results view (real-broadcast verification pending)
- [x] Provider-managed prompt-cache usage reporting, latency/failure analytics, and model-specific configurable token pricing
- [x] One-at-a-time private Reply Finder card UI and two-sentence answer briefs
- [x] Community memory profiles based on stable channel ID, visit history, and optional member badge
- [x] Meaningful memory suggestions within the Reply Finder pass, owner review, two-saved-memory cap, forget controls, and glanceable returning-viewer cue
- [ ] Preview/approval-based comment deletion integration
- [ ] Active Moderation enforcement (future phase only)

The project decision log in `docs/PROJECT_DECISIONS.md` is the source of truth for product decisions and future work.

**Poll permissions:** YouTube poll insertion and closing use `liveChatMessages.insert` and `liveChatMessages.transition`. These actions require `https://www.googleapis.com/auth/youtube.force-ssl` (or the documented `youtube` scope); the dashboard invokes them only when you click Launch or End. YouTube's API allows one active poll per chat. Draft preview is local and never sends anything to YouTube.

**Verification:** GitHub Actions run [40127947575](https://github.com/ZaphodBeeblebrox12/Youtube-Bot-Dashboard/actions/runs/40127947575) passed **73 tests in 2.89 seconds**, including mocked poll lifecycle tests. This is automated coverage, not a live YouTube OAuth/broadcast smoke test.


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

Each message is stored once and queued independently for moderation and Reply Finder. The optional `is_member` value must come from the source; the system never guesses membership. Community Memory tracks basic visit history by stable channel ID but stores no ordinary chat text as a memory. Reply Finder can suggest a short follow-up when someone shares an ongoing challenge, goal, or progress; the owner must approve it, with at most two saved memories per viewer. During the stream, the queue shows a brief “returning viewer” cue, not another inbox. Once a viewer is shielded, subsequent messages are excluded from both AI pipelines for the remainder of the session. Provider requests already dispatched cannot be recalled; their results are not persisted into the host queue. Treat all message text as untrusted input. Do not expose `INGEST_API_KEY` to viewers or include it in client-side code.


### Live Chat display and Super Chat priority

- The default Live Chat feed is fail-closed. Ordinary messages require a completed high-confidence `NO_VIOLATION` moderation result and a positive Reply Finder selection.
- Super Chats use the same safety gate, then appear at the top even if they have no comment or Reply Finder does not select them. YouTube's amount display, currency, and tier metadata are preserved when supplied.
- Super Chats are ordered ahead of ordinary messages in AI batches and the Reply Queue; a pending Super Chat triggers immediate batch preparation instead of waiting for the normal two-second collection window.
- The unfiltered feed is available through an explicit opt-in control and is off by default.
- Only source-confirmed `super_chat_details` are treated as paid events; text that merely claims to be a Super Chat does not qualify.
