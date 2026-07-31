# NetWorth — Backlog & Roadmap (LLM Reference)

> Shared, append-only-ish work log any assistant can read and extend. When you finish a task,
> move it to **Done** with a date. When you spot new work, add it under the right section with a
> severity/size tag. Keep entries one or two lines; link to `docs/KNOWN_ISSUES.md` items by number
> where relevant. This is a working doc, not a spec — the owner has final say on priority.

**Tags:** `[bug]` `[feat]` `[debt]` `[ops]` `[security]` `[cost]` · size `S/M/L` · e.g. `[debt] M`

---

## Now / high priority

- `[feat] L` **Group-owner approval console + group-scoped approvals.** Today `list_claim_requests`
  / `decide_claim_request` (players lambda) and finance-access approval are effectively SuperAdmin.
  Goal: a group **owner** sees the same approval UI as the SuperAdmin, but **scoped to their own
  group's members** — both claim/profile requests and finance-role requests. Touches: (a) requests
  must carry the target's `group_id` (or resolve it at read time via the group `roles` map);
  (b) `list_claim_requests` filters to groups the caller owns (`canManageGroup`/roles); (c)
  `decide_claim_request` + `_approve_finance_access` authorize a group-owner for their own group;
  (d) frontend shows the approval panel to owners, not just SuperAdmin. **Risk: high — this is the
  live permission system.** Ship on its own branch with careful staging verification; do NOT bundle
  with unrelated changes. (Owner request 2026-07-31.)
- `[feat] M` **Same-side (partnership) filter on the profile card.** Alongside the existing
  opponent head-to-head (`loadProfileHeadToHead` / `compute_head_to_head`), add a "player 1 + player 2
  on the **same** side" view — i.e. how this player performs *with* a chosen partner. Partner data
  already exists (`compute_partnerships`, `compute_partner_distribution`, `compute_head_to_head`'s
  mirror). Likely a contained frontend addition + a small backend "with-partner record" helper.
  Design Q to confirm with owner: put it on the profile card as a second picker next to head-to-head. 
- `[feat] M` **Pagination for large record lists.** `list_matches` / the game log render everything.
  Start with client-side pagination on the game log (simplest, fine for current scale); move to
  server-side DynamoDB `LastEvaluatedKey` only if match counts grow large. (Owner request 2026-07-31.)


- `[security] M` Retire or lock down the legacy open `/finance/{proxy+}` route once nothing
  external depends on it. (KNOWN_ISSUES #1)
- `[debt] M` De-duplicate copy-pasted logic into a **shared Lambda layer**: `sanitize_nickname`,
  `recompute_all_ratings`, `compute_momentum_stats`, `compute_comeback_bonus`, `compute_adaptive_k`,
  `get_pairing_count`, `_is_valid_completed_game`, `_caller_claims`, `_is_super_admin`, `_response`.
  Prioritize the nickname sanitizer — it also has a 4th copy in `app.js`. (KNOWN_ISSUES #6)
- `[security] S` Remove committed AWS account id from `current-policy.json` /
  `networth-deploy-policy.json`; parameterize. (KNOWN_ISSUES #3)

## Next / medium

- `[debt] L` Split `app.js` (6570 LOC, one IIFE) into modules by the existing section banners
  (auth, matches, tournaments, finance, store, profile…). Big win for local-model context limits.
  Watch the inline-`onclick` coupling (KNOWN_ISSUES #11) — needs a wiring pass on `index.html`.
- `[bug] S` Validate live-scoring `point_log` on write so bogus momentum can't be stored
  (replace the reactive `clear_bogus_momentum.py` script). (KNOWN_ISSUES #7)
- `[ops] S` Add a CI guard that fails the build if any `s3 sync --delete` appears in a workflow.
  (KNOWN_ISSUES #9)
- `[ops] S` Stop tracking `__pycache__/*.pyc` in git. (KNOWN_ISSUES #14)

## Later / ideas

- `[feat] L` **Batch voice → LLM → JSON match import (NOT finalized — design only).** Instead of the
  current one-utterance live parse (`nwParseMatchTranscript`), let a user record **multiple** voice
  notes into a log, then send the whole transcript to a qwen3-coder model (local, or self-hosted
  endpoint) that returns a JSON array of matches to insert. Design notes: keep the existing live path
  as-is; add a "batch mode" that accumulates transcripts; POST the log to a configurable model URL
  (owner's local Ollama/LMStudio or a hosted box); validate the returned JSON hard (player-name
  resolution + BWF score validity via the same rules as `_is_valid_completed_game`) before writing;
  show a review/confirm step before any match is recorded. Owner is still deciding the hosting shape.
  (Owner idea 2026-07-31.)
- `[feat] M` Group-scoped finance (currently one shared club finance view/key).
- `[feat] M` Per-group leaderboards & season resets.
- `[feat] S` Export tournament recap as image already exists (`downloadTournamentImage`); extend to
  a shareable per-player season card.
- `[feat] L` Move config out of inline `index.html` script into a generated `config.js`
  (update deploy injection accordingly — KNOWN_ISSUES #10).
- `[cost] S` Evaluate provisioned-capacity DynamoDB vs on-demand once traffic is steady.
  (KNOWN_ISSUES #13)

## Tooling (the doc set itself)

- `[feat] S` Optional: a pre-commit hook that runs `python tools/generate_codebase_map.py` when a
  lambda or `app.js` changes, so the map never goes stale.
- `[debt] S` If you add a new `app.js` section banner, add its line→title to `JS_SECTIONS` in
  `tools/generate_codebase_map.py` (line numbers shift as the file grows — the generator keys off
  the curated map, not live banner detection).

---

## Done

- ✅ 2026-07-31 — **Fix:** voice button showed literal `U0001F3A4` — the code used JS-invalid
  `\U0001F3A4`/`\U0001F534` escapes (capital `U`). Replaced with surrogate pairs
  `\uD83C\uDFA4` (🎤) and `\uD83D\uDD34` (🔴) in `app.js` (5 spots). ASCII-safe, renders correctly.
- ✅ 2026-07-31 — **Fix:** stuck-onboarding loophole. A user who signed up but closed the site
  before verifying could never reach the code screen again. `doLogin` now detects
  `UserNotConfirmedException`, resends a fresh code, and routes to the confirm view (stashing the
  password so confirmation auto-logs them in → profile/claim chooser). Added `doResendConfirmCode()`
  + a "Resend code" button on the confirm view.
- ✅ 2026-07-31 — Repo root cleanup: `git rm` scratch/debug files (`body.json`, `payload.json`,
  `values.json`, `key.json`, `hof.json`, `all_matches_raw.json`, `log.txt`, `old-policy.json`,
  `QR-Code.jpeg`); moved `AUTH_BACKLOG.md` → `docs/`; added scratch-file patterns to `.gitignore`.
  (Files remain in git history — not scrubbed, as none are secrets: `key.json` was just a tournament
  UUID payload.)
- ✅ 2026-07-31 — Created the LLM doc set: `AGENTS.md`, `docs/CODEBASE_MAP.md`,
  `docs/KNOWN_ISSUES.md`, `docs/BACKLOG.md`.
- ✅ 2026-07-31 — Added `tools/generate_codebase_map.py` (reproducible map regenerator, idempotent,
  preserves hand-written sections via AUTOGEN markers) and the change-delivery + release SOP in
  `AGENTS.md` §3a/§3b.
- ✅ 2026-07-29 — Frontend refactor: split monolithic `index.html` into `index.html` + `css/styles.css`
  + `js/app.js`; deploy pipeline updated to `cp` css/ and js/ separately.
- ✅ 2026-07 — Cognito claim/identity repair (mis-stamped profiles, stranded users).
- ✅ 2026-07 — Animated WebP cosmetic support; match-session safety net (localStorage fallback +
  re-login modal); staging environment (`infrastructure/staging.yaml`).
- ✅ 2026-07-19 — Went live during a real tournament.
- ✅ Earlier — Finance tiered access; owner-editable UPI QR; match-approval workflow + SuperAdmin
  recompute; XP/levels/coins/store/quests economy; login-by-nickname; profile-completion modal
  (create/claim); self-service avatar/banner; display-mode toggle; guest visibility restrictions;
  nickname as unique id; profile cards with banner header; walk-in guest support; weekly approval
  backfill Lambda (`progress_scheduler`).

---

### How to append (for future LLMs)
1. Pick the smallest correct section (Now / Next / Later / Tooling / Done).
2. One line, tagged, with a KNOWN_ISSUES cross-ref if it's a known trap.
3. On completion: move to **Done** with an ISO date, and note in the change whether
   `CODEBASE_MAP.md` needs regenerating.
