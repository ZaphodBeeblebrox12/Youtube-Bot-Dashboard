# Handoff — YouTube Bot Dashboard: Shield, Persistent Moderation, and Super Chat Priority

**Updated:** 2026-10-25  
**Repository:** https://github.com/ZaphodBeeblebrox12/Youtube-Bot-Dashboard  
**Working branch:** `main`  
**Code checkpoint before this handoff document:** `4026f55eec93b8492f024510cad21774093b17e7`  
**Last verified CI at that code checkpoint:** [Run 40130179648](https://github.com/ZaphodBeeblebrox12/Youtube-Bot-Dashboard/actions/runs/40130179648) — **100 passed in 3.28s**  
**Purpose of next chat:** Continue the existing design; do not ask the user to re-explain decisions already captured in notes.

## 1. User expectation and context

The user says this moderation/ban-mute flow was discussed at length and asks that saved notes be checked before asking the same questions again. The source of truth is the repository's `docs/PROJECT_DECISIONS.md` (plus the user's clarification in the current conversation).

Recent explicit requirement: **once a viewer is blacklisted/restricted, their subsequent messages must be excluded before AI processing—not repeatedly sent to Moderation or Reply Finder.** This must apply to both AI pipelines. A Super Chat does not bypass an active blacklist/restriction or the safety gate.

The current implementation only has **session-scoped Session Shield**. Do not mistake it for the planned persistent strike/sanction system.

## 2. Settled moderation policy from the project notes

The notes specify the following escalation policy for confirmed qualifying offences by the **same reliably identified YouTube account**:

1. First offence: restrict until the current chat/session ends.
2. Second offence: 7-day ban.
3. Third offence: 30-day ban.
4. Fourth offence: permanent ban.

Policy is enforced by the backend, **not by the AI model**. AI produces a structured classification/recommendation; backend validation, confirmed incident history, deduplication, strike counting, and sanction decisions are separate steps.

Other settled rules:
- Track identity by the stable supplied account/channel ID, never the mutable display name.
- Deduplicate a repeated spam burst as one incident so it does not create several strikes.
- Preserve supporting message IDs, evidence, reason, timestamps, policy decision, and enforcement outcome in an audit trail.
- Support moderator override and appeals; an overturned decision must be reflected correctly in strike history.
- Critical threats, doxxing, and clear scams need urgent review/protective handling; do not force them to wait for four strikes. Exact critical-case automatic-enforcement thresholds remain to be validated.
- Cross-account ban evasion is out of scope without reliable linking evidence. Similar names, avatars, wording, or writing style alone do not establish identity.
- Start in dry-run mode; do not enable live automated enforcement until representative false-positive/false-negative tests and enforcement semantics are reviewed.

The notes say an active restriction/ban must be represented in the user's record and active sanctions. The user's latest clarification requires the pre-AI gate to respect that active state across future messages/streams. A new stream must not silently erase an unexpired timed ban or permanent sanction. First-offence current-session restriction and the timed/permanent sanctions above must retain their distinct expiration semantics.

## 3. Current implementation checkpoint

### Already in the repository

- **Default Live Chat display is fail-closed** in `src/youtube_bot_dashboard/chat_visibility.py` and `app.py`:
  - Ordinary message: moderation task must be `COMPLETED`, category `NO_VIOLATION`, confidence at least **0.90**, and Reply Finder must be `COMPLETED` with `selected is True`.
  - Super Chat can bypass the *usefulness-selection requirement only*. It must still pass the same high-confidence safety gate.
  - Unfiltered message display is an explicit opt-in and defaults off.
  - This is a private display filter, not YouTube moderation enforcement.

- **Current-session Shield** in `src/youtube_bot_dashboard/live_queue.py`:
  - Identifies the viewer by supplied `user_channel_id` and session ID.
  - Newly ingested messages from a viewer already shielded in that session are marked `EXCLUDED_SHIELDED` for both `MODERATION` and `REPLY_FINDER`.
  - Batch preparation checks Shield for both pipelines.
  - If Shield is applied after ingestion but before send, the viewer's items are scrubbed from prepared payloads. The worker sends the persisted, current payload rather than an old snapshot.
  - If a provider request was already sent, it cannot be recalled; Shield marks that task excluded, so it must not return to the visible host queue.
  - This Shield is scoped to `session_id`; it is **not** a persistent blacklist, strike ledger, or YouTube ban/mute.

- **Super Chat capture and priority**:
  - `youtube_listener.py` reads the source-confirmed YouTube `super_chat_details` metadata, preserving amount micros, currency, display string and tier when available.
  - Super Chats without a user comment are still ingested as paid events.
  - `ingest_api.py` accepts equivalent source metadata from the normalized JSON bridge.
  - `storage.py` adds the new Super Chat columns for pre-existing databases.
  - `live_queue.py` puts Super Chats first in AI batch preparation and skips the normal collection delay when the paid message is first in the batch.
  - `prompts/reply_finder.py` instructs Reply Finder to treat verified paid messages as host priority without lowering safety standards.
  - Live Chat shows a **SUPER CHAT / amount / PRIORITY** label. Reply Queue also sorts paid messages first.
  - Source metadata, not text like “I sent a Super Chat”, determines paid status.

- Live Polls and Community Memory already exist; keep them working and avoid broad redesigns while completing moderation.

### Current test status

At code checkpoint `4026f55eec93b8492f024510cad21774093b17e7`, GitHub Actions test run `40130179648` passed with **100 tests**. These are unit/integration tests, including faked YouTube source objects—not a live-broadcast test.

## 4. Important gap: persistent strikes, sanctions, blacklist and enforcement

Before declaring moderation complete, inspect the current source again. At the checkpoint above, there is a `SessionShieldRecord`, but no complete persistent offence/strike/sanction domain model and policy engine implementing the agreed escalation ladder. The current repository also does not yet carry out the full authorized YouTube enforcement workflow.

Do not claim the ban/mute system is finished just because the Shield tests pass.

## 5. Required implementation plan for the next chat

### Step A — Reconfirm repository state and decisions
1. Read current `main` HEAD and CI status; the handoff document itself may add a docs-only commit after checkpoint `4026f55...`.
2. Read `docs/PROJECT_DECISIONS.md`, this handoff, and relevant current code before editing.
3. Inspect `live_queue.py`, `live_worker.py`, `prompts/live_moderation.py`, `youtube_listener.py`, `storage.py`, `app.py`, and the tests. Keep changes focused.

### Step B — Add durable moderation records and a backend policy engine
Design/persist records for at least:
- incident/offence with source message IDs and deduplication key, stable user/channel ID, category, severity/confidence, evidence, status/confirmation, decision explanation, timestamps;
- strike history tied to a confirmed, qualifying incident—not every individual chat message;
- sanction/restriction with type, start time, expiration, active/revoked state, reason, linked incident/strike, requested action, actual enforcement status, and audit history;
- moderator overrides/appeal outcomes sufficient to correct strike history if the decision is overturned.

Use schema migrations compatible with existing SQLite and supported production storage. Keep a unique/idempotent link between source incident and strike so retries cannot increment a strike twice. Consider concurrent duplicate worker responses.

### Step C — Put persistent sanction lookup before either AI pipeline
At ingestion and defensively before batch preparation, resolve the supplied stable user ID against active restrictions/sanctions. If a viewer has an active session restriction, unexpired timed sanction, or permanent sanction, subsequent messages must **not enter either Moderation or Reply Finder AI payloads**. This is the no-repeat-AI-cost requirement. Do not use display names as identity.

Preserve distinctions:
- session restriction expires only when that live chat/session ends;
- 7-day sanction persists across streams until expiration;
- 30-day sanction persists across streams until expiration;
- permanent sanction remains until an authorized moderator override/appeal changes it.
- expiry/override must be explicit and auditable; do not silently clear state at stream start.

An in-flight provider request cannot be recalled. If a viewer becomes restricted while a request is already in flight, ensure its result cannot overwrite the exclusion, create a second strike from the same incident, or resurface the message in Live Chat/Reply Queue. Add regression tests for this race.

### Step D — Connect validated AI results to the policy engine
- Validate all AI output against schema and exact IDs in the submitted batch.
- Treat AI output as a recommendation, not enforcement authority.
- Distinguish a potential violation from a confirmed qualifying offence according to the saved dry-run/review policy.
- Deduplicate spam bursts and idempotently confirm incidents/strikes.
- Apply escalation based on prior confirmed strikes for the same stable user ID.
- Allow urgent review for critical categories without inventing automatic thresholds that the notes say are still unvalidated.

### Step E — Enforcement adapter, not AI-driven actions
Implement platform-specific moderation actions only after the backend policy authorizes them. Verify the actual YouTube API action semantics, OAuth scopes, required permissions and supported duration behavior from current official documentation at implementation time. Record requested and actual outcomes. Handle rate limits, permission errors, retries, idempotency and failed sanctions. Keep dry-run as the default until tests and user approval establish live enforcement is safe.

### Step F — Dashboard
Add a persistent user/moderation record view showing:
- stable channel ID and supplied display name;
- confirmed incident/strike history and evidence;
- current sanction, reason, start/expiry, and enforcement outcome;
- Shield/blacklist state and why messages are excluded before AI;
- moderator override/appeal controls and audit history.

Clearly distinguish **AI display whitelist**, **session Shield**, **persistent sanction/blacklist**, and **actual YouTube enforcement**. Do not label internal exclusion as “banned” unless a YouTube enforcement response confirms it.

### Step G — Regression tests and verification
At minimum, test:
- first/second/third/fourth confirmed offences and exact escalation outcome;
- next stream while 7-day, 30-day and permanent sanctions remain active;
- expiry, override and appeal reversal;
- one spam burst produces one strike;
- retry/replay and concurrent responses cannot double-count a strike;
- messages from active sanctioned accounts are excluded before both AI calls, including source-confirmed Super Chats;
- no user ID available: no invented identity and no fabricated offence linkage;
- a safe Super Chat is prioritized only after safety clearance;
- harmful/uncertain/low-confidence Super Chats stay hidden;
- a shield applied to a prepared batch scrubs it, and an in-flight completion cannot undo exclusion;
- existing Live Polls, Community Memory and dashboard syntax remain correct.

Run focused tests first, then full CI. Test against the fake YouTube service; real-broadcast/OAuth verification remains a separate checkpoint once the channel is active.

## 6. Do not forget

- The user has already invested significant time defining this; consult these notes before asking questions.
- Do not reduce persistent sanctions to session Shield.
- Do not send messages from an actively blacklisted/restricted account to either AI pipeline just to classify them again.
- Do not allow Super Chat/payment priority to override safety or an active sanction.
- Do not fabricate cross-account identity.
- Do not enable live automated bans merely because unit tests pass.
- Keep the UI calm, compact and light-themed. Do not redesign unrelated pages.
- Update `docs/PROJECT_DECISIONS.md` after implementation with what changed, tests run, enforcement state, and remaining limitations.

## 7. Handy links

- Repository: https://github.com/ZaphodBeeblebrox12/Youtube-Bot-Dashboard
- Project decisions: https://github.com/ZaphodBeeblebrox12/Youtube-Bot-Dashboard/blob/main/docs/PROJECT_DECISIONS.md
- Current-session Shield and queue: https://github.com/ZaphodBeeblebrox12/Youtube-Bot-Dashboard/blob/main/src/youtube_bot_dashboard/live_queue.py
- AI whitelist display policy: https://github.com/ZaphodBeeblebrox12/Youtube-Bot-Dashboard/blob/main/src/youtube_bot_dashboard/chat_visibility.py
- Live Chat / Reply Queue UI: https://github.com/ZaphodBeeblebrox12/Youtube-Bot-Dashboard/blob/main/app.py
- Last green test run at code checkpoint: https://github.com/ZaphodBeeblebrox12/Youtube-Bot-Dashboard/actions/runs/40130179648
