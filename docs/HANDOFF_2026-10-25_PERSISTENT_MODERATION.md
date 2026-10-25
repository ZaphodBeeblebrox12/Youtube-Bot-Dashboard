# Handoff — YouTube Bot Dashboard: Shield, Persistent Moderation, and Super Chat Priority

**Updated:** 2026-10-25  
**Repository:** https://github.com/ZaphodBeeblebrox12/Youtube-Bot-Dashboard  
**Working branch:** `feature/persistent-moderation` (do not merge into `main` without explicit approval)  
**Code checkpoint before this handoff document:** `4026f55eec93b8492f024510cad21774093b17e7`  
**Last verified CI at that code checkpoint:** [Run 40130179648](https://github.com/ZaphodBeeblebrox12/Youtube-Bot-Dashboard/actions/runs/40130179648) — **100 passed in 3.28s**  
**Purpose of next chat:** Continue the existing design; do not ask the user to re-explain decisions already captured in notes.

## 1. User expectation and context

The user says this moderation/ban-mute flow was discussed at length and asks that saved notes be checked before asking the same questions again. The source of truth is the repository's `docs/PROJECT_DECISIONS.md` (plus the user's clarification in the current conversation).

Recent explicit requirement: **once a viewer is blacklisted/restricted, their subsequent messages must be excluded before AI processing—not repeatedly sent to Moderation or Reply Finder.** This must apply to both AI pipelines. A Super Chat does not bypass an active blacklist/restriction or the safety gate.

The current implementation only has **session-scoped Session Shield**. Do not mistake it for the planned persistent strike/sanction system.

## 2. Settled moderation policy from the project notes

The authoritative policy is the more detailed PROJECT_DECISIONS.md; the four-step first/7-day/30-day/permanent ladder is an inconsistent draft and is not implemented. Current saved rule: the first formal enforcement action is eligible only after three confirmed session strikes across three distinct live sessions for the same stable YouTube channel ID. The first action is intended to be temporary/session-scoped, pending verification of exact YouTube API duration semantics. Later escalation durations and counts remain unfinalized. Until the owner approves the future rulebook, confirmed incidents remain a review/audit workflow and enforcement stays disabled.

Policy is enforced by the backend, **not by the AI model**. AI produces a structured classification/recommendation; backend validation, confirmed incident history, deduplication, strike counting, and sanction decisions are separate steps.

Other settled rules:
- Track identity by the stable supplied account/channel ID, never the mutable display name.
- Deduplicate a repeated spam burst as one incident so it does not create several strikes.
- Preserve supporting message IDs, evidence, reason, timestamps, policy decision, and enforcement outcome in an audit trail.
- Support moderator override and appeals; an overturned decision must be reflected correctly in strike history.
- Critical threats, doxxing, and clear scams need urgent review/protective handling; do not force them to wait for four strikes. Exact critical-case automatic-enforcement thresholds remain to be validated.
- Cross-account ban evasion is out of scope without reliable linking evidence. Similar names, avatars, wording, or writing style alone do not establish identity.
- Start in dry-run mode; do not enable live automated enforcement until representative false-positive/false-negative tests and enforcement semantics are reviewed.

A moderator-applied persistent internal restriction is now represented separately from Session Shield and remains effective across new sessions until expiry or explicit revocation. The pre-AI gate must honor active internal restrictions. Actual YouTube sanctions and the unfinalized post-first-ban ladder are not implemented and must not be implied by the internal restriction feature.

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

## 4. Implementation status after the persistent moderation foundation

The feature branch adds durable moderation incident candidates, confirmed session-strike records, reviewer decisions, appeal/reversal records, moderation audit events, and timed/permanent internal AI restrictions. Active internal restrictions are checked before AI processing at ingestion and batch preparation. The dashboard exposes review, override, appeal, restriction and audit controls.

Still not implemented: YouTube ban/timeout enforcement, the finalized post-first-ban escalation ladder, owner-controlled activation of live enforcement, live-broadcast/OAuth verification, multi-worker locking, and a production migration runner. Do not describe internal restrictions as YouTube bans.

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


## 8. Persistent moderation implementation update (feature branch)

- Validated violation classifications create pending incident candidates; a moderator must explicitly confirm one before it counts as a session strike.
- A confirmed qualifying offence can add at most one strike per stable channel ID per live session. The saved three-distinct-session threshold is used only for enforcement review; no enforcement API is called.
- Persistent internal restrictions can be timed or permanent, apply across sessions, and exclude messages before both AI pipelines. They are distinct from Session Shield and do not prevent public YouTube chat participation.
- The dashboard has review, dismissal, override/overturn, appeal, strike-correction and audit controls.
- Policy authority: PROJECT_DECISIONS.md. The proposed 7-day/30-day/permanent escalation remains unresolved; do not activate automatic enforcement until the owner approves the final rulebook.
- Test and CI status must be updated after the branch workflow completes.

## 9. Non-negotiable live UX purpose — filtered chat and community recognition (2026-10-25)

The owner clarified the product's primary motivation: raw YouTube comments can be hurtful and demoralizing. This dashboard is being built so the creator can replace the raw YouTube chat view with a safer, curated feed of comments worth reading while livestreaming. It should also help returning viewers feel recognized and remembered, so the channel feels like a real community. Treat this as a core product requirement, not a design preference.

### Default live experience
- The default live screen is **Filtered Chat**, not a KPI overview, raw chat log, or moderation workbench.
- The centre of attention is the actual filtered comments that the creator can read quickly while speaking. Keep controls, metrics and status text secondary.
- Show only messages that pass the configured safety/display filters and the Reply Finder selection requirement. Do not mix raw or held-back comments into the ordinary reading feed. Any raw/audit view must be separately and deliberately opened.
- Do **not** put a repetitive “cleared/safe/approved” label on every filtered comment. Being in this feed already means it passed the configured filters. Show only labels that add meaning, such as a source-confirmed Super Chat or verified membership.
- Do not draw constant attention to the amount or content of rejected/hurtful chat. Filtering should quietly protect the reading experience.
- Super Chat payment must not bypass safety or Shield/internal restrictions. Session Shield and internal restrictions are not public YouTube bans.

### Live scrolling
- The filtered feed is a true scrollable chat timeline: older comments above, newest comments at the bottom.
- Qualifying new comments arrive automatically. If the creator is at the latest position, follow the latest messages; if the creator scrolls up, preserve their reading position rather than auto-snapping to the bottom.
- When new comments arrive while scrolled up, show a compact **Jump to latest** control with a new-message count. Selecting it returns to the newest comments.
- Avoid layout jumps that move the comment being read. The screen must be usable by glance during a livestream, not require continual interaction.
- Never present sample/placeholder messages or fabricated counts as live data. Disconnected or empty states must be explicit.

### Returning viewers and members
- Keep returning-viewer context beside the username in the feed when history can be reliably matched: for example, previous-stream count and last-seen time.
- Show membership status separately from returning-viewer status, and only when verified source data provides it. Do not assume all returning viewers are members or all members are new/returning.
- Keep concise Community Memory context accessible from the chat item with minimal interruption. Use relevant remembered details to help the host recognize regulars; put fuller profile detail on demand.
- Never fabricate prior visits, membership, identity, or saved memory. Prefer stable source-provided channel IDs; do not infer identity from similar names or writing style.
- Do not make the host navigate away from the filtered feed just to recognize a returning viewer.

### Design acceptance test
At a glance, the creator should see the filtered comments, read them, keep their place as more comments arrive, and recognize regulars. Any UI element that competes with those tasks should be secondary or moved out of Live Mode.

The user has explicitly said these points are requirements. Future implementation must preserve them and must not silently remove/reinterpret them in the name of simplification. The source of truth is this section together with the matching “Glance-first filtered chat and community recognition” section in docs/PROJECT_DECISIONS.md.

## 10. Three-day community-memory and viewer-history retention (owner decision, 2026-10-25)

The owner explicitly requires that remembered community context be retained for **three days only**, due to storage concerns. Do not lengthen this without approval.

- Suggested and saved community-memory records expire three days after original creation. Owner approval/use does not extend the expiry.
- Detailed per-stream visit rows expire after three days to control storage. Preserve one compact profile per stable channel ID with aggregate first/last seen, stream count and message count so regulars can still be recognized after a longer break. Personal community-memory notes expire separately after three days.
- The live reading feed and Community Memory workspace must hide expired records immediately by query/filter, even before physical cleanup.
- Purge should run during normal community activity and on Community Memory workspace access, throttled so the live worker does not scan the complete tables on every incoming message.
- Returning status uses the compact aggregate for a stable supplied channel ID and requires at least two distinct sessions. Membership remains a separate, source-confirmed fact. Detailed per-stream visit rows expire after three days.
- Tests should cover old saved memories, pending suggestions, expiry of detailed visit rows, preservation of compact profiles, and session-count aggregates.
- Implemented on feature/persistent-moderation in community_memory.py, tests/test_community_memory.py, and app.py. Verification is pending the current Actions run.

The adjacent glance-first filtered-chat requirements remain in force: the live feed is the default reading surface, raw chat is not mixed into it, and returning-viewer context is shown inline without clutter.

## 11. Latest filtered-chat and three-day retention implementation status (2026-10-25)

- Current feature head after this change set: see the open PR/branch for the live SHA. Work remains on feature/persistent-moderation; main is untouched and nothing was deployed.
- The default workspace is now Filtered Chat, refreshing every three seconds and showing only messages that pass the existing safety + Reply Finder-selection gate. Shielded/persistently restricted users remain hidden. The newest 250 messages are rendered chronologically so the latest is at the bottom; the scrolling container uses Streamlit 1.66+ sticky autoscroll and a small Jump to latest · N new control keyed to newly selected message IDs.
- Returning viewer and source-confirmed membership labels are shown separately. Saved personal memory is exposed from the feed on demand; no repetitive cleared labels are shown.
- Three-day retention is implemented in community_memory.py: pending/saved personal memories and detailed per-stream visit rows expire after three days. Compact viewer profiles remain for returning-viewer recognition without retaining raw comment text. Full-table purge is globally throttled per process to hourly intervals; the Community Memory workspace also purges before listing its contents.
- tests/test_community_memory.py now covers expired saved/pending memory, expiry of detailed visit rows, rolling counters and preservation of returning-viewer recognition after a longer gap. The time-dependent Shield batch test now calculates its due timestamp from the actual current UTC time.
- **Verification caveat:** The latest recorded Actions run is [40169805354](https://github.com/ZaphodBeeblebrox12/Youtube-Bot-Dashboard/actions/runs/40169805354), which failed one timing-sensitive test because its test clock was hard-coded to midnight; 114 other tests passed. That test has been fixed, but there is no newer Actions result for the current branch head yet. Do not claim the current code has a green full suite until that is confirmed.

