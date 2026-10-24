# YouTube Bot Dashboard

A private livestream co-pilot that filters chat into a curated answer queue, while keeping moderation decisions, persistent history, and historical comment cleanup separate.

## Safety levers

- **Lever 1 — Session Shield (initial mode):** internally excludes selected users' messages from the Reply Finder AI input for the remainder of the current livestream session. It does not mute, timeout, ban, or restrict the user on YouTube.
- **Lever 2 — Active Moderation (future, disabled):** actual enforcement is out of scope until a separate rulebook is approved and the owner explicitly enables it.
- **Past Videos Cleanup:** scans comments on the most recent 20 uploads, stores discovered comments, and classifies only newly discovered comments. Initial deletion workflow must be preview-first and owner-approved.

## Comment cleanup policy core

The first implementation slice is a deterministic policy gate around AI classifications. It separates target, sentiment, and conduct type. Negative sentiment alone is never enough to propose deletion. Genuine criticism such as "This lesson is terrible and your explanation makes no sense" is kept by default. High-confidence personal abuse or another explicitly mapped violation can become a deletion candidate, but still requires owner approval.

This package does not yet connect to YouTube or an AI provider and cannot delete comments. That separation is intentional: the policy decision is tested before wiring it to any API action.

## Development

Requires Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
pytest
```

## Current implementation status

- [x] Project scaffold and test setup
- [x] Conservative negative-comment decision gate
- [ ] PostgreSQL schema and migrations
- [ ] YouTube OAuth and comment ingestion
- [ ] Durable incremental sweeps and job checkpoints
- [ ] Gemini/Qwen AI adapters and schema validation
- [ ] Private Reply Finder dashboard and two-sentence answer briefs
- [ ] Preview/approval-based comment deletion integration
- [ ] Active Moderation enforcement (future phase only)

The project decision log in `docs/PROJECT_DECISIONS.md` is the source of truth for product decisions and future work.
