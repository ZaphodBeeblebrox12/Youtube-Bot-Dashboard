# Handoff — Four AI Providers & Provider Analytics

**Date:** 2026-10-25  
**Repository:** https://github.com/ZaphodBeeblebrox12/Youtube-Bot-Dashboard  
**Feature branch:** `feature/four-provider-analytics`  
**Base commit:** `5c178d3342113c2f3e4d9941a5f89d6fc406d77d`  
**Starting branch head before this handoff file:** `727b3d67c9d7dc8d4f119e6204cc629bc2c20c37`  
**Status:** Implementation in progress; test execution and review are outstanding.

## User approval and boundaries

The user approved implementing both workstreams together:

1. Add DeepSeek and Kimi as optional AI providers alongside Gemini and Qwen.
2. Expand provider analytics to compare request volume, reliability, latency, usage, prompt-cache activity, and configurable model-specific cost estimates.

Work was made only on `feature/four-provider-analytics`, created from the base commit above. Do not merge to `main`, create/deploy a release, or enable providers in a live environment without the user's further direction. No secrets/API keys have been added. A PR has not been opened.

## What has been added on the feature branch

### Four-provider routing
- Extended `ProviderName` with `DEEPSEEK` and `KIMI`.
- Added `DEEPSEEK_ONLY` and `KIMI_ONLY` fixed-provider strategies.
- Extended the worker's fixed-provider availability checks and Settings provider/strategy controls.
- Preserved independent persistent round-robin routing for `MODERATION` and `REPLY_FINDER`.
- The default remains Gemini + Qwen. DeepSeek and Kimi are optional and must be configured/enabled explicitly.
- Added optional environment configuration:
  - `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`
  - `KIMI_API_KEY`, `KIMI_BASE_URL`, `KIMI_MODEL`
  - Kimi also accepts `MOONSHOT_API_KEY`.
- Default adapter base URLs are set in `.env.example`, but actual credentials, model availability, region and model parameters have not been smoke-tested.

### Provider adapters and usage fields
- Added `src/youtube_bot_dashboard/providers/deepseek.py`.
- Added `src/youtube_bot_dashboard/providers/kimi.py`.
- Registered both in `providers/registry.py`.
- Extended the shared `ProviderResponse` with optional `cache_write_tokens`.
- Added Kimi cache-read/cache-write parsing and DeepSeek cached-input parsing. Qwen also reads cache-write fields when exposed by the endpoint.
- Worker completion now persists cache-write usage and includes it in the returned summary.
- Provider adapters use the existing OpenAI-compatible SDK for DeepSeek, Kimi and Qwen; Gemini retains its existing SDK adapter.

### Persisted usage analytics
- Added nullable `cache_write_tokens` to `LiveAIRequestBatch`.
- Added an additive compatibility migration in `storage.create_schema()` to add the column to existing databases where missing. Verify this against both supported SQLite test DBs and the intended PostgreSQL environment; formal versioned production migrations remain a project concern.
- Added `src/youtube_bot_dashboard/provider_analytics.py` to aggregate sent requests by provider, model and pipeline.
- Analytics currently computes request counts, success/failure/in-flight counts, recorded token totals, cache token counts/hit-rate when cache usage is reported, average and P95 latency, and estimated cost only when a matching rate card and token usage are available.
- Added model-specific USD-per-million-token rate cards stored via `AppSettingRecord`. Settings lets the user enter uncached-input, cache-hit, cache-write, and output prices for the configured model. No rates are hard-coded; cost shows as unknown until the card is fully configured.
- Analytics page now includes four provider status blocks, a reporting-window selector, an overall table, and a Moderation-vs-Reply-Finder table.
- Cost estimates are approximate token-rate estimates. They do not reconcile invoices, taxes, discounts or any provider-specific fees.

### Docs and tests added
- Updated `.env.example`.
- Updated `README.md` feature/status notes.
- Added:
  - `tests/test_provider_extensions.py`
  - `tests/test_provider_analytics.py`
  - `tests/test_four_provider_routing.py`

## Verification status — important

**The new code and tests have not been executed in this work session. Do not report the branch as passing tests or production-ready.** The previously known `main` baseline at the base commit had a reported successful run of 108 tests; that does not verify these new changes.

No live provider API calls have been run. No deployed app/browser verification has been performed. The repository connector's workflow-run query is filtered to pull-request runs, and there is no PR for this branch, so no new CI result was verified here.

## Next steps, in order

1. Fetch/check out `feature/four-provider-analytics` and run the full test suite, beginning with:
   ```bash
   python -m pip install -e ".[dev,ai]"
   pytest -q
   ```
   Run syntax/import checks as well. Fix failures before treating this as ready for review.
2. Review the new tests and run regression coverage for the existing 108-test suite, especially provider registry behavior, both-pipeline round robin, fixed-provider mode, retry assignment, schema validation, Session Shield, retention cleanup, and Settings persistence.
3. Validate the new response parsing against the current official APIs and the exact models the user will configure. Ensure absent cache fields remain “not reported,” not zero. Add fixtures for the actual DeepSeek, Kimi, and Qwen usage payloads. Consider exposing additional cache-miss counts where present.
4. Verify cost formulas against each provider's definition of input usage and cache usage. The current generic formula assumes `input_tokens` includes cache-hit/cache-write token counts and subtracts those from the normal-input portion; confirm each endpoint's semantics before relying on totals. Cache-hit rate should be shown as unavailable when denominators are not truly comparable.
5. Verify Settings UI end-to-end: provider multiselect, fixed-only strategies, one pricing form per provider, clearing/saving rate cards, and time-window analytics. Test an existing database before/after the additive cache-write column addition.
6. Confirm cache behavior per configured provider/model. The adapter implementation currently relies on provider-managed caching; it does not add a reusable application response cache or a dedicated explicit-cache lifecycle. Do not cache moderation classifications across different chat contexts.
7. Once test/implementation findings are fixed, report exact test results and commit SHA to the user. Keep the branch unmerged and undeployed until the user reviews and approves the next step.

## Official API documentation to verify

- DeepSeek prompt/context cache usage: https://api-docs.deepseek.com/guides/kv_cache/
- Qwen Model Studio context cache, OpenAI-compatible usage fields and region differences: https://help.aliyun.com/en/model-studio/context-cache
- Gemini caching and model-specific requirements: https://ai.google.dev/gemini-api/docs/caching
- Kimi/Moonshot context caching: https://platform.kimi.ai/docs/guide/context-caching
- Kimi API overview and Chat Completions compatibility: https://www.kimi.ai/help/kimi-api/api-overview

## Known product decisions

- Keep round robin as the baseline; performance-aware routing is not implemented.
- Keep pipeline rotations independent.
- Provider is assigned per batch; retries should retain the batch's assigned provider.
- Leave Gemini and Qwen as defaults until the owner explicitly enables additional providers.
- Provider-managed prompt caching is distinct from caching completed classification responses. Do not reuse an old moderation result for newly changed chat context.
- Cost must never be fabricated from an unknown model/rate card.
- No changes to `main`, no merge, no deployment, and no activation of real YouTube moderation enforcement.
