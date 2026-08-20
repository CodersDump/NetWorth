# NetWorth — Backlog & Roadmap (LLM Reference)

> Shared, append-only-ish work log any assistant can read and extend. When you finish a task,
> move it to **Done** with a date. When you spot new work, add it under the right section with a
> severity/size tag. Keep entries one or two lines; link to `docs/KNOWN_ISSUES.md` items by number
> where relevant. This is a working doc, not a spec — the owner has final say on priority.

**Tags:** `[bug]` `[feat]` `[debt]` `[ops]` `[security]` `[cost]` · size `S/M/L` · e.g. `[debt] M`

---

## Now / high priority

- `[feat] M` **Finance approval for group owners — DONE (verified 2026-08-19).** This entry said
  "blocked on group-scoped finance" but the blocker was cleared during the Stage 2/3 work below and
  the doc was never updated to say so. Verified end-to-end this session: `finance_access` is in
  `OWNER_DECIDABLE_TYPES`, `_owner_may_decide` group-scopes it via the request's `group_id`, requests
  render in the same `#settings-requests-list` the claim/rename panel uses (so `updateReviewTabScope`
  already showed them to owners — there was never a separate hidden "Finance-access panel" to
  un-hide), and the owner-facing per-member role selector (`setGroupFinanceRole`) is live in the group
  detail view gated on `canManageGroup`, not SuperAdmin-only. No code change needed here, just this
  correction. (Owner request 2026-07-31.)
- `[feat] L` **Group-scoped finance — staged rollout (all stages done, only the destructive cut-over
  left).** Decided design: every finance
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
  - **Stage 3 — frontend + owner approval (DONE 2026-07-31; caveat closed 2026-08-19).** Finance tab
    has a **group selector** (`populateFinanceGroups`, `finance_group_select`); `currentFinanceGroupId`
    is threaded through `finQS`/`finPost`, and any logged-in user now routes through `/finance-secure`
    so claims reach the Lambda. Removed the finance Lambda's coarse global gate (was blocking group
    owners) — access is per-group per-method. Broadened the shared-key handout
    (`_has_any_group_finance`) so owners can unlock during transition. **Request→approve flow
    (complete):** members request finance access scoped to their group (`group_id` on the
    action-request); owners see & approve in the Reviews requests panel (`finance_access` added to
    `OWNER_DECIDABLE_TYPES`, `_owner_may_decide` made group-precise); approval sets the **per-group**
    role (`_approve_finance_access` writes the group's `finance_roles` map). **Direct-set flow:**
    groups lambda `set_finance_role` + `/group-finance-role/{group_id}/{player_id}` route (template) +
    `setGroupFinanceRole` frontend helper — all in and tested. **Owner panel (confirmed live
    2026-08-19):** the group detail view already renders a per-member finance-role `<select>`
    (none/view/write/delete) for every plain member, gated on `canManageGroup` — not SuperAdmin-only.
    The "still to surface" caveat that used to live here was stale; nothing left to build.
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
  - **Stage 4c — slot-scoped FULL-ledger view (DONE 2026-08-19).** A plain `view`-level grant (not
    write/delete, not owner/admin — those stay unrestricted so they can manage the whole ledger) is now
    narrowed to their assigned slot(s) + the group-wide bucket in the main finance tab, not just their
    own dues. Backend: new `_view_scope_slots` (finance lambda) reads the group's `slot_members` map and
    returns `None` (unrestricted) or a set of allowed slot keys; threaded through `list_records` (all
    three record types — expenses, walk-ins, **and** membership rosters, so a slot-scoped viewer can't
    see who's enrolled in a slot that isn't theirs) and `summary`. Both responses include `scoped_to`
    when restricted. Extracted the previously-nested `_slot_key` helper to module scope so both
    `_settlement_rows` and the new scoping share one normalizer (avoids yet another KNOWN_ISSUES #6
    duplicate). Frontend: a `finance-scope-note` banner on the Finance tab explains why the ledger looks
    shorter than the whole club's when scoped. Verified with a standalone access-matrix script (view
    sees own slot + group-wide only; write/delete/owner/admin see everything unrestricted).
  - **Stage 5 — co-owners, ownership transfer, per-group payee (DONE 2026-07-31; co-owner control
    actually built 2026-08-20).** Ownership transfer (owner-only) via `transfer_to` on
    `PUT /group-slots/{group_id}` — old owner demotes to regular member (view access). Per-group
    `finance_payee` ({player_id, upi_id, upi_name}, must be a member) set by any owner/admin.
    `get_group`/`list_groups` return `finance_payee`. Frontend: Transfer ownership + Set payee controls
    in the group detail. Transfer + payee logic unit-tested. **Correction (Owner-reported 2026-08-20):**
    this entry claimed co-owners worked via "the existing role control" - that control never actually
    existed. The group detail only ever rendered a *read-only* role badge; the backend
    (`PUT /group-role/{group_id}/{player_id}`, `set_role`) already allowed any owner/admin of a group to
    set a member's role including 'owner', but nothing in the frontend called it. Built the missing
    piece: the role badge is now an editable `<select>` (member/admin/owner) for anyone `canManageGroup`,
    calling the now-wired `setGroupMemberRole`. A self-demotion away from 'owner' gets a confirm first
    (no backend "last owner" floor exists, so it's possible to leave a group ownerless - a SuperAdmin can
    always fix that, but worth a pause before doing it to yourself).
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

- `[security] M` Retire or lock down the legacy open `/finance/{proxy+}` route once nothing
  external depends on it. (KNOWN_ISSUES #1)
- `[debt] M` De-duplicate copy-pasted logic into a **shared Lambda layer**: `sanitize_nickname`,
  `recompute_all_ratings`, `compute_momentum_stats`, `compute_comeback_bonus`, `compute_adaptive_k`,
  `get_pairing_count`, `_is_valid_completed_game`, `_caller_claims`, `_is_super_admin`, `_response`.
  Prioritize the nickname sanitizer — it also has a 4th copy in `app.js`. (KNOWN_ISSUES #6)
- `[security] S` Remove committed AWS account id from `current-policy.json` /
  `networth-deploy-policy.json`; parameterize. (KNOWN_ISSUES #3)

- `[feat] L` **Privacy / "cloak" mode (reciprocity visibility filter) — admin-gated, ships dark.**
  A player can go *private*: they drop out of everyone's comparative views (rankings, Hall of Fame, H2H
  distribution, lookup/opponent dropdowns) AND lose the Stats tab themselves (reciprocity) — but keep
  their own Player Card, and stay fully selectable when recording matches. Public players see everyone
  except privates (ranks compress 1..N). **Also global:** strip the Elo from every dropdown *label*
  (the pairing-bias signal the club reacted to). Enforced server-side (a raw `/matches` call must not
  see through it). 7-day cooldown between switches (admin-configurable); SuperAdmin sees all + can
  force-flip anyone ignoring cooldown. Decisions locked 2026-08 (cloak not zen; rank compression yes;
  existing HoF records kept, future ones exclude privates; private names stay in factual match history).
  **No template/route changes needed** — reuses `/update-my-card`, `PUT /players/{id}`, `/app-settings`,
  and `/profile-secure` (all already Cognito-authed). Staged: **P1a** foundation (done) → **P1b** the
  comparative filter (fold B2's `.scan()` pagination in here) → **P2** frontend (hide Stats, self-lock
  the card lookup, toggle UI + cooldown messaging, admin controls, Elo-label strip). Build **before**
  Seasons so the season leaderboard inherits the filter.
- `[perf] M` **Build B2 — backend fan-out reduction (follow-on to Build B).** Build B killed the
  first-paint burst frontend-side (lazy-load + freshness), so throttling should be gone. Remaining
  server-side polish: (a) a single **bundle endpoint** that scans matches once and returns the Stats +
  Profile views together (extend `profile_bundle_for`) so opening those tabs is 1 call, not 6–7;
  (b) cache CORS **preflights** via `Access-Control-Max-Age` to drop the OPTIONS round-trip per call;
  (c) **paginate `table.scan()`** (KNOWN_ISSUES #15) while in these handlers. Backend deploy; do after
  confirming Build B cleared the 500s.
- `[ops] S` **Request a Lambda concurrency-limit increase.** Account is at the new-account default of 10
  concurrent executions (normal 1000); Service Quotas → Lambda → "Concurrent executions". Instant
  headroom for the throttling above while Build B lands. (KNOWN_ISSUES #16.)
- `[bug] M` **Paginate `table.scan()`.** `list_players` (and any sibling bare `.scan(`) reads only the
  first 1 MB page; players past it silently vanish from `/players` → `allPlayers` → every dropdown and
  the per-player linked-status check. Loop on `LastEvaluatedKey`. (KNOWN_ISSUES #15.)

## Next / medium
- `[bug] M` **iOS card-share: animated video won't save.** ✅ DONE 2026-08-09 (see Done). — On the animated share result panel, "Save"
  opens the clip in a viewer instead of downloading on iOS. iOS Safari ignores `<a download>` for video
  blobs — it navigates/opens the media rather than saving. The reliable iOS path to Photos is the native
  share sheet (`navigator.share({ files: [videoFile] })` → "Save Video"), not a download link. Fix:
  detect iOS (or feature-detect `navigator.canShare({files})`) and route Save through the share sheet;
  keep the `<a download>` path for desktop/Android. Card-share.js only — own focused patch. (2026-08-07.)

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
      **DONE in v1.11.0** — animated share via `MediaRecorder` + `canvas.captureStream`: when the
      selected frame or background animates, the export records a ~2.6s loop to **WebM** (MP4 where the
      browser's MediaRecorder supports h264) and shares/downloads it; static picks still export PNG.
      `drawComposite(ctx,cur,W,H,pad,t)` unifies the still + animated draw. Also v1.11.0: player store
      is grouped into labelled category sections; Holo/Ice/Plasma glass frames given distinct palettes;
      Flame frame redrawn as actual flame tongues (SVG in preview, canvas paths in export) instead of a
      glow. Web Share of video may be rejected by IG/WhatsApp (they want MP4) → falls back to download.
    - **v1.11.1** — animated share was silently failing (the ~2.6s recording expired the user-gesture
      before navigator.share/download); now records first, then shows a result panel with the clip +
      Share/Save (fresh gesture) + long-press-to-save. Backgrounds lightened (scrim reduced) + text
      shadow for legibility. Holo/Ice/Plasma palettes made distinct. Flame frame redrawn as a full
      four-edge fire border (canvas + preview SVG) matching the reference. Prefers MP4 where supported.
    - **v1.10.1** — SuperAdmin gets every cosmetic unlocked (customizer hides locks; `update_my_card`
      honours the same bypass via `_is_super_admin`). Added a **Flame** frame preset.
    - `[idea]` FC/FUT-style **card silhouette** (angled corners) — needs clip-path in the preview and a
      matching canvas path (bg clip + frame stroke) in the export; decide global shape vs per-frame.
- `[feat] L` **Seasons (monthly) — leaderboards, badges, history selector ("Build C").** Apex/PUBG-style:
  monthly windows, per-season leaderboards, participation/achievement badges mapped to each season, and a
  season selector to browse past seasons. **Placement (decided 2026-08-07):** render season
  rating/badges *inside the Player Card* (not a separate Profile section) — next to the existing
  rating-history chart. **Design fork (decide first):** derived (season = a date window;
  leaderboard replayed/delta'd within it; lifetime Elo untouched; freeze a snapshot at rollover,
  piggybacking `progress_scheduler`) vs a destructive rank reset. Strong lean: derived + an optional
  soft-reset "season rank" shown next to lifetime Elo, because Elo is path-dependent and the architecture
  is replay-based. Owner to pick reset behaviour (hard / soft / lifetime+season-rank). Best built after
  isolated staging so the migration is testable. Also lets the share-card say "season" not all-time.
- `[ops] L` **Isolated staging (data clone) — "Build D".** `deploy-staging.yml` is frontend-only and
  points at PROD's backend/DB/Cognito today, so "testing in staging" mutates prod. A true staging needs a
  parallel stack (separate DynamoDB tables, API Gateway, uploads bucket) + a clone script (scan prod →
  write staging). Wrinkles: Cognito password hashes can't be cloned (share prod pool for read-only tests
  or seed test users); make the uploads/CDN base a per-env injected config global so cloned image keys
  resolve against staging's CDN. Stand up before Build C. (Owner idea 2026-08-07.)
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

- ✅ 2026-08-20 — **Match edit/delete: real Cognito auth (was code-only) + relief double-counting
  fix in My Dues + paid amount shown + match-count-per-group metric (all Owner-raised).**
  (1) **`PUT`/`DELETE /matches/{match_id}` moved off the shared confirmation code onto real auth**
  (Owner asked why matches still needed the code "like profile/group deletion" - turned out the code
  wasn't just friction, it was the ONLY gate: both routes were `AuthorizationType: NONE`, no Cognito
  identity checked at all - see KNOWN_ISSUES #17 for the full writeup). Routes are now
  `COGNITO_USER_POOLS`; `matches/index.py` gained `_caller_may_edit_match` (SuperAdmin, or owner/admin
  of the match's own group - mirrors `OWNER_DECIDABLE_TYPES`' existing bar for the request/approve
  path). Frontend: the confirmation-code prompts/fields are gone from `deleteMatch`, `editMatchScore`,
  and the full `editMatch` modal - a plain `nwConfirm` is the only "are you sure" now, and all three
  call `authedFetch` instead of plain `fetch` (route requires a token now). `decide_claim_request`
  (players lambda) no longer forges a `confirm` field in its internal invoke - it never needed to
  anymore. Verified with an auth-matrix script (own-group owner edits, plain member blocked,
  other-group owner blocked, ungrouped match SuperAdmin-only, SuperAdmin edits/deletes anything) plus
  a regression check that owner-approval of a match request still works end-to-end.
  Player/group deletion and other genuinely destructive whole-account operations still require the
  code, unchanged - this only touched the two match routes.
  (2) **"My dues" double-counted relief as still-owed.** `my_settlement` showed a residual
  (walk-in-share refund) as "the club owes you ₹X" for every month it was generated, even after that
  exact amount had already been auto-applied as relief against a later month's bill (reducing what was
  owed there - see the 2026-08-20 dues-showing-pending fix above it in this log). The money had already
  changed hands as a smaller bill; showing it again as a standing credit double-counted it. Added
  `_next_period` and a consumption check: if the following month has a non-forfeited "Yes" membership
  in the same slot, the residual is treated as spent (capped at what that month actually needed, since
  relief today only ever looks one month ahead - excess isn't carried further). Verified full and
  partial consumption with fixtures.
  (3) **"Paid" showed no amount.** `you_paid_amount` (the actual confirmed/effective amount) is now
  returned alongside `you_paid`; the "My dues" table shows "paid ₹X" instead of a bare "paid".
  (4) **Matches-logged-per-group metric.** New `GET /matches?counts_by_group=true` (matches lambda) -
  one tally over the same full scan every other `/matches` query shape already does, no new scan cost.
  Wired into the group detail panel (`loadGroupMembers`, shared by the SuperAdmin's "browse any group"
  view and an owner/co-owner's own group management - covers both asks in one place) as "Matches
  logged in this group: N", visible whenever that panel is open.
  Files: `infrastructure/template.yaml` (auth type), `backend/lambdas/matches/index.py` (1, 4),
  `backend/lambdas/players/index.py` (1, internal-invoke cleanup), `backend/lambdas/finance/index.py`
  (2, 3), `frontend/js/app.js` (1, 3, 4).
- ✅ 2026-08-20 — **Stats/history group filters no longer leak other groups' data to members, +
  "My dues" no longer shows already-confirmed payments as pending (both Owner-reported).**
  (1) **Group-scoped Stats/History filters.** `loadGroups()` populated eight different filter
  dropdowns (`rankings_scope_select`, `hof_group_filter`, `diversity_group_filter`,
  `badges_group_filter`, `attendance_group_filter`, `history_scope_select`, `log_group_filter`,
  `profile_partnerships_scope_group`) from the full, unfiltered `allGroups` list for every viewer, so
  any member could browse another group's rankings/Hall of Fame/diversity/badges/attendance/game
  log/partnerships just by picking it from the dropdown. Added `visibleGroupsForFilter()` (SuperAdmin
  gets `allGroups`, everyone else gets `myGroups()`) and wired all eight selects through it. The
  select's own built-in "All groups"/"Global"/"All players" option is untouched, so club-wide
  aggregate views still work for everyone — only the *per-group* picks are narrowed. UI-level only,
  matching the existing `populateFinanceGroups` pattern and consistent with KNOWN_ISSUES #2 (`GET
  /groups/{id}` itself stays an open, unauthenticated read — group rosters are treated as
  club-public data, not new backend enforcement here).
  (2) **"My dues" showing confirmed payments as pending.** `my_settlement` (finance lambda) compared
  a member's stored `payment_confirmed_amount` against the raw `cost_per_head`, but
  `update_record`'s `confirm_payment` branch has stored the **relief-adjusted effective amount**
  (`cost_per_head` minus the prior month's residual/relief) since 2026-08-01 — the two sides of the
  same check were using different numbers. Any month where the member had nonzero relief carried
  over (the normal case, since residual/relief is core to this club's model) would show as unpaid
  immediately after confirming, because the confirmed (effective) amount could never equal the raw
  per-head figure. Fixed `my_settlement` to compute the same relief via `_member_relief` and compare
  against the same effective figure `_settlement_rows`' own "settled" check already uses — the two
  now agree. `you_owe` also now reflects the effective (post-relief) amount, not the pre-relief one.
  Verified with a two-month fixture (July residual creates August relief; confirming August's payment
  now correctly shows `you_paid: true`, `you_owe: 0` instead of a false-positive pending amount).
  Frontend: `frontend/js/app.js` (1). Backend: `backend/lambdas/finance/index.py` (2).
- ✅ 2026-08-20 — **Group owners get match delete/edit requests + an actually-working co-owner
  control (both Owner-reported: "only shows up under my admin account", "owner should be able to add
  co-owners").**
  (1) **Match delete/edit requests now reach group owners, not just SuperAdmin.** `_create_match_request`
  already stamped a `group_id` on every request specifically so this could be wired up later without a
  schema change - that follow-up was never done. Added `match_edit`/`match_delete` to
  `OWNER_DECIDABLE_TYPES` (players lambda); `_owner_may_decide`'s existing group-precise check
  (`gid in owned_group_ids`) now applies to them exactly like it already does for `finance_access` - an
  owner sees and can approve/reject requests for THEIR group's matches, never another group's, and a
  request with no group (a genuinely one-off match) still falls through to SuperAdmin-only, unchanged.
  Verified with a request-matrix script (own-group match request decidable, other-group rejected,
  ungrouped rejected, `finance_access`/`delete_player` behavior unchanged). **Caveat noted, not a new
  risk:** approving a match change still triggers a club-wide rating recompute (Elo is one shared pool,
  not per-group) - true for a SuperAdmin approval today too, not something this change introduces.
  `delete_player` deliberately stays SuperAdmin-only (whole-account removal, not group-scoped, no
  `group_id` to gate against). Frontend already rendered these request types in the shared requests list
  (no change needed there) - just updated the "sent to the admin" toast since it's no longer always the
  admin.
  (2) **Co-owner control actually built.** The group detail page's per-member role badge was read-only,
  despite BACKLOG and the page's own copy both claiming a working "role control." Backend
  (`PUT /group-role/{group_id}/{player_id}`) already allowed any owner/admin of a group to promote a
  member to owner/admin - only the frontend call was missing. Added `setGroupMemberRole` and swapped the
  read-only badge for an editable member/admin/owner `<select>` when the viewer `canManageGroup`, mirroring
  the existing finance-role select right next to it. Self-demotion away from 'owner' asks for confirmation
  first (no "last owner" backend floor exists to prevent an accidental lockout).
  Both: `backend/lambdas/players/index.py` (1) + `frontend/js/app.js` (both).
- ✅ 2026-08-19 — **Stats tab defaults to your own group + two staleness fixes (Owner-reported).**
  Three related fixes, same session:
  (1) **Stats scoped to your group by default.** All five Stats-tab scope filters (rankings, Hall of
  Fame, diversity, progress badges, attendance) now default to the viewer's own group instead of
  club-wide "All" - still one click away from global via the same dropdown. Backend: `stats_bundle`
  (the single-scan endpoint the Stats tab opens with) now accepts an optional `group_id` and threads it
  into `compute_hall_of_fame`/`compute_diversity`/`compute_progress_badges`/`compute_attendance` exactly
  like the individual per-section loaders already did - still one request, no extra Lambda invocations
  (verified end-to-end: a synthetic two-group fixture produces different, correctly-scoped bundle output
  per group and an unscoped bundle covering both). Frontend: `loadGroups()` now defaults every scope
  select to `myGroups()[0]` via a DOM-only `.value` set (fires no 'change' event, so this costs zero
  extra fetches at boot) instead of just `rankings_scope_select`; `loadStatsBundle()` passes that scope
  through as `group_id`.
  (2) **Session-restore race that could leave the group defaults never applied.** `restoreSession()`'s
  `getSession()` callback can resolve after the boot sequence's own `loadGroups()` call already ran with
  no identity yet (silent session restore is async; `loadGroups()` doesn't wait for it) - previously the
  only way to pick the default group back up was a second page reload. `restoreSession()` now re-runs
  `loadGroups()` in that callback too (alongside the existing `loadPlayers()`); every default there only
  fills an empty select, so calling it twice is a no-op past whatever's already been chosen. This also
  makes the *existing* match-recording group default (`defaultMatchGroup()`, previously the only thing
  gated this way) reliable for the same reason - Owner flagged "keep the player's group as default when
  filling up their matches", and it turned out to already exist but not always fire.
  (3) **Newly-registered player not appearing in the team dropdowns without a page reload
  (Owner-reported, "crucial").** The "+ Register a new player for this match" quick-add on the
  record-match form already refreshed `allPlayers` via `loadPlayers()`, but the team-select dropdowns
  actually populate from `currentMatchGroupMembers` - a client-side cache of the selected group's roster
  added so randomizing teams doesn't re-fetch the group on every click (see the swap-to-repair entry
  below). `loadPlayers()` doesn't touch that cache, so a player just added to the currently-selected
  group silently didn't show up until something else refreshed it (switching groups, or a full reload) -
  and became more likely to bite now that a group is selected by default per (1)/(2) above. The handler
  now also calls `updateMatchGroupCache()` + `refreshTeamSelectOptions()` after a successful quick-add.
  All three: frontend-only (`app.js`) except the `stats_bundle` `group_id` param (`matches/index.py`,
  additive/backward-compatible - omitting it reproduces the old unscoped behavior exactly).
- ✅ 2026-08-19 — **Match-recording team pickers: swap-to-repair instead of hide-to-repair.** The 4
  player dropdowns on the record-match form (`team_a1_select`/`_a2`/`_b1`/`_b2`) used to exclude anyone
  already picked in one of the other 3 slots, so moving someone from one slot to another meant clearing
  their current slot first, then re-finding them in a dropdown whose option list had just changed shape
  - annoying when re-pairing an existing doubles group (Owner-reported friction). Now every dropdown
  always lists the full pool, and picking someone already assigned elsewhere **swaps** the two slots
  (`handleTeamSelectChange`) instead of hiding/duplicating - move Bob from A2 into B1 and whoever was in
  B1 lands in A2, one click. `refreshTeamSelectOptions` only rebuilds a select's `<option>` list when the
  underlying pool changes (group switch, initial load) rather than on every pick, so a dropdown's options
  never get torn down while you're mid-search in it. `teamSelectValues` mirrors each slot outside the DOM
  so the swap can see the pre-change value. Submit-time and backend duplicate checks are unchanged as a
  backstop. Verified with a standalone swap-logic simulation (repair, clear, move-into-empty-slot, no
  duplicates ever produced). Frontend-only (`app.js`), no backend/template change.
- ✅ 2026-08-11 — **Stats-tab fan-out consolidation (mitigates concurrency throttling, KI #16).** Opening
  Stats used to fire the 4 matches-derived sections (Hall of Fame, diversity, progress-badges, attendance)
  as **4 concurrent `/matches` calls, each doing its own full-table `_scan_all`** - a big chunk of the
  Lambda-concurrency pressure (account cap = 10) and 4x the DynamoDB RCU. New `stats_bundle=true` endpoint
  computes all four from a **single scan** and returns them together; `loadStatsBundle` makes one call and
  distributes to the existing `renderX` functions, falling back to the individual loaders if it fails.
  Group-filter changes still use the per-section loaders. Net: Stats matches-scans 4->1, fewer concurrent
  invocations. **Still the #1 reliability action (owner):** request a Service Quotas increase for Lambda
  'Concurrent executions' (10 -> 1000) - that's the real fix for burst 500s during busy sessions.
- ✅ 2026-08-11 — **3 more share-card templates + season tasks split out.** card-share now has **11**
  stat layouts: added **Everything** (dense 6-stat grid + trend: W-L, win rate, games, peak, streak, best),
  **Head-to-head** (record vs your most-played opponent, from the bundle's top_opponents), and **Best
  partner** (record with your most-played partner - one extra `partnerships_for` fetch). Both new preview
  + export renders each; opponent/partner names come from the computes (`opponent_name`/`partner_name`).
  Quests list now **groups by scope**: a 'This week' section and a separate season section (headed by the
  season name) - season tasks are no longer listed under weekly. **Next:** badge SVG asset wiring; more
  templates if wanted; optional rollover scheduler.
- ✅ 2026-08-11 — **Seasons C4 (season-scoped tasks).** Quests can now reset **per season** instead of
  weekly. Backend: a shared `_quest_period(quest)` returns (window bounds, claim-key prefix, label) by
  scope - season quests use the current season's window + `season:<id>` claim namespace (so they reset at
  rollover), weekly quests use the Monday week as before. Wired into `list_quests`, `claim_quest`, and
  `save_quest` (which now stores `scope`, default 'weekly' = backward-compatible). A season-scoped quest
  with no active season is hidden. Weekly vs season claim keys never collide, so the two reset
  independently (verified). Frontend: a **Resets: Weekly / Per season** selector in the admin quest form,
  and each quest now shows its period (· This week / · Season 0) in the Quests list. **Season epic C1-C4
  complete.** Optional later: deterministic rollover scheduler seal; badge SVG assets; more card templates.
- ✅ 2026-08-11 — **4 new share-card stat templates + bigger season badges.** card-share now has **8**
  stat layouts (was 4): added **Peak rating** (highest Elo reached, gold), **Streaks** (current + best
  streak), **Win record** (big W-L + win-rate + games), **Last 10** (recent W/L result pills) - each with
  both the live HTML preview and the canvas export render, all free. Enriched the card `stats` object with
  `peak` (peak_rating), `bestStreak` (personal_best_streak), and `form10` (last-10 results) from the
  profile bundle. Season badges on the Player Card enlarged 34->60px (Apple-Watch-ish presence; the small
  ones looked diminishing). **Note:** new templates are best-effort blind renders - eyeball and I'll nudge
  any spacing. **Next:** more templates if wanted, badge SVG asset wiring, C4 season tasks.
- ✅ 2026-08-11 — **Season fixes + card-share width.** (1) **Most-improved** season badge now only awards
  to a non-champion - in a uniform-baseline season (Season 0 starts at the beginning, everyone at 1000) the
  winner is always the biggest climber, so the two collided; now it only fires for a genuine over-performer
  who didn't win (meaningful from Season 1 on). (2) **Season achievements count only SEALED (completed)
  seasons** - so they're permanent milestones; the current Season 0 contributes once it ends Aug 31.
  (3) **Season section moved below** the player dropdown (was above it). (4) **Season race fixed:**
  `seasonsEnabled` now loads with the initial app-settings fetch (not only when Stats opens), and
  `loadPlayerSeasons` relies on the response's `enabled` instead of the global - so the card's Seasons
  section shows first paint, no reload needed. (5) **Card-share width: reverted the naive fix** - the
  exported card's footer is bottom-pinned (H-pad) while stats/trend are top-anchored, so making the canvas
  taller only widens the content->footer gap. Kept 1080x1350; the real fix is re-flowing the stats layout
  to fill the card (iterative, needs screenshots).
- ✅ 2026-08-10 — **Season-scoped achievements (folded into the achievements grid).** `compute_achievements`
  now aggregates the player's season history (via `compute_player_season_summary`) into cumulative counts:
  season wins (#1 finishes), podiums (top-3), most-improved seasons, iron-player seasons, seasons played.
  Frontend adds five tiered tiles - Season Champion / Season Podiums / Season Riser / Season Grinder /
  Seasoned - shown only when Seasons are enabled, sitting alongside the existing achievements in the
  Achievements popup. (Note: adds a season pass to the profile bundle; cheap since ended seasons read the
  sealed row and only the live season recomputes - fold the summary into the bundle later if it needs
  trimming.) **Next:** C4 season-scoped tasks; the card-width choice (A taller export vs B 4:5 preview) when
  the owner decides.
- ✅ 2026-08-10 — **Player Card header reflow + card-share MP4.** (1) The five squished controls on the
  Player Card header reflowed to 3 rows: **dropdown + refresh** (row 1, so the player select gets full
  width), **Achievements** full-width (row 2), **Settings + Share card** side-by-side (row 3). (2)
  card-share now offers **more MP4/H.264 mime candidates** (`avc1.640028`, `avc1.42E01E`, `avc1`, `h264`,
  plain `video/mp4`) before WebM, so iOS records a shareable **MP4** (WebM won't post to WhatsApp/Instagram).
  **Open (needs owner call):** exported card is 1080x1350 (4:5, IG-feed standard) while the in-app preview
  is ~296x406 (0.73) - the export looks wider. Either make the export match the preview's taller ratio, or
  the preview match the 4:5 export (WYSIWYG). Deferred pending owner's choice (visual, can't verify blind).
  **Next:** season-scoped achievements in the achievements grid; then C4 season tasks.
- ✅ 2026-08-10 — **Seasons C3b (season badges + standing on the Player Card).** **Backend:**
  `player_season_summary=<player_id>` returns a player's per-season standing (rank, season score, climb,
  games) + earned badges across all started seasons, using the shared `_season_board_leaders` (sealed if
  ended, live otherwise). Badges per season: **podium** (1/2/3), **most_improved** (biggest positive
  delta), **iron** (most games), **participation** (qualified). Privacy-guarded (returns empty for a
  private target to a non-admin). **Frontend:** a 'Seasons' card on the Player Card lists each season with
  standing + badges rendered as **format-doc SVG medallions** (`seasonBadgeSvg`: hexagon + one gradient +
  one glyph - gold/silver/bronze podium, emerald up-arrow = most improved, slate dumbbell = iron, blue
  check = participation), loaded on profile view. These are drop-in until you sketch custom glyphs per
  `docs/BADGE_FORMAT.md`. **Next:** more season-scoped *achievements* (fold into the achievements grid),
  then C4 season-scoped tasks; optional deterministic rollover scheduler seal.
- ✅ 2026-08-10 — **Seasons C3a (season sealing) + Achievements popup + badge format.** (1) **Lazy
  season sealing** (matches lambda): when an ended season's board is first requested it's frozen once into
  `sealed_leaders` on the `__season__<id>` row, so past seasons become immutable - later match edits
  recompute lifetime but no longer reshuffle a finished season (the current season still recalculates live
  from its frozen baseline). (2) **Achievements popup:** the Player Card 'Achievements' section moved out of
  the long scroll into a Settings-style modal opened by a header button (`openAchievementsModal`); inner IDs
  preserved so the profile bundle still populates it. (3) **Badge format doc** (`docs/BADGE_FORMAT.md`): the
  Apple-Fitness recipe (one shape, one vertical gradient, one centered glyph, thin inner highlight), tier
  palette, and a drop-in SVG template so new badges can be sketched consistently; `seasonMedallion` is the
  working example. **Next: C3b** — award/display per-season badges on the Player Card (podium, most-improved,
  iron-player, participation) from the sealed board, a season stat line on the card, and more season-scoped
  achievements; then C4 season tasks. The deterministic rollover *scheduler* seal (vs lazy) is optional
  polish since lazy sealing already freezes correctly on first post-end view.
- ✅ 2026-08-10 — **Seasons C2 polish + admin-can-record (frontend).** (1) **Future seasons** no longer
  appear in the member Season selector - only seasons whose `start_date <= today` show; a future season
  (e.g. Season 1 starting Sep 1) stays in the admin list until it starts. (2) **Collapsible Stats sections**
  (`makeStatsCollapsible`): every Stats card is now tap-to-expand (all start collapsed), so you don't
  scroll through each - inner element IDs preserved, idempotent. (3) **Player-less admin can record
  matches / run tournaments** as a non-participant recorder (`canRecord = showForms || logged-in admin`),
  and the 'account not linked' notice is hidden for admins - so the admin needs **no** player profile
  (nothing to hide from rankings). If a profile is ever wanted for card testing, the privacy `private`
  flag already hides it from comparatives. **Next: C3** (rollover seal + season badges + card season line).
- ✅ 2026-08-10 — **Seasons C2 (frontend, dark until enabled).** index.html + app.js. **Season board** on
  the Stats tab: a season selector + climb leaderboard (rank, player, season score, climb delta, games),
  reading `/matches?season_leaderboard` via `statsFetch` (privacy-scrubbed, admin see-all). Top-3 get a
  clean **Apple-Fitness-style hexagon medallion** (`seasonMedallion` - flat hexagon, single gradient,
  rank glyph); full season achievement badges come with C3 awarding. **Admin season management** in the
  settings panel: enable toggle, soft-reset `k` input, season list + add/remove (name + start + optional
  end) - writes the `seasons` list via `/app-settings`, so no more hand-POSTing JSON. Card hidden unless
  seasons enabled + at least one season defined. **Season 0 setup:** create "Season 0" start 2026-01-01
  (or first-match date) end 2026-09-01, then "Season 1" start 2026-09-01 - via the new admin UI. **Next:**
  C3 rollover scheduler (seal a finished season's final board + award season badges) and season stats on
  the Player Card; C4 season-scoped tasks.
- ✅ 2026-08-10 — **Seasons C1 (backend foundation, dark).** Design locked: **derived seasons, soft
  reset, lifetime rating never resets.** Each season is an admin-defined window with an explicit
  `start_date`; the board starts everyone at `1000 + (lifetime_at_start - 1000) * k` (k = soft-reset
  retention, admin-adjustable, **default 0.3**) and then moves them by their lifetime rating change
  across the window - so grinders re-climb to the top while a struggling player gets a fresh (non-500)
  start each season. Chose distance-based soft reset over rank-based seeding (rank-based rewards finishing
  low - a bias the owner flagged). **Storage:** definitions + `seasons_enabled` + `season_reset_k` live in
  the app-settings row (managed via `/app-settings`); per-season **frozen baseline** (`start_lifetime` +
  `baseline`) lives in a `__season__<id>` **sentinel row** in the matches table (mirrors `__quests__`) -
  **no new table / template change.** Baseline is frozen once (from stored `ratings_after` as of the
  start), so editing OLD matches never moves where a season started you, but in-season movement still
  recalculates from that baseline off the recomputed `ratings_after` (exactly the owner's recompute point).
  No Elo re-replay - reads stored `ratings_after`. **Reads (no new route):** `GET /matches?seasons=list`
  and `?season_leaderboard=<id|current>`; season boards are privacy-scrubbed and SuperAdmin-see-all like
  the other comparatives. Ships dark (`seasons_enabled` default off). **Next: C2** frontend (season board
  on Stats + selector, season stats on the Player Card, admin season management UI); **C3** the rollover
  scheduler that seals a finished season's final board + awards season badges; **C4** season-scoped tasks.
  Testing: `POST /app-settings {key:'seasons', value:[{name,start_date}]}`, enable, then
  `GET /matches?season_leaderboard=current`.
- ✅ 2026-08-09 — **Privacy button race fix + `.scan()` pagination (KNOWN_ISSUES #15 closed).**
  (a) The Player Card Public/Private control was rendered by `updateAuthUI` *before* `loadVisiblePlayers`
  had defaulted the profile select, so `viewingOwn` was false and it hid until a manual refresh. Now
  `renderPrivacyControl()` also fires at the end of `loadProfile` and `loadVisiblePlayers`, once the
  selected player is settled - so it reflects correctly on first paint. (b) Finished #15: added a
  paginated `_scan_all()` to the players lambda and swapped all **9** players-table `.scan()` sites
  (incl. `list_players`) to it via a word-boundary regex (left the tiny `claim_requests`/`groups`
  scans alone); in the matches lambda paginated the remaining **matches (3) + history (1) + tournaments
  (2)** scans. Growing tables (players, matches, history, tournaments) no longer truncate at 1 MB.
- ✅ 2026-08-09 — **Privacy/UX polish + iOS card-share save fix.** frontend only. (1) A **player-less
  SuperAdmin can now view the Player Card tab** as a pure lookup surface (`canViewProfiles = showForms ||
  logged-in admin) - they can pull up anyone's card; the Settings button is gated on having your *own*
  player, so no dead button. (2) The **"Set up your player profile" modal is dismissable** again ("Maybe
  later" + `closeCompleteProfileModal`): the persistent Players-tab notice is the retry path, so skipping
  no longer strands a returning player - the original reason it was forced. (3) **iOS card-share Save**
  now routes through the share sheet on iOS (`navigator.share({files})`) instead of `<a download>`, which
  iOS ignores for video blobs (it just opened the clip in a viewer) - so "Save" reaches Photos/Files.
  Desktop/Android keep the direct download.
- ✅ 2026-08-09 — **Privacy P2c (admin polish) - privacy epic now functionally complete.** app.js +
  index.html. (1) **Admin see-all:** comparative reads now route through `statsFetch()` - SuperAdmins hit
  the authed `/profile-secure/matches?...` (unscrubbed), everyone else the public `/matches` (scrubbed), so
  admins see private players in Hall of Fame / diversity / badges / history / attendance, not just rankings.
  (2) **Admin force-flip:** a "Force a player's visibility" control in the admin settings panel
  (`adminSetPrivacy` -> `PUT /players/{id} {privacy_private}`, no confirm code, ignores cooldown; the select
  flags who's already PRIVATE). (3) **Pure-admin UX:** the "Set up your player profile" modal no longer
  fires for SuperAdmins (they're intentionally player-less). Ships with privacy mode still default-off.
  **Privacy epic status:** P1a/P1b (backend) + P2a/P2b (frontend) + P2c (admin) done. Remaining bare
  `.scan()` sites (players `list_players`, matches attendance-window/week/groups/history/tournaments) are
  the only tracked follow-up (KNOWN_ISSUES #15). **Ready to test end-to-end** once deployed + flag enabled.
- ✅ 2026-08-08 — **Privacy P2b (Elo-label strip) — the pairing-bias fix. NOT dark (visible on deploy).**
  app.js only. Removed the rating from the *picker* labels — the surfaces where you choose who's in a
  match/team, which is where the "no one wants to pair with the low number" bias lived: the match player
  selects, add-player checkboxes, team-pairing participant picker, game-log edit-players dropdown,
  tournament-participant checkboxes, and the profile lookup/H2H/partner/compare selects (10 sites total).
  **Kept** (rating still shown): the rankings table and Player Card (the point of them), roster/member
  rows, and the Elo-*balanced* pairing preview (rating there is the tool's balancing rationale, not a
  manual-pairing lever). Unlike the rest of the privacy epic this ships live, not behind the flag — it's
  an unconditional UX change. **Open question for owner:** want the rating pulled from the pairing preview
  + member rows too (lines 572/1577/1580/6895), or leave them? **Remaining privacy work (P2c):** admin
  force-flip UI (control on admin player-edit → `PUT /players/{id} {privacy_private}`) and routing the
  admin's Stats reads through `/profile-secure` so admins see privates in HoF/diversity.
- ✅ 2026-08-08 — **Privacy P2a (frontend enforcement + controls, still dark).** index.html + app.js.
  Reads `privacy_mode_enabled` into a `privacyModeEnabled` global (init + loadAppSettings). **Player**
  self-toggle: a Visibility card on their own Player Card (`renderPrivacyControl`/`toggleMyPrivacy`) that
  PUTs `/update-my-card {privacy_private}`, surfaces the 429 cooldown message, and reloads. **Reciprocity:**
  a cloaked non-admin loses the Stats tab (hidden + bounced to Players if active) and their lookup/H2H/
  partner selects lock to themselves (can't scout). **Public viewers:** private players are filtered out of
  rankings (client-side, via `privateHiddenIds()` cross-referenced by id so group rankings work too) and
  out of the lookup/opponent/partner dropdowns. **Admin:** a Private-mode enable toggle + cooldown-days
  input in the app-settings panel (`setPrivacyMode`/`setPrivacyCooldown`); SuperAdmin bypasses all hiding.
  Everything is gated on `privacyModeEnabled`, so P2a also ships **dark** until you flip the admin toggle.
  **Next: P2b** — the global Elo-label strip across the ~14 pairing/match dropdowns (the bias signal),
  the admin force-flip UI (a control on the admin player-edit that PUTs `/players/{id} {privacy_private}`),
  and routing SuperAdmin's Stats reads through `/profile-secure` so admins see privates in HoF/diversity.
- ✅ 2026-08-08 — **Privacy P1b (backend enforcement, still dark).** matches lambda only. Central
  scrubber (`_scrub_private` + `_load_private_ids`) omits private players from the *live* comparative
  computes — Hall of Fame, diversity, progress-badges, attendance, top-opponents (H2H distribution),
  partner-distribution, partnerships, and the profile bundle's leaderboard parts — keyed on
  player_id/opponent_id/partner_id/top_partner_id. **Grandfathered:** frozen `progress_history` is left
  as-is (per decision); the card owner's own `recent_form`/`overall_record` are not scrubbed (factual
  history). **SuperAdmin see-all:** `profile_view_enforced` short-circuits admins straight to
  `list_matches`, and `list_matches` only builds the private set when the caller is *not* a verified
  SuperAdmin — so a raw public `/matches` call (no claims) is always filtered, while an admin via
  `/profile-secure` sees everything. Enforced server-side (not spoofable via query param). No-op while
  the flag is off (scrubber returns the same object on an empty set). **B2 folded in (partial):**
  `_scan_all()` paginates the `list_matches` feed *and* `recompute_all_ratings`' match+player scans
  (KNOWN_ISSUES #15 — recompute truncating would silently corrupt ratings past 1 MB). Remaining bare
  `.scan()` sites in this lambda (attendance-window, week, groups, history, tournaments) + players
  `list_players` still to paginate — lower risk, follow-up. **Next: P2 (frontend)** — hide Stats for
  private users, self-lock the card lookup, toggle UI + cooldown messaging, admin controls, and the
  global Elo-label strip; route SuperAdmin stats reads through `/profile-secure`.
- ✅ 2026-08-08 — **Privacy P1a (backend foundation, dark).** players lambda only, no template changes.
  `app-settings` gains `privacy_mode_enabled` (default **off**) + `privacy_cooldown_days` (default 7,
  0–30) via `get_app_settings`/`set_app_setting`. Self toggle rides `/update-my-card` (`privacy_private`
  in body): gated on the feature flag, enforces the cooldown (per-direction, SuperAdmin-exempt), stamps
  `privacy_changed_at`, returns 429 with days-left when cooling. Admin force-flip rides `PUT /players/{id}`
  (SuperAdmin sets `privacy_private` standalone — no confirmation code, no cooldown). `list_players` now
  serializes `privacy_private`. New player fields `privacy_private`/`privacy_changed_at` are schemaless
  (absent = public), so no migration. Ships dark — flag off = identical behavior. **Next:** P1b applies the
  actual comparative filter (rankings/HoF/diversity/badges/history/H2H) + SuperAdmin see-all via
  `/profile-secure`; P2 is the frontend.
- ✅ 2026-08-07 — **Build B (frontend-only): lazy per-tab loading + freshness — the throttling fix.**
  Root cause was the boot fan-out (~13 eager API calls in the init IIFE: rankings, diversity, badges,
  history, HoF, attendance, public-walkins, tournaments×2, and a 5-call `loadProfile`) exceeding the
  account Lambda concurrency limit of 10 (KNOWN_ISSUES #16). Fix: removed the eager fan-out; each tab now
  pulls its own data on first open via a small lazy layer (`ensureFresh`/`ensureOnce`/`ensureProfileFresh`
  + `loadActiveTabData`). Freshness: `matchesRev` bumps on every match add/edit/delete/reorder/recompute,
  so a matches-derived tab (Stats, Profile) auto-refetches on next open only when matches actually changed
  — otherwise switching tabs is free (this is the "add a match, switch to Stats, had to hit reload" pain).
  `loadVisiblePlayers` no longer auto-fires `loadProfile` at boot (gated on the Profile tab being active).
  First paint drops from ~13 concurrent invocations to ~6, comfortably under 10. Static deploy, no lambda/
  DDB change. Remaining server-side polish (bundle endpoint, preflight caching, `.scan()` pagination) is
  logged as Build B2. **Still recommended:** request the Lambda concurrency-limit increase regardless.
- ✅ 2026-08-07 — **Build A (frontend-only): quests visibility, profile settings on its own tab,
  re-login nudge.** (1) **Quests** moved out of the SuperAdmin-only Store tab (where regular users could
  never see or claim them) into their own **Quests** tab, gated by `xpVisible()` like Store; `loadQuests()`
  fires on the Quests-tab open and `loadStore()` on the Store-tab open (were coupled in one branch).
  (2) **Profile customization** (edit name, avatar/banner/background) moved off the burger menu — the
  `open-settings-btn` now sits on the **Player Card** tab header beside Share card; the burger is
  utility-only (logout / display-mode / theme). (3) **Re-login nudge:** new `refreshMySession()`
  force-refreshes the Cognito ID token via `refreshSession` (no full logout), so an account linked or
  repaired server-side after last login picks up its `custom:player_id` in place; surfaced as a
  "Refresh my session" button on the "Finish setting up your account" (unlinked) notice. Frontend-only,
  static deploy, no lambda/DDB change. **Also confirmed:** password-reset was **already shipped**
  (`auth-forgot-view` + `doForgotPassword`/`doConfirmForgotPassword`, reachable via "Forgot password?" on
  the login view) — no work needed. Note: `CODEBASE_MAP.md` predates card-share and now `refreshMySession`
  too; regenerate it in a housekeeping pass.

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
