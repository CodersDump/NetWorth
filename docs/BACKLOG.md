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
  - **Stage 2 — backend read/write scoping (DONE 2026-07-31).** Finance lambda now scopes every
    record op to a `group_id` (defaults to the "Club (default)" group so the current UI is unchanged):
    `_group_for_request`, `_group_finance_level`, `_default_group_id`; `_scan_type`/`list_records`/
    `create_records`/`update_record`/`delete_record`/`summary`/`insights`/`_settlement_rows` all take
    a group; update/delete reject cross-group records; public walk-ins scoped to the default group.
    Access = SuperAdmin (all) / owner+admin (full on own group) / per-group `finance_roles` map /
    legacy global grant as a floor **on the default group only** (no cross-group leak). Added
    `GROUPS_TABLE` env to the finance function. Verified with an access-matrix unit test.
    **Stage 2b (still to do):** a set-per-member-finance-role endpoint in the groups lambda +
    `finance_roles` seeded on group creation — folded into Stage 3 where the UI to manage it lives.
  - **Stage 3 — frontend + owner approval (DONE 2026-07-31, one caveat).** Finance tab has a **group
    selector** (`populateFinanceGroups`, `finance_group_select`); `currentFinanceGroupId` is threaded
    through `finQS`/`finPost`, and any logged-in user now routes through `/finance-secure` so claims
    reach the Lambda. Removed the finance Lambda's coarse global gate (was blocking group owners) —
    access is per-group per-method. Broadened the shared-key handout (`_has_any_group_finance`) so
    owners can unlock during transition. **Request→approve flow (complete):** members request finance
    access scoped to their group (`group_id` on the action-request); owners see & approve in the
    Reviews requests panel (`finance_access` added to `OWNER_DECIDABLE_TYPES`, `_owner_may_decide`
    made group-precise); approval sets the **per-group** role (`_approve_finance_access` writes the
    group's `finance_roles` map). **Direct-set flow:** groups lambda `set_finance_role` +
    `/group-finance-role/{group_id}/{player_id}` route (template) + `setGroupFinanceRole` frontend
    helper — all in and tested. **Caveat / follow-up:** the owner-facing *panel* to browse group
    members and set their roles inline isn't surfaced yet (the existing role panel stays
    SuperAdmin-global to avoid regression); owners grant via the request→approve flow today, and the
    direct-set endpoint is ready to wire into an owner panel next. Needs hands-on staging test.
  - **Stage 4 — per-group time slots (DONE 2026-07-31).** Groups store `slots` + `slot_members`
    (owner/admin-set via the new Cognito route `PUT /group-slots/{group_id}` → `set_group_slots`;
    validated: only real members, only existing slots). `get_group`/`list_groups` now return them.
    Frontend: slot list + per-slot member assignment in the group detail (`manageGroupSlots`,
    `assignSlotMembers`, owner/admin only).
  - **Stage 4b — "own settlement only" member view (DONE 2026-07-31).** `GET /finance/my-settlement`
    (`my_settlement`) returns a member's OWN dues per (month, slot): what they owe (`cost_per_head`)
    and what's owed back (`residual_per_head` - the walk-in-share/relief refund), plus net. Available
    to any group member with no view key or finance role; expenses and others' numbers never exposed.
    Frontend "My dues" card on the Finance tab (`loadMyDues`), visible to any member. Math unit-tested.
  - **Stage 4c — slot-scoped FULL-ledger view (still to do).** A view-access member assigned to a slot
    should see only that slot's expenses/walk-ins in the main finance tab (not just their own dues).
    Needs slot-filtering threaded through `list_records`/`summary` for non-owner view members. The
    member's own dues are already slot-safe via 4b; this is the remaining nice-to-have.
  - **Stage 5 — co-owners, ownership transfer, per-group payee (DONE 2026-07-31).** Ownership
    transfer (owner-only) via `transfer_to` on `PUT /group-slots/{group_id}` — old owner demotes to
    regular member (view access). Per-group `finance_payee` ({player_id, upi_id, upi_name}, must be a
    member) set by any owner/admin. Co-owners: use the existing role control to promote a member to
    owner/admin. `get_group`/`list_groups` return `finance_payee`. Frontend: Transfer ownership + Set
    payee controls in the group detail. Transfer + payee logic unit-tested.
  - **Stage 6 — member dues + UPI tap-to-pay (DONE 2026-07-31).** `my_settlement` now also returns
    the group's payee (VPA + name, member-gated). The "My dues" card shows a "Pay ₹X via UPI" button
    that builds a `upi://pay?pa=...&am=...&cu=INR` deep-link the phone hands to the user's UPI app.
    NetWorth processes nothing and gets no confirmation, so paying does NOT mark you paid (manual,
    owner-side) and the UI says so. One payee per group today, so it's one button; multi-payee split
    is only needed if a member spans groups with different payees (Stage 6b if it comes up).
  - **Security guardrails (apply across Stages 5–6).** NetWorth never processes payments — UPI
    tap-to-pay only builds an `upi://pay?pa=...&am=...` deep-link the OS hands to the user's UPI app;
    no money, bank details, or card data flow through the app. Two things to gate carefully: (a) a
    payee **VPA** is member-visible only (Cognito-gated per group), NOT on a public route unless the
    owner explicitly wants a public pay page; (b) the per-member **dues** endpoint returns only the
    caller's own breakdown (owners see their group's). The pre-filled amount is editable in the payer's
    app and the app gets no payment confirmation, so **"mark as paid" stays a manual step** — never
    auto-reconciled — and the UI must say so.
  - **Final — cut-over (destructive, LAST).** Retire the shared `FINANCE_VIEW_KEY` + the legacy open
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
- `[feat] S` ~~Export tournament recap as image already exists (`downloadTournamentImage`); extend to
  a shareable per-player season card.~~ **DONE in v1.8** — `frontend/js/card-share.js` adds a
  spotlight-carousel customizer (Share card button on the Player Card) that renders the player's
  stats card to a canvas and shares via the Web Share API (`navigator.share({files})`), falling back
  to PNG download. Two customizable axes: **background** (free presets + store `background_image`
  cosmetics) and **frame** (free Minimal + store `card_frame` cosmetics, drawn as PNG overlays on the
  export). Both owned/locked through the existing store (`owned_items` + `/store-purchase`); locked
  picks render dulled with a baked watermark and the export refuses to bake a locked frame. Equip
  persists via `/update-my-card` (new `card_frame_url` field; `background_url`/`background_id` reused).
  Backend: `card_frame` added to `_owns_store_cosmetic`, new `card_layout` value-cosmetic +
  `_owns_card_layout`/`FREE_CARD_LAYOUTS`, both new fields serialized. Follow-ups:
    - **DONE in v1.9.0** — preset frames & backgrounds sellable from the UI with no art assets. New
      `card_frame_preset` / `background_preset` value-cosmetics; visuals code-defined in `card-share.js`
      (css preview + canvas painters, incl. the Holo glass frame). SuperAdmin adds them by picking a
      preset from a dropdown in the store form (`window.NW_CARD_PRESETS` feeds it). Equipped state
      persists to `card_frame_preset` / `background_preset`, mutually exclusive with the image/id fields
      (`_owns_value_cosmetic` gates ownership). Add a new preset later = add to the lib in card-share.js
      (css class + canvas painter); selling it is then pure admin.
    - `[feat] M` Premium **stats layouts** (curve / donut graphs) as `card_layout` cosmetics — backend
      field already accepts them; needs the per-layout canvas draw code + a third customizer axis.
      **DONE in v1.10.0** — Stats is now a third customizer axis (Full / Compact free, Rating-curve /
      Season-donut premium via `card_layout` presets), with canvas draws for each in the export. Also
      shipped: more frame presets (Ice/Plasma/Ruby/Chrome added to Gold/Holo/Carbon/Neon), fancy
      backgrounds (Nebula/Ember/Blueprint) moved off the free defaults into store `background_preset`
      items alongside new ones (Ocean/Sunset) + **animated** presets (Aurora Drift, Nebula Pulse —
      animate in the live preview; the still export bakes a representative frame), and **preset preview
      swatches** in both the player store and the admin store list (`NW_CARD_PRESETS.swatchHtml`).
      Free background defaults trimmed to Court + Plain.
    - `[feat] S` Use the player's avatar image (not just initials) on the exported card. **DONE in
      v1.10.0** — avatar image is loaded (crossOrigin) and drawn on all four export layouts.
    - `[feat] M` **Animated background export** — animated bg presets currently export as a static
      frame; a true animated share would need WebP/APNG frame encoding on the canvas.
    - **v1.10.1** — SuperAdmin gets every cosmetic unlocked (customizer hides locks; `update_my_card`
      honours the same bypass via `_is_super_admin`). Added a **Flame** frame preset.
    - `[idea]` FC/FUT-style **card silhouette** (angled corners) — needs clip-path in the preview and a
      matching canvas path (bg clip + frame stroke) in the export; decide global shape vs per-frame.
- `[feat] M` Per-group leaderboards & season resets (would let the card say "season" instead of all-time).
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

- ✅ 2026-08-03 — **Feature:** ranking eligibility — only players with **5+ games** are ranked. A
  rating from 0-4 games is mostly noise, so those players are shown separately as "provisional (N/5
  games)", unranked. `recompute_all_ratings` and the record-match path now track `games_played` on each
  player (exposed in the players list + group members). **Backend change — needs a prod deploy, then
  click "Recompute all ratings" ONCE to backfill games_played for existing players.**

- ✅ 2026-08-03 — **Feature:** edit PLAYERS in a recorded match, not just the score. `update_match`
  (matches lambda) now optionally accepts `team_a`/`team_b` (validated: right size, no player on both
  teams, all must exist) and recomputes every rating. Game-log Edit opens a themed modal with per-team
  player pickers + scores (SuperAdmin); non-admins keep the score-only request flow. **Backend change —
  needs a prod deploy.**

- ✅ 2026-08-03 — **Bug fix:** match reorder (review day) drag-and-drop now works. `dragstart` never
  stored the source index and `getData()` is unreadable during `dragover` (protected mode), so `from`
  was always NaN and nothing moved. Now the index is stashed on dragstart, and the reorder happens once
  on `drop` (with a green insertion line on hover) instead of rebuilding the list mid-drag, which was
  destroying the dragged row.

- ✅ 2026-08-02 — **Feature (money):** slot-less / group-wide expenses & walk-ins. A record with no
  slot goes to a group-wide bucket whose cost/residual splits across the DISTINCT Yes members across
  every slot that month (counted once even if in two slots). `_settlement_rows` sets the group-wide
  bucket's player_count to that distinct count so the same per-bucket math applies; `my_settlement` and
  `insights` add each distinct member's group-wide cost + relief (a "(whole group)" line). Slot is now
  optional on the expense/walk-in forms ("— whole group —"); membership still requires a slot.
  Unit-tested (300 split across 3 distinct = 100 each, Z-in-both-slots counted once; group-wide walk-in
  feeds group-wide residual; real slots unaffected). Note: group-wide has no per-member payment tracking
  (always 'collecting') and no forfeit — edge cases for later.
- ✅ 2026-08-02 — **QoL:** the Finance tab remembers your last month/slot (localStorage), restored on
  open, so you don't re-pick every visit.

- ✅ 2026-08-01 — **Bug fix (money reference):** per-slot relief no longer cross-contaminates. A member
  in two slots was getting BOTH slots' relief subtracted on EACH slot's effective (10−5 / 15−5 instead
  of 10−2 / 15−3). `_member_relief` now takes an optional `slot` filter; the per-slot card, confirm-store
  and settled-check pass it, while the aggregated Insights row keeps the summed total. Unit-tested.

- ✅ 2026-08-01 — **Feature + fix:** (1) themed modal system — `nwConfirm`/`nwAlert`/`nwPrompt`
  (Promise-based) replace ALL 137 native browser confirm/alert/prompt calls; styled via the app's CSS
  vars so they follow light/dark automatically (Esc/click-out cancels, Enter confirms). (2) Race-fix:
  the Finance tab now loads groups BEFORE rendering the dues card + ledger selector, so they no longer
  intermittently fail to appear (they read allGroups, which was sometimes still loading).

- ✅ 2026-08-01 — **Feature (money):** payment confirmation now uses the relief-adjusted (effective)
  amount. Members list returns per-member `relief` + `effective` (cost_per_head − relief); the card
  shows "Pay ₹X (₹cost − ₹relief relief)"; the confirm dialog and paid badge use effective. Confirm
  now STORES the effective amount, and "settled" is checked against it (second pass in
  `_settlement_rows`, since relief is cross-month). Shared `_prev_period`/`_member_relief` helpers are
  the single source of truth (insights refactored onto them). Math unit-tested (825 − 175 relief =
  650; settled matches on 650, not the pre-relief 825).



- ✅ 2026-08-01 — **Feature (money):** residual forfeit + redistribute. A membership can be marked
  `forfeit_residual` (owner-only "Forfeit refund" toggle on the card); that member's relief becomes ₹0
  and the residual pool is split among the remaining Yes members of that (month, slot) - each gets
  more. Cost-per-head is unchanged (only the refund is affected). `_settlement_rows` computes
  `active_count = player_count - forfeit_count` and divides residual by active_count; `my_settlement`
  and `insights` give forfeiters 0 relief. Redistribution math unit-tested (225 forfeited -> others
  225->300, forfeiter 0, cost 825 unchanged).

- ✅ 2026-08-01 — **Script:** `link_finance_to_profiles.py` links membership/walk-in finance records
  to player PROFILES (by player_id) and normalizes display names (e.g. "prasanna" -> "Prasanna
  Varade"). Matters because my_settlement matches dues by player_id, so unlinked records never show in
  a member's My Dues. Conservative: auto-links only confident single matches, flags the rest for a
  `--map` override. Dry-run/idempotent.

- ✅ 2026-08-01 — **UX:** membership status changes no longer reload the whole section on every
  toggle. Each change still saves instantly; a **Recalculate amounts** button (with a "saved — click
  Recalculate" hint) appears and recomputes per-head figures once, when you're done. Removed the
  auto/debounced reload.

- ✅ 2026-08-01 — **Feature + script:** owner-facing member finance-access panel. In the group
  detail, owners/admins now see each member with a dropdown to set their finance access
  (no finance / view / edit / delete) inline — wires the Stage 3 `setGroupFinanceRole` endpoint into
  the UI (get_group now returns `finance_roles`). Non-managers see no controls (empty). Default is
  "no finance" so members only see their My Dues card, not the expense ledger. `scripts/
  map_july_walkins_to_group.py` re-homes a month's walk-ins under a named group (default Matchpoint,
  July 2026), slots untouched (dry-run/idempotent).

- ✅ 2026-08-01 — **Fix + scripts:** wired group slots into the Finance forms (`populateFinanceSlots`
  fills the expense/membership/walk-in slot dropdowns from the selected group's `slots`, fallback to
  the default pair) — Stage 4 defined slots but never connected them to these forms.
  `scripts/backfill_group_slots.py` sets default slots `["7AM-8AM","8AM-9AM"]` on any group without
  them (dry-run/idempotent). `scripts/reset_august_unpaid.py` removes `payment_confirmed_amount` from
  a month's memberships (default August 2026) to reset them to unpaid mid-collection (dry-run/idempotent).

- ✅ 2026-08-01 — **Feature + fix:** interactive zoom/pan on the rating-history chart
  (`chartjs-plugin-zoom`, Chart.js 4.4.1). Gestures reworked so they don't fight page scroll:
  **drag a box** to zoom, **Shift+drag** to pan, **Ctrl+scroll** to zoom (desktop), **pinch** on
  mobile. Added `touch-action: pan-y` on the canvas so the browser stops eating the pinch as a page
  zoom while one-finger vertical scroll still works. "Reset zoom" button (`resetRatingZoom`).
  Frontend-only; min zoom ~2 days. Mobile fix: added **Hammer.js** (required by the zoom plugin for
  touch pinch/pan - without it desktop worked but mobile pinch was dead) and `touch-action: none`.
  Fix 2: bounded zoom to the data extent (`min/max: 'original'`) with a mode-correct `minRange`
  (match-count in sequence mode, ms in time mode) - pinch-out no longer collapses the chart to a
  flat line with no way back.
- ✅ 2026-08-01 — **Feature:** SuperAdmin claim-audit panel (User → profile mapping). Surfaces
  `claim_audit.py` in-app: Reviews-tab section shows every account's linked player, flags broken
  links at top (no_profile / dangling / claimed_unlinked / misstamp), with **re-link** (point an
  account at a player, stamp both directions) and **unlink** (clear the link, optionally strip a
  mis-stamped email) actions. Players lambda `audit_claims` + `claim_audit_action`, `/claim-audit`
  GET/POST route (Cognito, SuperAdmin), IAM `AdminDeleteUserAttributes` added. Classify logic
  unit-tested. Fixes the "logged in, no profile linked" case (e.g. Suren).
- ✅ 2026-07-31 — **Group-scoped finance Stage 6 (UPI tap-to-pay) + UI fixes.** "My dues" card now
  builds a `upi://pay` deep-link to the group's payee ("Pay ₹X via UPI"); payment stays manually
  marked (no auto-confirm), stated in the UI. Fixes: (1) finance expense/walk-in tables + the game
  log render single-line and scroll horizontally instead of wrapping/bleeding; (2) membership status
  toggles save instantly but the recompute/reload is debounced so bulk-adding doesn't flash the whole
  section each change; (3) logout now switches off a now-hidden admin tab (Reviews/Store) instead of
  leaving its panel visible until refresh.
- ✅ 2026-07-31 — **Group-scoped finance Stage 5 (co-owners, transfer, payee) + deploy cache fix.**
  Ownership transfer (owner-only, old owner → member) and per-group `finance_payee` via
  `PUT /group-slots/{group_id}`; co-owners via existing role promotion; group detail UI. Also added
  `--cache-control "no-cache"` to the frontend upload in deploy.yml so a stale `app.js` can't sit in
  the browser cache after a deploy (the cause of "loadUnconfirmedUsers is not defined").
- ✅ 2026-07-31 — **Group-scoped finance Stage 4 + 4b (per-group slots + member dues).** Groups get
  owner-managed `slots`/`slot_members` via `PUT /group-slots/{group_id}` (`set_group_slots`, new
  Cognito route); group detail UI for slot list + assignment. `GET /finance/my-settlement`
  (`my_settlement`) gives any member their own dues/owed-back per slot with expenses hidden; "My dues"
  card on the Finance tab. Slot + settlement logic unit-tested. Slot-scoped full-ledger view for
  view-members is deferred as Stage 4c.
- ✅ 2026-07-31 — **Feature:** SuperAdmin unconfirmed sign-ups tool. Lists Cognito accounts stuck
  in UNCONFIRMED (signed up, never verified) and lets an admin delete one so that email can register
  again. New players-lambda `list_unconfirmed_users` / `delete_unconfirmed_user` (SuperAdmin-gated;
  delete refuses any non-UNCONFIRMED account), `/unconfirmed-users` GET+DELETE route, and a
  "Unconfirmed sign-ups" section in the Reviews tab. Pairs with the earlier signup-recovery fix.
- ✅ 2026-07-31 — **Group-scoped finance Stage 3 (frontend + owner approval).** Finance-tab group
  selector; group_id threaded through all finance calls; logged-in users routed to `/finance-secure`;
  members request finance access per-group → owners approve in Reviews (sets per-group role); groups
  lambda `set_finance_role` + `/group-finance-role` route + `setGroupFinanceRole`. Fixed a coarse
  global gate that blocked group owners. Backend logic unit-tested. Caveat: owner-facing inline
  role-set panel not surfaced yet (grant via request→approve); needs staging test.
- ✅ 2026-07-31 — **Group-scoped finance Stage 2 (backend scoping).** Finance lambda scopes all
  record reads/writes/deletes + summary/insights to a `group_id` (defaults to "Club (default)");
  access resolved per-group (`_group_finance_level`) with a default-group-only legacy floor; added
  `GROUPS_TABLE` env. Backward-compatible: the current finance UI sends no `group_id`, so it operates
  on the default group exactly as before. Access matrix unit-tested.
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
