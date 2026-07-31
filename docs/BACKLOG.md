# NetWorth — Backlog & Roadmap (LLM Reference)

> Shared, append-only-ish work log any assistant can read and extend. When you finish a task,
> move it to **Done** with a date. When you spot new work, add it under the right section with a
> severity/size tag. Keep entries one or two lines; link to `docs/KNOWN_ISSUES.md` items by number
> where relevant. This is a working doc, not a spec — the owner has final say on priority.

**Tags:** `[bug]` `[feat]` `[debt]` `[ops]` `[security]` `[cost]` · size `S/M/L` · e.g. `[debt] M`

---

## Now / high priority

- `[feat] M` **Finance approval for group owners — blocked on group-scoped finance.** Owners can
  now approve claim/rename requests for their group (done 2026-07-31), but NOT finance-access:
  finance roles are still club-**global**, so an owner granting one would hand access across every
  group. To let owners approve finance for their members, finance must first become group-scoped
  (below). Then add `finance_access` to `OWNER_DECIDABLE_TYPES` and un-hide the Finance-access panel
  in `updateReviewTabScope`. (Owner request 2026-07-31.)
- `[feat] L` **Group-scoped finance — staged rollout (in progress).** Decided design: every finance
  record belongs to exactly one group (fully separate ledgers); the group **owner has full finance**
  for their group, others get a **per-group** finance role; existing records migrate under a new
  **"Club (default)"** group; and finance moves **off the shared view-key** onto Cognito group-role
  gating, retiring the legacy open `/finance/{proxy+}` route. Sequenced so a live ledger is never at
  risk:
  - **Stage 1 — migrate data (DONE 2026-07-31, additive/safe).** `scripts/backfill_finance_groups.py`
    creates the "Club (default)" group and stamps `group_id` on every existing finance record.
    Dry-run by default, idempotent, only adds an attribute. Run locally after committing; no deploy.
  - **Stage 2 — backend read/write scoping.** Finance lambda: every op requires a `group_id`; access =
    owner/admin of that group OR a per-group finance role (SuperAdmin sees all). Add a per-group
    `finance_roles` map + a set-role endpoint in the groups lambda. Keep the shared key path working
    during transition. Needs `GROUPS_TABLE` + `PLAYERS_TABLE` on the finance function (verify env).
  - **Stage 3 — frontend.** Group selector on the Finance tab; owner sees full control for their
    group, members see per-group-role UI. Then un-hide the finance-access panel for owners and add
    `finance_access` back to `OWNER_DECIDABLE_TYPES` (unlocks owner finance approval).
  - **Stage 4 — cut-over (destructive, LAST).** Retire the shared `FINANCE_VIEW_KEY` + the legacy open
    `/finance/{proxy+}` route once Stages 2–3 are verified in prod. Route/template change + a
    KNOWN_ISSUES update. Do NOT do this before the new path is proven.

- `[feat] L` **Group-scoped finance.** Today there's one shared club finance (one view-key, one
  global `finance_role` per player). Make finance per-group so each group owner manages their own.
  Prereq for the finance-approval item above. (Was in Later; promoted because two requests depend
  on it.)


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

- ✅ 2026-07-31 — **Group-scoped finance Stage 1 (data migration).** Added
  `scripts/backfill_finance_groups.py`: creates the "Club (default)" group and stamps `group_id`
  on every existing finance record. Dry-run by default, idempotent (`attribute_not_exists` guard),
  additive-only. Stages 2–4 (backend scoping, frontend, key/route cut-over) tracked above.
- ✅ 2026-07-31 — **Feature:** partner match-list. The "with a partner (same side)" view now lists
  every same-side match under the W/L summary — date, opponents, score, result — most-recent-first,
  paginated 25/page (`renderPartnerGames` / `partnerGamesGoto`). Backend `compute_with_partner` now
  also returns a `games` array (opponents resolved from whichever side the pair wasn't on).
- ✅ 2026-07-31 — **Fix:** partner filter returned 400. `compute_with_partner` was wired into
  `list_matches`, but the `/profile-secure` gate (`profile_view_enforced`) resolves the target
  player from a param whitelist that didn't include `with_partner`, so the request was rejected
  ("no player specified") before reaching the dispatch. Added `with_partner` to that whitelist.
- ✅ 2026-07-31 — **Feature:** same-side (partnership) filter on the profile card. Backend
  `compute_with_partner()` (matches lambda) + `with_partner`/`partner` query; frontend
  `loadProfileWithPartner()` + a "With a partner (same side)" picker under Head-to-head.
- ✅ 2026-07-31 — **Feature:** game-log pagination. `loadGameLog` now fills state and hands off to
  `renderGameLog()` + `gameLogGoto()` (25/page, Prev/Next, "X–Y of N · page a/b"). Filtering still
  runs on the full set; only display is paged.
- ✅ 2026-07-31 — **Feature:** group-owner approvals (partial). A group owner/admin now sees the
  Reviews tab and can approve/reject **claim** and **rename** requests **scoped to their own group's
  members** (players lambda: `_caller_owned_group_ids`, `_player_group_ids`, `_owner_may_decide`,
  `OWNER_DECIDABLE_TYPES`; `GROUPS_TABLE` env added — no IAM change, shared role already had read).
  Frontend shows the tab to owners and hides every SuperAdmin-only panel (`updateReviewTabScope`).
  **Held on purpose:** finance-access approval and delete/match-change approvals stay SuperAdmin-only
  (see Now/high-priority for why finance is held). `new_profile` requests carry no group, so they
  also stay SuperAdmin-only for now.
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
