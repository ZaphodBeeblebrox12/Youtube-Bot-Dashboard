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

## 3A. Locked product requirements: meaningful questions and remembering returning viewers

These requirements are already recorded in `docs/PROJECT_DECISIONS.md` under **Second AI system: Reply Finder / Personal Attention Queue** and **Community Memory and glanceable live UX checkpoint (2026-10-25)**. Preserve them while working on moderation; do not reinterpret the dashboard as a raw-comment browser.



### The product meaning of “remembering a member”

Recognition is not just a returning-viewer badge or member label. The host needs a compact reminder of the **specific meaningful question/topic and what made it memorable**, with a natural way to continue that conversation. Example pattern: “Previously asked about entering breakouts too early; the memorable detail was difficulty waiting for confirmation; follow-up: ‘Did that change on your next setup?’” This is illustrative only; never fabricate a past interaction.

A generic question, hello, praise or membership badge alone is not a memory. Do not store every question. Prefer an explicit ongoing challenge, goal, preference, learning point or progress. Keep a short neutral summary linked to the actual source message and stable viewer ID, not a copy of raw chat text. Show the reminder only where it helps the host respond to a current message. Never automatically greet the viewer or claim a prior conversation that the stored memory does not support. Keep owner approval, private display, and forget controls.


### Meaningful questions — curated, attention-protecting queue

The reason for this product is to protect the host from having to manually read a constant stream of potentially hurtful or demoralizing raw comments. The default host-facing workflow should surface the messages genuinely worth responding to, not dump the unfiltered chat stream in front of the host.

Reply Finder is a separate AI system from moderation. It should prioritize:
- genuine trading/concept/risk/strategy/lesson/community questions;
- thoughtful disagreement and good-faith challenges;
- constructive criticism and actionable feedback;
- useful experiences, trade reviews, insights and new discussions;
- supportive/community-building messages and important unresolved requests for help.

Normally omit personal attacks, abuse, harassment, hate, threats, doxxing, scams, spam/flooding, provocation and near-duplicate questions. **“Worth answering” does not mean “positive only”:** a respectful critical question can be valuable; an abusive message is not worth surfacing just because it contains a question. Deduplicate repeated questions unless new information changes their value. The queue may be empty when nothing deserves the host's attention. Never automatically post suggested replies.

### Remember returning members and viewers

The goal is for returning community members—and viewers who contribute thoughtful questions—to feel recognized and remembered, without creating another inbox or forcing the host to stare at a dashboard while speaking.

- Build profile history only from an actual stable YouTube channel/user ID. Record first-seen, last-seen, distinct stream sessions, message count and supplied display name. Mark someone as a member only when the input source explicitly says so; missing membership information must remain unknown.
- Save *meaningful personal memory*, not every question. Examples: an ongoing learning challenge, goal, progress, meaningful lesson, or explicit preference that helps the host follow up. Do not save a basic definition question, generic market question, greeting, generic praise or spam as personal memory by itself.
- Ask for memory candidates within the existing Reply Finder AI request, not via a new model call for every comment. Validate source message ID, stable viewer ID, type, required text and confidence server-side.
- AI creates a private suggestion only. The host approves it via **Save memory** or discards it via **Skip**; the AI must not save it automatically or greet people publicly.
- Limit to one pending suggestion and two saved memories per viewer. Never silently replace a saved memory when the limit is reached.
- In the Reply Queue, a selected message from a returning viewer can show a compact cue when a saved memory has not been used in the current stream: supplied member status, stream count, first-seen date, one remembered detail and a suggested follow-up. **“Used this follow-up”** suppresses that cue for the rest of the current stream.
- Do not copy ordinary raw chat text into the community-memory table. Keep only a short, neutral memory summary and optional follow-up. Preserve controls to forget an individual memory or remove a viewer profile, visit history and associated suggestions/memories.
- Do not infer identity by display name or merge viewers with similar names. Do not infer membership. Do not invent viewer history when stable identity is absent.

The repo notes say this behavior is implemented in `community_memory.py`, `live_queue.py`, `prompts/reply_finder.py`, `live_worker.py` and the Community Memory / Reply Queue UI in `app.py`; the README also lists meaningful-memory suggestions, owner review, the two-memory limit, forget controls and returning-viewer cues as implemented. Preserve and test existing functionality while delivering persistent moderation. Actual live-broadcast delivery still needs real-stream verification.



## 3B. Reply Finder prompt refinement — separate selection from memory

The prompt/backend are being refined so the AI makes two independent decisions in one Reply Finder request:
1. `selected_messages`: what deserves a live response now.
2. `memory_candidates`: which supplied message contains a genuinely useful, explicit, non-sensitive detail for future recognition, whether or not that message was selected for the queue.

Do not put memory suggestions only inside each selected message. That coupling prevents a worthwhile detail from being remembered when the message isn't a good fit for today's live queue. Backend must validate candidates against exact supplied message IDs and use stable source identity, with no invented identity. Keep the one-call design, confidence threshold, memory limits, owner approval, privacy and no-auto-greeting/no-auto-posting rules. See the “Reply Finder memory independence refinement” entry in `docs/PROJECT_DECISIONS.md` for the settled decision.



## 3C. Reply hints are only for Medium and Hard questions

**Easy questions receive no AI hint, no answer angle, and no suggested response.** The backend must strip all three even if the model sends them. For Medium and Hard questions, provide a concise separate `ai_hint` and a `suggested_live_response` of exactly two complete sentences. The dashboard must render the hint and response only for those two difficulty levels.

## 3D. Dashboard visual implementation target

Implement the approved TradeThesis light-theme reference as the actual, data-backed dashboard workspace: top metrics; left workspace navigation; a safe approved-chat list with inline AI hint and two-sentence draft for Medium/Hard only; selected-message details; returning-member profile, saved memories and suggested follow-up; and lower panels for Reply Queue, recent Super Chats, filtered metadata and Session Shield. Do not fabricate viewer counts or sample messages. Hide raw content in filtered/shield summary panels, use only safe metadata there, and preserve the separate explicit unfiltered toggle on the Live Chat workspace. Keep all other existing workspaces and their action semantics intact.


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
