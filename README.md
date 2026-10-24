# YouTube Bot Dashboard

A private livestream co-pilot that filters chat into a curated answer queue, while keeping moderation decisions, persistent history, and historical comment cleanup separate.

## Safety levers

- **Lever 1 — Session Shield (initial mode):** internally excludes selected users' messages from the Reply Finder AI input for the remainder of the current livestream session. It does not mute, timeout, ban, or restrict the user on YouTube.
- **Lever 2 — Active Moderation (future, disabled):** actual enforcement is out of scope until a separate rulebook is approved and the owner explicitly enables it.
- **Past Videos Cleanup:** scans comments on the most recent 20 uploads, stores discovered comments, and classifies only newly discovered comments. Initial deletion workflow must be preview-first and owner-approved.

## Comment cleanup policy core

The first implementation slice is a deterministic policy gate around AI classifications. It separates target, sentiment, and conduct type. Negative sentiment alone is never enough to propose deletion. Genuine criticism such as "This lesson is terrible and your explanation makes no sense" is kept by default. High-confidence personal abuse or another explicitly mapped violation can become a deletion candidate, but still requires owner approval.

The initial Streamlit panel, durable live queue, worker, and optional Gemini/Qwen provider adapters are in place. YouTube OAuth/live chat ingestion, provider-specific prompt caching, PostgreSQL production migrations, and historical comment deletion calls are not connected yet. No YouTube ban/mute/timeout/delete action is made by the live Session Shield path.

## Development

Requires Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev,ai]"
pytest
streamlit run app.py
# in a second terminal, start the live queue worker
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
- [x] Initial Streamlit settings/queue-status dashboard scaffold (no live source connected yet)
- [x] Live moderation and Reply Finder system-prompt templates
- [ ] PostgreSQL production configuration, migrations, and concurrency hardening
- [ ] YouTube OAuth and live chat ingestion
- [x] Background worker and Gemini/Qwen provider adapters with response-schema validation
- [x] Environment-based provider registry (model IDs/API credentials supplied outside source)
- [ ] YouTube OAuth and live chat ingestion to populate the live queue
- [ ] Provider-specific prompt caching and usage reconciliation
- [ ] Fully implemented private Reply Finder card UI and two-sentence answer briefs
- [ ] Preview/approval-based comment deletion integration
- [ ] Active Moderation enforcement (future phase only)

The project decision log in `docs/PROJECT_DECISIONS.md` is the source of truth for product decisions and future work.
