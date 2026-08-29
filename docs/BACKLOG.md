# NetWorth — Backlog & Roadmap (LLM Reference)

> Shared, append-only-ish work log any assistant can read and extend. When you finish a task,
> move it to **Done** with a date. When you spot new work, add it under the right section with a
> severity/size tag. Keep entries one or two lines; link to `docs/KNOWN_ISSUES.md` items by number
> where relevant. This is a working doc, not a spec — the owner has final say on priority.

**Tags:** `[bug]` `[feat]` `[debt]` `[ops]` `[security]` `[cost]` · size `S/M/L` · e.g. `[debt] M`

---

## Now / high priority

- `[bug] S` **Signup form: no email-typo safeguard, unfriendly "already registered" error.** Owner
  found this while chasing the "forgot password not sending the code" report (2026-08-24) - he'd typo'd
  his own email at signup and only found out when no mail arrived. Proposed fix (not yet built, owner
  redirected to the password-reuse issue before confirming scope - see v1.69.0 in Done): a confirm-email
  retype field on the signup form (blocks submit client-side on mismatch), plus a friendlier message
  when `userPool.signUp` returns "this email is already registered" instead of showing Cognito's raw
  error text as-is.

- `[feat] L` **Manual-mode tournaments (leaders + pool draft/auction) — Phases A-D all done, feature
  complete. Configurable best-of-3 + real visual bracket added 2026-08-22 (v1.58.0, see Done); a
  projected-knockout preview while group ties are still pending added 2026-08-22 (v1.59.0, see Done);
  the same projection extended into the bracket view, plus a per-group advancing/contested panel for
  real named groups, added 2026-08-22 (v1.60.0, see Done); the same group-stage projection surfaced in
  Table view, inline new-player registration during squad substitution, the player leaderboard
  redesigned into a flat, performance-ranked pair leaderboard with placement medals, more sections made
  collapsible, and a downloadable share image of the tournament's CURRENT (in-progress) state, added
  2026-08-22 (v1.61.0, see Done); the leaderboard also got its OWN separate downloadable image, drawn
  with each pair's real picked banner as its row background, added 2026-08-22 (v1.62.0, see Done);
  organizer-only cancel/forfeit actions for a tie match that can't be played, plus a fix so the real-
  named-groups knockout draw pairs adjacent groups deterministically instead of randomly, added
  2026-08-23 (v1.63.0, see Done); a best-of-N tie now decides itself early once one side clinches an
  unbeatable majority (e.g. 2-0 in a best-of-3), instead of demanding every match slot be filled, with a
  "Best of N (first to K)" label added to each tie card, added 2026-08-23 (v1.64.0, see Done); the final
  and third-place match can now each have their OWN "matches per tie" setting, separate from the base
  semifinal/knockout setting (v1.65.0), bundled with a fix for champion/runner-up/third-place leaderboard
  highlighting being completely dead on cross_squad tournaments (v1.66.0), and a same-day follow-up so the
  leaderboard's row ORDER also respects podium finish instead of pure raw stats (v1.67.0, see Done) —
  shipped together 2026-08-23; and a fix so a manual-draft tournament match (group/knockout/third-place)
  actually counts toward a player's lifetime `games_played`/XP/level/coins, matching what a regular
  match has always done (v1.68.0, see Done) — 2026-08-24.**
  Full design: organizer names leaders, splits every player into ranked pools (drag/tap board -
  **done**), leaders draft squads via a live organizer-paced point-budget auction (**done**), then a
  squad-vs-squad group stage (round robin, N individual matches per tie) and knockout (final/semi/3rd-
  place, N matches per tie, default 1 but configurable) auto-generate (**done**). Decided mechanics
  (owner-confirmed): sub-match player picks within a tie are made by **each squad's own leader**, not
  the organizer; a tie level on match-wins is broken by aggregate point differential (cricket-NRR
  style) rather than requiring an odd match count; unsold auction players are resolved manually by the
  organizer, no auto-assign rule. Also wanted alongside this: a tournament-scoped (non-Elo) per-player
  score/leaderboard for the tournament only - Elo itself is untouched, still updates globally per
  individual match exactly as today (**done**).
  - **Phase A - DONE (see Done section, 2026-08-21).** Data model, leader/pool CRUD endpoints,
    `_authorize_tournament_organizer`, drag/tap pool board UI.
  - **Phase B - DONE (see Done section, 2026-08-21).** The auction engine: `start-auction`, `open-lot`,
    `bid` (atomic conditional `update_item`, no new table), `close-lot` (awards + auto-freezes into
    `squads_locked` once every leader's pool quotas are full), `skip-lot`, `get_draft_state` (small
    polling payload, ~1.75s interval, paused on tab-hide/tab-switch/leaving the tab). Auction room +
    bidding UI.
  - **Phase C - DONE (see Done section, 2026-08-21).** Tie-based schedule generation:
    `build_tie`/`build_tie_round_robin`/`build_knockout_tie_round` (generalizing the existing
    `build_round_robin`/`build_knockout_round`), `generate_schedule`, `pick_tie_player` (leader-only
    lineup nomination), `record_group_tie_score`/`record_knockout_tie_score` (score-based tie decision,
    auto-advance, third-place auto-creation), `compute_squad_standings`, `compute_player_tournament_scores`
    (the tournament-scoped non-Elo leaderboard). Tie-card UI with per-leader lineup pickers, standings
    tables.
  - **Organizer-assign (out-of-band bidding) - DONE (see Done section, 2026-08-21).** Owner-requested:
    a way for the organizer to record a winning bid and award a player directly, for auctions run
    partly or fully outside the app where not every leader has it open. `organizer-assign` (organizer
    only, no lot needs to be open) - picks a queued player, a leader, and an amount, then performs the
    exact equivalent of open-lot + winning bid + close-lot in one call. Works standalone or mixed with
    live app-based bidding.
  - **Phase D - DONE (see Done section, 2026-08-21).** Auth-hardening test matrix across every route
    from Phases A/B/C plus organizer-assign, and a real fix for `substitute_player` (both the
    squad-name-rebuild bug and a newly-found crash on manual-draft tournaments).
  - **Pool/auction privacy — DONE (see Done section, 2026-08-21).** Owner request: pool assignments and
    auction budgets/bids restricted to the organizer, and to leaders only while that phase is still
    live - never the general public, and never at all once the phase has passed (leaders included).
  - **Decimal-from-DynamoDB crash on Generate schedule — FIXED (see Done section, 2026-08-21).** Real
    production bug the owner hit live: `range(matches_per_tie)` blew up because DynamoDB always returns
    stored numbers as `decimal.Decimal`, not `int`.
  - **Organizer-assign leader dropdown now excludes quota-full leaders — DONE (see Done section,
    2026-08-21).** Owner request: convenience fix so the dropdown never offers a leader an assignment
    the backend would just reject.
  - **"Not a hard stop": squad roster editing, doubles pairing within a tie, real mid-tournament
    substitution — DONE (see Done section, 2026-08-21, v1.51.0).** Owner asked (2026-08-21) for
    manual-draft tournaments to not be a hard stop once the auction completes: (1) `move-squad-player`
    lets the organizer rebalance squads between the auction and schedule generation; (2)
    `manual_draft.match_type` ('singles'/'doubles', settable at creation) plus a generalized
    `pick_tie_player` let a leader field a nominated PAIR for a doubles tournament's tie matches, not
    just one player at a time; (3) `substitute-squad-player` is real mid-tournament substitution
    (squads_locked through knockout), clearing any not-yet-played lineup pick for the outgoing player
    while leaving already-played match history untouched — replacing the old flat 400 rejection.
  - **Roster-exclusion gap for a non-playing organizer — FIXED (see Done section, 2026-08-21,
    v1.51.0).** Owner-reported real bug: every group member lands in `pools.unassigned` automatically
    at tournament creation, and `lock_pools` refuses to proceed until that tray is empty — an organizer
    who is the group owner/admin but isn't personally playing (health reasons, etc) had no way to
    excuse themselves (or anyone else not participating) from that required roster, and got
    permanently stuck unable to lock pools. New `remove-player` route (pools_open only, rejects
    removing a current leader) plus an "x" control on each unassigned/pool chip.
  - **Organizer can also set a tie's lineup (not just the leader); squad renaming; creation-form
    tooltips — DONE (see Done section, 2026-08-21, v1.52.0).** Owner asked for the organizer/admin to
    also be able to set the pairing within a tie (in consultation with the leader) rather than it
    being leader-only, for leaders/organizer/admin to be able to name their squad, and for an (i)
    tooltip on every manual-draft creation-form field since "Group-stage matches per tie" etc weren't
    self-explanatory.
  - **Real separate groups (Group A/B/C..., random squad assignment, top N per group advance) — DONE
    (see Done section, 2026-08-21, v1.53.0).** Resolved the open question above: owner confirmed they
    wanted genuine separate groups (not the existing single round-robin), randomly assigned, with the
    top 2 per group (configurable via `advance_per_group`) advancing to a combined knockout bracket —
    the rest are eliminated after the group stage. `num_groups=1` (the default) keeps every existing
    manual-draft tournament's behavior byte-identical.
  - Two small, fully independent asks captured alongside this (not gated on the phases above):
    organizer can create a brand-new player profile inline during pool setup (**done** - Phase A
    reuses the existing `/register-and-join` route, no new backend code needed) and an admin "rename
    a player's real name" control (**done, shipped as v1.44.2** - `PUT /players/{id}` already accepted
    a `name` change, only the admin UI was missing).
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
  `networth-deploy-policy.json`; parameterize. **DONE 2026-08-20** (see Done — replaced with the
  same `*` wildcard already used by every other ARN in both files). (KNOWN_ISSUES #3)

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
  the per-player linked-status check. Loop on `LastEvaluatedKey`. **DONE 2026-08-20** (see Done — this
  was previously marked resolved for players/matches only; the other 5 lambdas were still fully
  unpaginated). (KNOWN_ISSUES #15.)

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
  **DONE 2026-08-20** (see Done). (KNOWN_ISSUES #9)
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

- ✅ 2026-08-28 (v1.72.0) — **Group owners get real direct match edit/delete (incl. participants), a
  group label on pending requests, and bulk registration can target/create a group.** Three owner
  reports/asks in one message.
  (1) "i'm still not getting the match edit or delete request of our group ... can we also show which
  group or if global is the match coming from." Investigated: the approval-routing itself was already
  fixed on 2026-08-20 (`match_edit`/`match_delete` are in `OWNER_DECIDABLE_TYPES`, `_owner_may_decide`
  scopes them by the request's own `group_id` exactly like `finance_access`) - a request with no
  `group_id` (an ungrouped, one-off match) has no owner to route to and correctly stays SuperAdmin-only
  by design, which is the most likely explanation for what looked like a missing request. Two things
  fixed regardless: `players/index.py`'s `decide_claim_request` had a stale comment/403 message still
  claiming only claim/rename are owner-decidable, three lines above code that (correctly) already
  handles finance/match types too - corrected the text so it can't mislead a future debugging session.
  And `loadClaimRequests` (app.js) now shows a group-name badge (or "Global / no group") on every
  pending request row, so it's visible at a glance which group (if any) a request is tagged with.
  (2) "the edit option should allow not only to change the score but also the naming as well ... i need
  the ability to edit that as well" (e.g. a wrongly-added participant). Investigated and found the
  backend (`_caller_may_edit_match`, matches lambda) already authorizes a group owner/admin to directly
  edit/delete their own group's matches - identical bar to SuperAdmin, per that function's own comment
  ("going direct isn't a lower bar than request+approve, it's the same approval, minus the detour") -
  and `update_match` already accepts `team_a`/`team_b` changes from any caller it authorizes, not just
  score. The gap was purely in the frontend: `editMatch`/`editMatchScore`/`deleteMatch` all gated direct
  action to `isSuperAdmin()` only, funneling group owners into the slower request flow (score-only, no
  participant-change path at all) even though the backend already trusted them to act directly. Added
  `canActOnMatchDirectly(m)` (SuperAdmin, or owner/admin of the match's own group) and wired it into all
  three call sites plus `matchPermissions()` (which drives the Edit/Delete button labels) - a group
  owner now gets the full players+score edit modal and a direct delete on their own group's matches,
  exactly like SuperAdmin; everyone else's behavior (request flow, ungrouped-match handling) is
  unchanged.
  (3) "under the bulk registration, allow to select a group as well and option to create a fresh new
  group as well from there." Bulk registration previously called the older group-unaware `POST
  /register` with no way to target a group at all (despite its own card copy saying "useful for adding
  a whole new group at once"). Added a group `<select>` (populated the same way the single-registration
  form's already does) plus an inline "Create & use" mini-form (reuses the existing `/group-create` /
  `/groups` endpoints, auto-selects the new group once created) to the Bulk register players card;
  switched the registration loop from `/register` to `/register-and-join` (same route the single form
  uses) so the selected `group_id` is actually applied per player.
  Verified with `/tmp/test_match_owner_direct_edit.js` (10 checks: owner/admin vs plain member vs
  SuperAdmin vs a different group's match vs ungrouped, plus an end-to-end editMatch() run confirming a
  group owner's save PUTs directly and never files a request), `/tmp/test_bulk_register_group_ui.js`
  (12 checks: dropdown population, create-and-auto-select, register-and-join carrying `group_id`,
  optional-group case still working), and `/tmp/test_claim_requests_group_badge.js` (3 checks: real
  group name vs "Global / no group"). Full existing backend suite (35 files) re-run clean.

- ✅ 2026-08-28 (v1.71.0) — **Finance Insights: "Sessions paid" for non-members now reflects real
  sessions, not a raw count of fee entries.** Owner report, with 2 screenshots of the "🎯 Non-members:
  attendance, fees & conversion" table: "the sessions paid is equivalent to how many entries i have
  made for them" — some guests pay per-session (80/slot every day) and were fine, but a guest who pays
  a lump sum in one entry (Shashi: whole month at once, 23 real days shown as "Sessions paid: 2" because
  he only has 2 fee entries) or in batches (some pay 180 every third day; Aniket was a 90/slot weekend-
  only guest) got wildly undercounted. Owner also asked whether 2 slots paid in one entry on one day
  could be made to read the same as 2 single-slot entries on different days (both paying the same total).
  Root cause: `insights()`'s guest-conversion loop (`backend/lambdas/finance/index.py`) did
  `g['sessions'] += 1` per walk-in record, regardless of what the fee actually covered. Fix: new optional
  numeric field `sessions_covered` on a walk-in record (`ALLOWED_FIELDS['walkin']`/`NUMERIC_FIELDS`) -
  defaults to 1 whenever absent/blank/zero, so every existing entry and every guest who already pays
  per-session is completely unaffected. `insights()` now sums `sessions_covered` instead of counting
  entries. Asked the owner (AskUserQuestion) how to fill this field in practice - chose **"Both -
  auto-suggest, still editable"**: new "Sessions covered" input on the Finance → Walk-ins add/edit form
  (`fwalk_sessions`), auto-suggested from that guest's own most recent per-session rate (prefers their
  latest *confirmed single-session* entry as the rate basis, else falls back to the latest entry's own
  implied rate) the moment a fee is typed and the field loses focus (`suggestWalkinSessions()` in
  `app.js`) - always overridable, and a manual edit is remembered for the rest of that form session so a
  later fee edit never clobbers it. Walk-ins list table gets a new "Sessions" column so the value is
  visible/auditable per entry, and the Insights table gets an explanatory caption. **Historical data:**
  owner chose to fix already-logged entries himself via the existing Edit button now that the field
  exists, rather than having me patch specific records - Insights table's existing days-attended /
  fees-paid columns (for linked guests) remain the cross-check for which guests still look off.
  Verified with new `/tmp/test_walkin_sessions_covered.py` (14 checks: API round-trip of the new field,
  unchanged per-session behaviour, the Shashi lump-sum case, a mixed per-session+batched guest, the
  "2 slots one day == 2 single-slot days" equivalence, and 0/blank falling back to 1 not 0) and
  `/tmp/test_walkin_sessions_ui.js` (8 checks: field renders, table column, auto-suggest fill +
  hint text, manual-override protection, and the POST body actually carrying the field).

- ✅ 2026-08-24 (v1.70.0) — **Finance: group-wide expenses now split by total slot-enrollments
  ("portions"), not distinct members; Insights table gets a "Copy as image" button.** Two owner
  requests in one message.
  (1) Group-wide (slot-less) expense split, e.g. shuttle boxes: "you know how the whole group
  calculations happens by splitting the amount to unique members across each slot for that month and
  then divides the amount. can we switch it back to members in all the slots and then if a group has 2
  slots and 3 members share both the groups then they should have 2 portions each ... we have 2 slots
  like 12 and 12 each, the total amount for 5 boxes of shuttle amounts to 6000, 1200 per box, then the
  amount 6000 should be divided into 24 parts ... those who belong to one slot pay for the one slot
  while others who play in both slots will pay for the 2 portions." This deliberately REVERSES the
  narrower 2026-08-20 decision (recorded in a big comment in `_settlement_rows`) that deduped the
  group-wide expense side to distinct members while leaving only walk-in fees slot-weighted - the
  owner now wants both sides using the exact same weighting. Fixed in `backend/lambdas/finance/index.py`:
  the group-wide bucket's denominator (`player_count`) is now `total_slots` (sum of every Yes member's
  slot-enrollments that month, matching `walkin_denominator` exactly) instead of `len(distinct members)`
  - so `cost_per_head`/`residual_per_head` on that bucket become PER-PORTION prices; a new
  `distinct_member_count` field keeps the old headcount available separately for the UI; new
  `expense_shares`/`expense_residual_shares` per-member dicts (mirroring the pre-existing
  `walkin_shares` exactly, including precision - computed from the raw totals divided by `total_slots`
  directly, not from the already-rounded per-portion price times slot count, to avoid compounding
  rounding error) are what `my_settlement` (the "My dues" personal view) and `insights` (the organizer
  Insights table) now actually charge/credit each member, replacing their old flat per-bucket reads.
  Frontend `loadFinanceSummary()` relabels the group-wide row's Members/Per head/Residual columns so
  the portion-based pricing reads clearly (e.g. "24 portions (12 members)") instead of looking like a
  broken headcount; the Insights cost-breakdown tooltip shows a multi-slot member's actual weighted
  share ("1000/portion x 2 slots = 2000") instead of the bare per-portion price. Verified with a new
  `/tmp/test_group_wide_expense_split.py` (21 checks) reproducing the owner's exact worked example
  (2×12 slots, 6000 ÷ 24 = 250/portion) plus an overlapping-membership scenario hand-verified against
  the expected math, and the pre-existing `/tmp/test_group_split.py` had its now-superseded assertions
  (12 distinct members as the denominator) updated to the new correct behavior (18 portions) with new
  `expense_shares` checks added - full backend suite re-run clean (34 files).
  (2) "can you make in the finance section under insights that i can copy the image of the table that
  gets created and not just the 'copy for whatsapp' part. reason being, in desktop it looks fine but in
  mobile only when i make it landscape mode the tabular data is readable, else in normal portrait mode
  it gets squished." New `copyInsightsTableAsImage()` in `app.js`, a "Copy table as image" button next
  to the existing "Copy for WhatsApp" one in the Insights panel - hand-draws the full on-screen 7-column
  table (Member/Slots/Paid/Relief/Effective/Games/₹-per-Game, same Estimated/Actual toggle state as the
  screen) to a canvas, matching this file's existing `downloadTournamentImage`/
  `downloadDraftLeaderboardImage` pattern rather than pulling in a third-party DOM-to-canvas library,
  then copies the PNG straight to the clipboard via `navigator.clipboard.write`/`ClipboardItem` so it
  can be pasted directly into WhatsApp as a real image (unlike copyDuesForWhatsApp's simplified
  4-column monospace text block, which still wraps on a narrow phone) - falls back to a plain download
  when clipboard image support isn't available (anchor appended to the DOM before `.click()` and
  removed after, matching `downloadCSV`'s more robust pattern rather than the three existing tournament
  share-image functions' bare unattached click - fixed there too as a drive-by, since Playwright
  couldn't reliably detect the download event without it). Verified with a new
  `/tmp/test_insights_image_copy_ui.js` (10 checks): drives the real `loadFinanceInsights()` ->
  `renderInsights()` flow against mocked data, confirms the button renders, and exercises BOTH the
  clipboard-copy path and (by explicitly stripping `window.ClipboardItem`) the download-fallback path,
  checking the resulting PNG has real nonzero image bytes either way.

- ✅ 2026-08-24 (v1.69.0) — **Account security: forgot-password let you reset to your OLD password.**
  Owner report, testing the forgot-password flow himself right after the v1.68.0 investigation: "i was
  able to forget password and use my old password again. it should not allow that as well." Cognito
  doesn't block this by default - the `UserPool`'s `LITE` feature tier (the default, and what this pool
  was on) doesn't support `PasswordHistorySize`, the CloudFormation-native setting that blocks reusing
  any of a user's last N passwords across every password-setting path (forgot-password/
  `ConfirmForgotPassword`, `ChangePassword`, and the admin `AdminSetUserPassword` flow alike - one
  setting covers all of them, no per-flow logic needed). Fixed in `infrastructure/template.yaml`:
  `UserPoolTier: ESSENTIALS` (required for `PasswordHistorySize` to take effect - confirmed via AWS's
  Cognito pricing page, which lists "disallowing password reuse" as an Essentials-tier feature, not
  available on Lite) + `Policies.PasswordPolicy.PasswordHistorySize: 3` (blocks reuse of the last 3
  passwords). Essentials pricing is free for the first 10,000 MAU and $0.015/MAU after - a non-issue at
  this app's scale. This is an infra-only change (no lambda/frontend code touched); confirmed the full
  `template.yaml` still parses cleanly (200 resources) after the edit. Deploys through the exact same
  `git tag vX.Y.Z` → `deploy.yml` pipeline as every other change (`deploy.yml` already runs
  `aws cloudformation deploy` against `infrastructure/template.yaml` on every release).
  **Separately investigated, not a bug:** the owner also asked why players get logged out after ~30
  days - that's the Cognito refresh token's default validity (`UserPoolClient` doesn't override
  `RefreshTokenValidity`), working as designed, not a defect; flagged as a possible future tweak if the
  owner wants a longer session, not actioned here. Also investigated "forgot password not sending the
  code" - initially suspected the `UserPool`'s email delivery (no SES/`EmailConfiguration` configured,
  so it's on Cognito's default sender, which is capped at 50 emails/day account-wide and has poor
  deliverability) but the owner's own test account received its email fine, and he traced the real
  affected users to typo'd email addresses at signup instead - existing admin panels ("Unconfirmed
  sign-ups" and "User → profile mapping" under Access Reviews & Approvals) already surface exactly
  those accounts, no new code needed to find them. A signup-form fix (confirm-email retype field +
  friendlier "already registered" messaging) was proposed but the owner redirected to this password-
  reuse issue before confirming scope - still open, see "Now / high priority" if picked back up.

- ✅ 2026-08-24 (v1.68.0) — **Manual-mode tournaments: fixed lifetime `games_played`/XP/level/coins never
  incrementing for tournament matches.** Owner report, real live tournament, `networthmatches.csv` export
  attached: "see tanay nitish who played in this tournament atleast played knockout, should've had 5 or
  more than 5 matches. they are getting displayed under season which i believe doe not have a minimum
  requirement, but in player card i can see matches were getting registered for both of them, but still,
  they are showing up as 3/5 matches played for the ranking? is it getting missed somehow?" The player
  cards and the `matches` table itself were both correct (Tanay genuinely had 6 real matches logged,
  Nitish 8) — the bug was in what the "View Rankings" screen reads: `loadRankings()` (app.js) filters on
  the player's persisted `games_played` field directly, with a flat `MIN_GAMES = 5` floor (unrelated to
  the season-badge system's own separate min-games logic).
  Root cause: `update_elo_and_log` in `tournaments/index.py` — the ONE function that records every
  manual-draft tournament match's Elo update, for every stage (group/knockout/third-place) — only ever
  did `SET rating = :r` on each player. It never touched `games_played`, `previous_rating`, `xp`, `level`,
  `coins`, or `coins_earned`, unlike the equivalent regular-match path in `matches/index.py`'s
  `_play_and_log`, which updates all of those together in one `update_item` call. So a player who only
  ever played tournament matches showed whatever `games_played` value an earlier full `recompute_all_ratings()`
  run (in `matches/index.py`, which DOES replay the whole shared `matches` table correctly, tournament
  matches included) happened to stamp — frozen forever after that point, no matter how many more
  tournament matches they went on to play. Tanay's "3" lines up exactly with a recompute that ran after
  his 3 group-stage matches but before his knockout + third-place matches existed.
  Fix: ported the XP/level/coins constants and helpers (`XP_PLAYED`, `XP_WIN_BONUS`, `XP_LEVEL_COEFF`,
  `COINS_PER_LEVEL`, `level_from_xp`, `xp_for_match`, `event_multiplier_for_date`) into
  `tournaments/index.py` (KNOWN_ISSUES #6 duplication convention, kept byte-identical to `matches/index.py`
  so a future shared recompute stays consistent regardless of which lambda logged a match), and extended
  `update_elo_and_log`'s player-update loop to mirror `_play_and_log` exactly: `previous_rating` snapshot,
  XP award (stage-aware — group/knockout/third_place all have their own XP tier, same table as regular
  matches), level, coin balance, and `games_played = if_not_exists(games_played, :zero) + :one`. Elo
  `rating` math itself is byte-identical to before — this only ADDS fields, never changes ranking numbers.
  `/tmp/test_tournament_games_played.py` (new, 16 checks): drives `update_elo_and_log` directly across
  group/knockout/third-place matches for one pair and confirms `games_played` climbs by 1 each time
  instead of freezing, `previous_rating` is correctly re-snapshotted match-to-match, stage-specific XP
  stacks correctly, a doubles tie bumps both teammates, and a fresh 1v1 match's Elo number is unchanged
  from the pre-fix formula. Full backend suite re-run clean (33 files); leaderboard placement/sort UI
  tests re-run clean (unaffected, backend-only change).
  **Follow-up needed after deploy:** this bug has silently affected every manual-draft tournament ever
  played, not just this one — every player who has played ONLY tournament matches (or whose tournament
  matches came after their last recompute) is undercounted right now. Recommended: after this ships, run
  a `recompute_all_ratings()` (the `matches/index.py` copy, via the admin `/recompute` route — it replays
  the ENTIRE shared `matches` table including tournament-sourced rows) once, to backfill `games_played`/xp/
  level/coins correctly for every already-recorded tournament match, not just ones going forward.
  **Known related gap, not fixed here (kept out of scope for this change):** `recompute_all_ratings()` in
  `tournaments/index.py` (used only when deleting a tournament, to safely unwind that tournament's Elo
  deltas) still only recomputes `rating`/`ratings_after`, not `games_played`/xp/coins — so deleting a
  tournament today won't correctly unwind those fields either. Flagged for a future pass rather than
  bundled into this fix, to keep this change tightly scoped to the reported bug.

- ✅ 2026-08-23 (v1.67.0) — **Manual-mode tournaments: podium finish now outranks raw performance stats on
  the player leaderboard.** Owner follow-up, same live tournament, right after v1.66.0's highlight fix
  landed and correctly lit up the medals: "why is the 3 position team not moving up. this is like doing
  based on number of played matches or what? can't we fix this?" — the bronze medalist (Tanay & Saurav
  Ashok) was now correctly highlighted, but still sat in 4th ROW POSITION, below a pair (Guddu & Mirgank)
  who never reached the podium at all but had a better raw win/loss record.
  `computeLeaderboardRows`'s sort was pure regular-season performance (wins, then point-diff, then matches
  played) with no regard for how the knockout bracket actually finished — a real, common situation, since
  the bronze medalist's own run typically INCLUDES a semifinal loss (that's what put them in the third-
  place match to begin with), so their win tally can easily be lower than a squad that never even made the
  final four. Fixed by pinning the exact podium-deciding pair (`isDecidingPair` — specifically the pair
  that played the final/third-place match, not every squadmate who merely shares that squad's thinner
  tier-colored edge, since a squad can field several unrelated pairs and only one of them actually earned
  the medal) to the top 3 rows in gold/silver/bronze order; the existing wins/point-diff/matches-played
  tiebreakers now only apply within a tier (gold-vs-gold is a non-issue, there's only ever one) and among
  the rest of the field below the podium — unchanged from before for everyone off the podium.
  `/tmp/test_cross_squad_leaderboard_placement_ui.js` extended (+5 checks, 16 total): a non-podium pair
  with a strictly better win record than the bronze medalist is confirmed to rank BELOW bronze but still
  ABOVE the worse-performing non-podium pairs (i.e. still correctly performance-ranked among its own
  tier). The pre-existing `/tmp/test_leaderboard_pairs_ui.js` had its row-order assertion updated to match
  the new INTENDED behavior (its old assertion encoded the pre-fix ordering, which this change deliberately
  reverses for the podium pairs) — the runner-up (silver, worse raw stats) now correctly sorts ahead of
  both the third-place pair (bronze) and a non-podium pair with better stats, exactly reproducing the
  owner's live complaint in miniature. Full suite re-run clean (32 backend files, 18 UI files).

- ✅ 2026-08-23 (v1.66.0) — **Manual-mode tournaments: fixed champion/runner-up/third-place highlighting
  being completely dead on the player leaderboard for cross_squad tournaments.** Owner report, on the SAME
  live tournament right after cancelling the third-place tie's moot slots: "i cannot see this it
  reflecting proiperly. the leaderboard is misleading tranay and shourav won the 1 matchs but in their row
  it shows up as 6 matches but it should be 7 ... and tanhay and sourav should be showing 4-3 and in third
  position, but nothing is getting higlighted."
  Investigated against the owner's real live `dump.json`: the matches-played/win-loss NUMBERS were actually
  already correct - Tanay & Saurav Ashok's own pair lost their OWN semifinal 0-2 to a *different* pair from
  their *same* parent squad (Nitish & Sandeep, the other rep that squad fielded in the other group - this
  is the same "C and D make tanay's teams pitch against each other" scenario confirmed correct at the very
  start of this whole thread), so their pair went into the third-place match already 2-3, and the 21-19
  third-place win correctly brought them to 3-3 across 6 matches - matching what was on screen exactly.
  The REAL bug was the "nothing is getting higlighted" half: `computeLeaderboardRows`'s champion/runner-up/
  third-place placement lookup was keyed by plain squad id (`squadOf[pid]`, built from `t.squads`, whose
  member list spans a squad's WHOLE roster across every rep), but `finalTie.winner_squad_id`/`squad_a`/
  `squad_b` and `third_place_match.winner_squad_id` are REP ids (`"{squad_id}::repN"`) in cross_squad
  mode, not plain squad ids - so the lookup silently matched nothing, for ANY row, on ANY cross_squad
  tournament that ever reached `completed`. Worse, since a squad's own reps all share one plain squad id,
  even fixing the id mismatch naively would have collided two of a squad's own placements together -
  exactly this tournament's shape, where Team Tanay's rep0 (Nitish & Sandeep) finished runner-up (silver)
  while rep1 (Tanay & Saurav Ashok, a completely different pair) finished third (bronze) at the same time.
  Fixed by building a reverse `pairKey -> repId` map from `t.reps` and preferring an exact rep match over
  the plain-squad-id fallback when looking a row's placement up, so each rep now gets its own independent
  placement entry with no collision. For a normal (non-cross_squad) tournament `t.reps` is empty/absent,
  so every row falls through to the squad-id lookup exactly as before - byte-identical behavior, no
  regression. Shared by both the HTML leaderboard and the downloadable leaderboard share image, since both
  already ran through the same `computeLeaderboardRows` helper.
  New `/tmp/test_cross_squad_leaderboard_placement_ui.js` (11 checks): a fixture reproducing the exact live
  shape (one parent squad fielding two independent reps, one runner-up + one third-place) confirms the
  champion gets gold, the runner-up gets silver, the third-place pair gets bronze, the two same-squad
  placements no longer collide, and an uninvolved pair gets none. Full existing suite re-run clean (32
  backend files, 18 UI files including the new one).
  Bundled into this same release with v1.65.0's per-stage matches-per-tie work below, since the owner
  hadn't deployed v1.65.0 yet when this was found - ship both together as v1.66.0.

- ✅ 2026-08-23 (v1.65.0) — **Manual-mode tournaments: separate "matches per tie" settings for the final
  and third-place match, distinct from the semifinal/base knockout setting.** Owner follow-up right after
  confirming the cancel-workaround for the third-place tie would work for now: "next time onwatds itr
  will ask me how many sets for eacxh semis or finals or third place matches?" — direct callback to the
  same live event where semis+final were played best-of-3 to 11 but third place was a single match to
  21, which is exactly why the third-place tie needed the cancel-workaround at all: `knockout_matches_per_tie`
  was one global number shared by the WHOLE knockout stage (semis, final, AND third place alike), so
  there was no way to configure a tournament ahead of time to match that real format split.
  `create_manual_draft_tournament` now accepts two new optional fields, `final_matches_per_tie` and
  `third_place_matches_per_tie`, each defaulting to `knockout_matches_per_tie` when omitted — a
  tournament that never touches these fields behaves byte-identically to before. Both are snapshotted
  into `item['knockout']` (alongside the existing `matches_per_tie`) the moment the bracket is first
  built, so a later change to the tournament's `manual_draft` config can't retroactively reshape an
  in-progress bracket. `_advance_knockout_ties_if_round_complete` picks `final_matches_per_tie` for
  whichever round comes out to exactly one tie (the final) and `third_place_matches_per_tie` for the
  auto-created third-place match, falling back to the base `matches_per_tie` for every earlier knockout
  round (semis, or quarters+semis on a bigger bracket); the very first knockout round built straight off
  group-stage qualifiers also picks up `final_matches_per_tie` in the edge case where it's already down
  to a single tie (only 2 total qualifiers, no separate semifinal round). Frontend: two new optional
  create-form fields, "Final matches per tie" / "Third-place matches per tie", both left blank by default
  (placeholder "same as knockout") and only included in the create payload when actually filled in, so
  the backend's own default-to-`knockout_matches_per_tie` fallback is what applies for the common case;
  `renderTieCard`'s existing "Best of N" label (added in v1.64.0) already reads straight off
  `tie.matches.length`, so it automatically shows the right count per-stage with no extra plumbing.
  New `/tmp/test_per_stage_matches_per_tie.py` (12 checks): explicit values are stored as given, omitted
  overrides default to `knockout_matches_per_tie`, invalid values (<1) are rejected, a straight
  2-qualifier final (no semis) is built with `final_matches_per_tie`, and — reproducing the real event's
  exact 4-squad shape end to end — both semifinals build with the base `knockout_matches_per_tie` (3),
  the final builds with `final_matches_per_tie` (3), the third-place match auto-creates with
  `third_place_matches_per_tie` (1) and correctly pits the two semifinal losers against each other, and a
  single 21-15 game fully decides that 1-slot third-place tie with no cancel-workaround needed. New
  `/tmp/test_per_stage_create_form_ui.js` (11 checks): the two new create-form inputs exist and are
  clearly marked optional, leaving them blank omits the fields from the create payload, filling them in
  includes the values submitted.

- ✅ 2026-08-23 (v1.64.0) — **Manual-mode tournaments: a best-of-N tie now decides itself the instant one
  side clinches an unbeatable majority, instead of demanding every match slot be filled.** Owner report,
  right after v1.63.0's cancel feature shipped, from the SAME live tournament: a knockout tie configured
  `knockout_matches_per_tie=3` (best of 3) finished 2-0 (Nitish & Sandeep Singh Rathore beat Tanay &
  Saurav Ashok twice) — "i thing the tanay saurav and nitish sandeep match only had 2 since both the
  matches were won by nitish sandeep. so i have entered the 2 matches but i can see the third match
  still shows up. it should not be." Also asked for a way to "see how many matches are to be done in
  each of these" (best-of-N ties had no visibility into their own configured length).
  `_update_tie_progress` now computes `needed_wins = len(matches)//2 + 1` and decides the tie the moment
  either side reaches it — standard best-of-N majority math (2 of 3 can't be caught by a third match),
  so a real 2-0 sweep completes immediately and the third slot is simply never needed, exactly matching
  how a real best-of-3 series is actually played (nobody plays game 3 after 2-0). A tie that ends up
  evenly split with no early majority (e.g. 1-1 in a best-of-2) still needs every slot resolved and
  still falls back to the existing point-diff tiebreak, unchanged. Once a tie is decided this way,
  `_score_tie_match`/`_cancel_tie_match`/`_forfeit_tie_match` all now reject any further action on its
  remaining match slot(s) (400 "this tie is already decided") — both because it can't change the
  outcome, and because letting a moot match's score still add to `point_diff_a`/`point_diff_b` would
  leak into that squad's standings tiebreaker even though the match had no bearing on this tie.
  Frontend: `renderTieMatchRow` renders an already-moot, not-yet-played slot as a plain "Not needed –
  tie already decided" line instead of live score-entry/cancel/forfeit controls (fixes the exact "it
  should not be [showing]" complaint); `renderTieCard`'s header now shows a "Best of N (first to K)"
  label (e.g. "Best of 3 (first to 2)") computed straight from `tie.matches.length`, giving the
  visibility into configured match count the owner also asked for.
  New `/tmp/test_tie_early_decision.py` (9 checks): a 2-0 sweep in a best-of-3 decides immediately with
  the third slot left untouched, that moot slot then rejects scoring/cancelling/forfeiting, its
  point-diff never leaks in, a best-of-2 needs both slots and correctly falls back to the point-diff
  tiebreak on an even split, and — reproducing the exact live scenario end to end — a knockout semifinal
  configured best-of-3 completes the whole tournament (2 real teams) on a 2-0 sweep. `/tmp/test_tie_cancel_forfeit_ui.js`
  extended (4 more checks, 15 total): the "Best of 3 (first to 2)" label renders, the moot third slot
  shows "Not needed" with no Cancel/forfeit buttons on it specifically, and the two real, played matches
  still show their actual scores untouched.

- ✅ 2026-08-23 (v1.63.0) — **Manual-mode tournaments: cancel/forfeit for a tie match that can't be
  played, and a fix for the real-named-groups knockout draw being randomly paired.** Live-event owner
  report: "the two group stage matches were pending due to which i couldn't enter the semis... the group
  stages wouldn't happen due to players availability now. what should i do?" followed by "just let me
  cancel these two matches or somehow draw them out if not cancelled... need to check if post the logic
  of cancelling match happened if everything works fine. Also keep an option for forfeit when either of
  the team doesn't show up."
  Two new organizer-only actions on any not-yet-played tie match, group stage or knockout/third-place:
  **cancel** (`cancel-group-tie-match` / `cancel-knockout-tie-match`) — the match can never be played
  (players unavailable on both sides) and should count toward neither side, but must stop blocking
  advancement; **forfeit** (`forfeit-group-tie-match` / `forfeit-knockout-tie-match`, body
  `{tie_id, match_index, forfeited_by: 'a'|'b'}`) — one side didn't show up, so the other side is awarded
  the match win outright. Neither touches Elo (no real result happened) and neither requires a lineup to
  have been nominated first — forfeit deliberately doesn't, since the whole point is covering the side
  that never got to nominate anyone because they never showed up.
  `_update_tie_progress` now tracks a match's win by *side* (`forfeited_by`) rather than only by
  resolving `winner_id` against `player_a`/`player_b`, and excludes a `cancelled` match from both the win
  tally and the point-diff tally entirely, as if it never existed. If EVERY match in a tie ends up
  cancelled, the tie itself now resolves `decided: True, winner_squad_id: None, cancelled: True` instead
  of falling into the pre-existing genuine-score-deadlock branch (which would otherwise leave it stuck
  pending forever, since 0-0 looks identical to a real deadlock). `compute_player_tournament_scores`
  excludes cancelled matches too, so they don't inflate anyone's tournament-scoped leaderboard with a
  phantom 0-0 "played" match. Cancel/forfeit are organizer-only (`_authorize_tournament_organizer`) —
  stricter than normal scoring, which a tie's own squad leader can also submit.
  Separately, and directly relevant to the same live event: `_advance_squads_to_knockout_from_groups`
  (real-named-groups mode) used to shuffle group qualifiers into the knockout draw with
  `random.shuffle` — the owner's actual live bracket was "the group A qualifies went against group B
  qualifies and similarly c with D" (always adjacent groups in name order), which the random draw had no
  guarantee of reproducing. Changed to a deterministic pairing: qualifiers are now collected in
  `sorted(groups.keys())` order and paired consecutively (A-B, C-D, ...), so the app's auto-generated
  semifinal draw always matches what a real live event actually plays. The existing same-group-avoidance
  swap (for `advance_per_group > 1`) is unchanged, just no longer needed to fix up shuffle-induced
  same-group adjacency in the common `advance_per_group=1` case.
  Frontend: `renderTieMatchRow` gets a small "Cancel match" / "`<Squad A>` forfeits" / "`<Squad B>`
  forfeits" control row wherever a match isn't yet played (works whether or not a lineup has been picked
  yet, matching forfeit's no-nomination-required design) - shown to everyone since there's no reliable
  client-side "am I the organizer" flag (same convention as the lineup pickers), with the backend
  enforcing the real check. An already-resolved match shows "Match cancelled – no result" or "Forfeited
  by `<name>`" instead of a score. Fixed a latent bug the cancelled-match case would otherwise have hit:
  the banner-card winner-highlight ternary used to fall through to "side B won" whenever `winner_id` was
  `null` (i.e. for any cancelled match), rather than showing no winner at all.
  New `/tmp/test_tie_cancel_forfeit.py` (16 checks, backend): cancel/forfeit authorization (organizer-
  only, stricter than a tie's own leader), Elo untouched by either action, cancelling before any
  nomination, a mixed tie (one slot cancelled, one played normally) deciding purely on the played slot,
  a fully-cancelled tie resolving with no winner, `compute_player_tournament_scores` excluding cancelled
  matches, and — repeated across 3 runs to prove it's no longer random — the real-named-groups knockout
  draw deterministically pairing Group A-vs-B and Group C-vs-D once the last pending group match is
  cancelled. New `/tmp/test_tie_cancel_forfeit_ui.js` (11 checks, Playwright): cancel/forfeit controls
  render with the right squad-name labels before any lineup is picked, clicking them posts the right
  body to the right route, and already-resolved matches show the cancelled/forfeited badges with no
  leftover admin controls.

- ✅ 2026-08-22 (v1.62.0) — **Manual-mode tournaments: a SEPARATE downloadable image for the pair
  leaderboard, distinct from the group/standings share image.** Owner, once both the v1.61.0 standings
  share image and the redesigned flat pair leaderboard were live: "can we not have the leaderboard image
  downloadable or shareable separately? i meant the group one is useful when sharing it as an image
  before the matches start or in the middle, but yeah this leaderboard one with custom background being
  picked by different banner looks cooler." Two distinct exports for two distinct moments, not merged
  into one image. Refactored `renderPlayerTournamentStatsTable`'s pairing/ranking/placement logic out
  into a standalone `computeLeaderboardRows(stats, t)` (returns the same ranked-pair-row array the HTML
  render consumes), so the new canvas export and the on-screen leaderboard can never drift apart - one
  source of truth for who's paired with whom, the sort order, and which pair gets which placement ring.
  New `downloadDraftLeaderboardImage()` (sibling of `downloadDraftShareImage()`): draws one row per pair
  exactly as computed, with each row's background matching what `teamBanner()` would actually pick for
  that pair on screen - the real uploaded banner photo when there is one (loaded via the same crossOrigin
  `Image()` + cover-crop technique the avatar/match-card exports already use), or a 2-stop canvas gradient
  approximating that preset's CSS colors otherwise (`LEADERBOARD_PRESET_CANVAS_COLORS`, hand-picked from
  each `BANNER_PRESETS` entry's own gradient stops, since canvas can't render an arbitrary multi-layer CSS
  gradient/pattern string directly) - so the "different banner per pair" look the owner liked on screen
  survives into the exported PNG rather than flattening to one plain background. Gold/silver/bronze rings
  carry over identically. New "Download leaderboard image" button next to the existing (now relabeled
  "Download standings image" for clarity) share button in `renderDraftScheduleView`, shown whenever
  `player_tournament_stats` has data. `/tmp/test_draft_round5_ui.js` extended (now 18 checks): both
  buttons present and independently labeled, `downloadDraftLeaderboardImage()` completes and triggers a
  download without throwing, alongside the existing standings-image checks.

- ✅ 2026-08-22 (v1.61.0) — **Manual-mode tournaments: group-stage projection in Table view, inline
  new-player registration for squad substitution, squad-banner leaderboard redesign, more collapsible
  sections, and a "share current state" image export.** Five owner requests in one message, right after
  confirming v1.60.0's bracket-view fix was just a stale-cache issue, not a real bug:
  (1) "the table view does not show the semi finals, only in bracket it shows predicted" — v1.60.0's
  `group_stage_projection` was only wired into the Bracket view's new panel, never into Table view's
  existing per-group standings tables. `renderSquadStandingsTable(standings, projection)` now takes an
  optional `projection` argument (the matching entry from `t.group_stage_projection`), adding a "Status"
  column (gold "→ Knockout" for advancing squads, italic muted "Contesting last spot" for the boundary
  tie) plus a pending-ties note below the table — same data v1.60.0 already computes, just surfaced in
  the view that was missing it.
  (2) "i can add an existing player as a substitute but not a new person like it can be done in other
  formats" — ported the legacy tournament flow's own "register a new player inline" pattern
  (`#sub_new_player_toggle` → `POST /register` → use the returned `player_id`) to manual-draft squad
  substitution: new `#squad-sub-new-toggle` checkbox reveals `#squad-sub-new-register-fields`
  (name + skill level), disables the existing free-agent `<select>` while checked, and
  `substituteSquadPlayer()` now branches to call the plain `POST /register` endpoint first when adding a
  new player (no backend change needed — `substitute_squad_player` only requires the replacement not
  already be on any squad, not that they're a group member, so the same unauthenticated-by-group-id
  `/register` route the legacy flow already uses is sufficient here too).
  (3) "the leaderboard that shows the people and points, rather can we have a teams banner witht the
  pairs ... each player's profile and banner image being used" — `renderPlayerTournamentStatsTable`
  rewritten to take a second `t` argument and group `player_tournament_stats` by squad (via a
  player_id→squad_id map built from `t.squads`), rendering one banner card per squad (squad name over a
  `teamBanner()`-picked background, reusing the exact same `vsPlayerVisual`/`vsAvatarHtml` helpers the
  match banner cards already use) with each member's photo avatar plus their Played/W/L/point-diff stats
  underneath — the stats entry's own server-computed `name` wins over the live-roster name for the label
  (a sub-match participant isn't guaranteed to already be in the loaded player list). Any stat entries
  that don't map to a squad member fall back to the old plain table underneath, so nothing silently
  disappears. Revised twice before first deploy, both still under v1.61.0 (never tagged/deployed yet, so
  no version bump needed either time): (a) compactness pass (owner: "i hope the leaderboard is showing
  the banner style but does not too much of height, it should feel compact") - swapped stacked
  photo+name+stats per player for a smaller 34px avatar with name and one terse stat line side-by-side.
  (b) real DOUBLES-PAIR grouping + placement medals, revised TWICE more after the owner saw (a) render
  every squad member as its own row grouped into per-squad boxes:
  (b1) first pass grouped pairs INSIDE per-squad boxes, one box per squad, squad name centered as a
  banner headline. Clarified via AskUserQuestion that manual-draft doubles partners aren't a
  separately-stored field (`pick_tie_player` lets a leader nominate a different 2-person combo for every
  match - see backend investigation), but the owner pointed out the real tell: "you can see the ones with
  the same points are the partners" - since `compute_player_tournament_scores` credits BOTH members of a
  played doubles match the identical result, two players who've always played together end up with
  identical Played/W/L/diff numbers - this became (and remains) the pairing-reconstruction mechanism.
  (b2) owner then rejected the per-squad grouping entirely on seeing a real 16-pair tournament screenshot:
  "i don't need the squads grouped. rather it should be mixed as each pair performed differently ... so
  there would be 16 rows as 16 pairs competed where 4 of each squad" - plus "the profile pic ... is
  getting overlapped" (the two avatars used a negative margin to overlap, reading as squished/merged in
  the actual screenshot). Rebuilt as a single FLAT, PERFORMANCE-RANKED list (no squad containers, no squad
  header divs at all) sorted by (wins desc, point_diff desc, matches_played desc) across the WHOLE
  tournament, mixing all squads' pairs together by how they actually performed. Pair reconstruction
  unchanged (scans `group_stage.ties` + `knockout.rounds` + `knockout.third_place_match` for 2-person
  `player_a`/`player_b.members` sides, pairs each player with their most-frequent mutual partner, falls
  back to solo for singles tournaments or anyone with no played match yet) but no longer scoped per-squad -
  built once, ranked, one row per pair regardless of which squad it belongs to. Each row: two adjacent
  (non-overlapping, small gap - the fix for the overlap report) 30px avatars, "Name & Name", the squad
  name + stats stacked in a small right-aligned column (satisfies "with their team name either in the
  beginning or [at] the other end"). Placement rings adapted for the flat layout since there's no more
  squad container to ring: the SPECIFIC pair that played the deciding match gets a full glow ring + a
  medal emoji ("🥇 Champion", "🥈 Runner-up", "🥉 Third place" pairs), while every OTHER pair belonging to
  that same placed squad gets a thinner tier-colored edge only - so "the entire squad" placing still reads
  across all of that squad's rows, while the pair actually on court for the deciding match stands out
  further, matching the owner's two-part clarification from the prior round. New
  `/tmp/test_leaderboard_pairs_ui.js` (Playwright, 11 checks, rewritten for the flat design: pairs still
  correctly grouped from match history, NO standalone squad-header elements exist inside the leaderboard
  card, row order interleaves squads by rank instead of clustering by squad, no more negative-margin
  avatar overlap, exactly one strong ring per placement tier plus the correct count of thin edges on a
  same-squad non-deciding pair, medal emoji present, squad names still shown as row labels).
  (4) "can you make other sections also collapsible, like the pairing sections under each squad" — added
  a second tracked-open-state `Set` (`draftOpenSquadSections`, deliberately separate from the existing
  `draftOpenGroups` since a squad_id and a group name could collide, e.g. both "A") + `toggleDraftSquadSection`;
  converted `renderDraftSquadsReview`'s and `renderSetSquadPairsPanel`'s per-squad blocks from plain
  `<div>`s to collapsible `<details>` (keys `review:${sid}` / `pairs:${sid}`), the pairing one's summary
  line also previewing the squad's currently-set pairs (or "not set yet") so you don't have to expand it
  just to check.
  (5) "need a way to share these current state as well ... shareable to insta" — new
  `downloadDraftShareImage()`, a manual-draft sibling of the existing (legacy-only, completed-only)
  `downloadTournamentImage()`: works at ANY schedule status (group stage in progress, knockout in
  progress, or completed) since a live event wants to share progress as it happens, not just the final
  result. Canvas-rendered: tournament name/status header, one standings block per real named group (or a
  single flat block) with the same advancing/contesting gold-highlight as the Table view, then the
  current-or-most-recent knockout tie as a photo banner card (dashed border + "PROJECTED" label for the
  flat case's still-projected pairing, solid + score for a real in-progress or decided tie), then a
  champion line once completed. Exported via `canvas.toBlob(...,'image/png')` + a client-side download
  link, following the same crossOrigin-image-loading/`roundRect`/avatar-circle patterns
  `downloadTournamentImage()` already established. New "Download share image" button in
  `renderDraftScheduleView`, shown whenever a schedule exists (not gated to `completed`).
  New `/tmp/test_draft_round5_ui.js` (Playwright, 14 checks: Table view shows the advancing marker +
  pending-ties note, leaderboard renders squad-name card headers + a photo/avatar block per player +
  keeps the "not Elo" disclaimer, the new-player substitution toggle shows/hides its fields and disables
  the free-agent dropdown and calls `POST /register` exactly once, the share-image button is present and
  `downloadDraftShareImage()` completes and triggers a download without throwing, and it alerts-and-
  returns gracefully for a tournament with no schedule yet instead of throwing). Two pre-existing
  Playwright tests updated for the intentional collapsible-panel/name-source changes rather than left
  broken: `test_cross_squad_ui.js` (the pairing panel's "Currently set" text became a compact summary-
  line preview; its selects now need their `<details>` opened first) and `test_draft_schedule_ui.js` (no
  assertion change needed — was a real bug this round caught: the redesigned leaderboard cards were
  showing the live-roster name instead of falling back to the stats entry's own name for a player not
  yet in the loaded roster; fixed in `renderPlayerTournamentStatsTable`). Full existing backend (29
  files) and Playwright (15 files, including the new one) suites re-verified passing throughout.

- ✅ 2026-08-22 (v1.60.0) — **Manual-mode tournaments: bracket-view group panel + projected
  pairing in the SVG itself, for real named groups.** Immediate owner follow-up on v1.59.0: "will the
  v1.59.0 version not show the probable semifinal matches in table or bracket section in staging?" →
  confirmed the projection only rendered in Table view, not Bracket → owner: "add it in the bracket view
  as well and the bracket only shows knockout. so can't we have the groups and their teams listed inside
  each and the ones who [advance] to be projected to the knockout stage as well." Turned out Rally Royale
  uses real separate named groups (confirmed by the owner checking their screen), which v1.59.0's
  `compute_projected_knockout` deliberately didn't cover (it only handles the flat single-round-robin
  case). New `compute_group_stage_projection(item)`: per named group, returns that group's own standings
  (`compute_squad_standings` scoped to the group, exactly what `group_standings` already shows in Table
  view), `advancing_ids` (currently-safe top `advance_per_group` squads), `contested_ids` (populated only
  when the group's standings are EXACTLY level - both `ties_won` and `point_diff` - right at the
  `advance_per_group` cutoff, mirroring the exact same boundary check
  `_inject_group_tiebreakers_if_needed` uses for real, rather than guessing who wins a tie the real system
  would resolve with an extra match), and `pending_ties` (that group's own undecided-tie count).
  **Deliberately does NOT attempt a projected knockout PAIRING for this case** (unlike the flat
  round-robin's `projected_knockout`): the real advancement path
  (`_advance_squads_to_knockout_from_groups`) draws the knockout pairing among qualifiers RANDOMLY (with
  a same-group same-round-1 avoidance swap), so even once every qualifier is 100% known there is no
  single deterministic matchup to show - showing one anyway would be actively misleading. Attached to
  `get_tournament` as `item['group_stage_projection']` (read-time-only) whenever `status=='group_stage'`
  and real named groups exist; `projected_knockout` and `group_stage_projection` are mutually exclusive
  per tournament. Frontend: new `#bracket-groups-panel` div (index.html, sits above `#bracket-svg`,
  toggled together with it by `applyTournamentViewMode`) populated by new `renderDraftBracketGroupsPanel`
  — one card per group listing every squad, advancing ones bold + "→ knockout", the contested boundary
  pair shown in italics + "(contesting last spot)", a pending-match-count note per group, and a plain
  disclaimer that the real pairing is drawn randomly once groups finish. Separately, `renderDraftBracketView`
  now also draws the flat round-robin case's `projected_knockout` directly into the SVG (one dashed
  preview box per projected tie, labeled "Projected (preview - N group matches still pending)") instead
  of just saying "No knockout bracket for this tournament yet" — this part *is* deterministic, so it gets
  drawn as a real (if dashed) box rather than a separate panel. New `/tmp/test_group_stage_projection.py`
  (6 checks: appears with real groups present, one entry per group, correctly separates safely-advancing
  from a genuinely-tied contested pair with the right pending-tie count, a clean non-tied boundary group
  shows no contested_ids, each group's `squads` field is the real per-group standings, disappears once
  status leaves `group_stage`). New `/tmp/test_bracket_groups_panel_ui.js` (Playwright: groups panel shows
  in bracket mode with both groups/all 6 squad names/advancing markers/the contested flag/the pending-count
  note, hides again in table mode, the flat round-robin case draws its projected pairing straight into the
  SVG instead of "no bracket yet", and the groups panel correctly stays empty for that flat case). Full
  existing backend (29 files) and Playwright (13 files, including the two new ones) suites re-verified
  passing throughout.

- ✅ 2026-08-22 (v1.59.0) — **Manual-mode tournaments: projected knockout matchup while group
  ties are still pending.** Live-event owner report: "2 matches are pending in group stage but we
  clearly see the teams qualifying for semifinal, will that matchup not be released? ideally it
  should be right?" The real bracket only gets built once every single group tie is `decided` (see
  `record_group_tie_score`), so it genuinely doesn't exist in the data yet while any tie is still
  pending, however obvious the outcome looks on the standings table. Rather than trying to prove
  qualification is mathematically locked (`point_diff`, part of the tiebreak, is unbounded, so that
  would need real worst-case-swing reasoning - too much to rush into a live event), new
  `compute_projected_knockout(item)` computes a clearly-labeled **preview** instead: it reads
  `compute_squad_standings`' current output (which already only credits *decided* ties, so it's
  exactly what the standings table already shows) and runs it through the existing
  `build_knockout_tie_round` seeding — same pairing the real knockout would use once it exists —
  without creating any real tie objects, writing anything, or touching scoring/Elo in any way.
  Attached to `get_tournament`'s response as `projected_knockout` (read-time-only, same "never
  persisted" convention as `squad_standings`/`player_tournament_stats`) whenever `status ==
  'group_stage'` and at least one group tie is still pending; automatically absent again the moment
  every tie is decided (the real `knockout` supersedes it) or once real separate named groups
  (`group_stage.groups`) are in play — `advance_per_group`/tiebreaker-injection eligibility isn't
  replicated here, so this deliberately returns `None` for that case rather than guessing at a shape
  that could turn out wrong. Frontend: new `renderProjectedKnockout(t)`, a plain dashed-border card
  (no score inputs, no lineup pickers — it's a preview, not a real tie) reading "Based on the current
  standings, with N group match(es) still pending. This is a preview, not the final bracket - it can
  still change..." with each projected pairing (or bye) listed below, shown in `renderDraftScheduleView`
  only while no real knockout exists yet. New `/tmp/test_projected_knockout.py` (8 checks: appears with
  the correct pending-tie count, correctly seeds the actual top-2-by-standings pairing from a synthetic
  4-squad partial group stage, remaining squads correctly paired, projected ties never marked
  decided/no real `knockout` key created, disappears once every tie is decided, withheld for real
  separate groups, an odd squad count still projects the right bye). New
  `/tmp/test_projected_knockout_ui.js` (Playwright: card renders with heading/pending-count/squad names,
  contains no scoring controls, and correctly disappears once the real knockout exists even with stale
  `projected_knockout` still present in the payload). Full existing backend (28 files) and Playwright
  (12 files, including the two new ones) suites re-verified passing throughout.

- ✅ 2026-08-22 (v1.58.0) — **Manual-mode tournaments: configurable best-of-3, a real visual
  bracket, and a smoother multi-game entry flow.** Owner follow-up on v1.57.0 ("the tournament format
  is not supporting this bracket as well" / "make the chnage where you said each semifinal section will
  have 3 matches and how you can make it smooth as well"). Two previously-unsupported asks, both scoped
  down first via clarifying questions (best-of-3 configurable **per tournament**, not forced on
  everyone; the group stage stays best-of-1 for the current live event, only the knockout needs
  best-of-3 right now; build a real SVG bracket, not just lean on the flat tie list).
  (1) *Configurable best-of-3*: new `manual_draft.group_best_of` / `manual_draft.knockout_best_of`
  fields (both default `1`, must be `1` or `3`), validated and stored by
  `create_manual_draft_tournament`. `_score_tie_match` now picks `best_of` per-stage off its existing
  `stage_label` parameter (`'group'` vs `'knockout'`/`'third_place'`) instead of reading a flat
  `item.get('best_of', 1)` field that manual-draft items never actually had (silently always `1`
  before). `_submit_game`'s `needed_wins = (best_of // 2) + 1` engine needed **no changes** - it already
  generically supported best-of-N, shared with legacy tournaments. New "Games per group match" / "Games
  per knockout match" selectors on the creation form, wired into `submitManualDraftCreation`.
  (2) *Visual bracket*: `renderTournament` early-returned to `renderManualDraftTournament` before ever
  calling the legacy `renderBracketView`, and `renderManualDraftTournament` unconditionally hid
  `#bracket-svg` - so a manual-draft tournament had no bracket at all, just the flat tie list, even
  though the shared "View: Table/Bracket" dropdown was visibly sitting there doing nothing. New
  `renderDraftBracketView(t)` adapts the legacy SVG renderer for tie-shaped rounds (`squad_a`/`squad_b`
  instead of `player_a`/`player_b`, `draftSquadName(t, squadId)` for names, `wins_a`/`wins_b` instead of
  a per-game point total, `winner_squad_id`/`decided` instead of `winner_id`/`played`) - coordinate math,
  TBD-placeholder-round filling, and the third-place box are unchanged copies, since a tie carries the
  exact same `rounds`/`third_place_match` shape a legacy knockout does. The View toggle is now hidden
  and forced back to table mode during pools/auction/squad-review (nothing bracket-shaped exists yet),
  and shown + wired to `renderDraftBracketView` once a schedule exists (`group_stage`/`knockout`/
  `completed`). (3) *Smoother multi-game entry (real bug fix)*: `renderTieMatchRow`'s "lineup still
  pickable" path - the one actually used for ordinary squads-mode ties, as opposed to the cross-squad/
  already-decided "banner card" path - rendered a bare, contextless "Submit" button with no game number
  or running score whenever a match was mid-way through a best-of-3 series (`m.played` stays `false`
  until the decisive game, so a 1-0 match after game 1 looked identical to a fresh, unplayed one). Now
  shows the same "Games so far: ... (1-0) | Game 2:" progress line the banner-card path already had,
  in both plain-input and live-scoring modes. New `/tmp/test_best_of_config.py` (9 checks: field
  storage/validation, stage isolation - group stays best-of-1 even when knockout is best-of-3 on the
  same tournament, a 2-0 sweep deciding after 2 games not 3, a 2-1 result correctly needing all 3, the
  aggregate score fed to Elo/match-log being the sum across every game played not just the last one).
  New `/tmp/test_draft_bracket_best_of_ui.js` (Playwright: best-of selectors present and submitted,
  View toggle hidden pre-schedule and shown once a knockout exists, switching to bracket mode hides the
  table and shows both squad names on the SVG and back again, the games-so-far progress line and a live
  "Game 2" input both present for a mid-series best-of-3 match). Full existing backend (27 files) and
  Playwright (11 files, including the new one) suites re-verified passing throughout.

- ✅ 2026-08-22 (ops, no version tag) — **Aman→Guddu misattribution repair, on real production data
  (Rally Royale 22-23 Aug).** Standalone one-time repair scripts, not part of the deployed app zip (see
  `scripts/`, following the existing `add_third_place_match.py`/`repair_ratings_after.py` convention -
  read/write DynamoDB directly, run locally by the owner with AWS creds). `dump_tournament_for_review.py`
  (read-only diagnostic) confirmed the exact blast radius: one squad ("Smashers", leader Mirgank), one
  rep ("Aman & Mirgank"), and 3 already-played matches scored *after* the v1.57.0 substitution fix -
  proving the pre-fix bug's bad data had already leaked into real match history.
  `fix_misattributed_squad_player.py` relabels the squad/rep/match entities and repairs Elo, built around
  a baseline-vs-fixed **replay-diffing** technique (run the exact same full chronological replay twice,
  once unmodified and once with the fix applied, then diff the two) so genuine pre-existing Elo drift
  unrelated to this substitution - proven for real via the owner's own suggested no-op test (same
  `old_player_id`/`new_player_id`) - never gets silently "corrected" alongside it; writes are expressed
  as deltas on top of currently-stored ratings, never a raw replay overwrite. Caught and fixed two real
  bugs before any data was touched, both surfaced by the owner reading dry-run output carefully: (a) a
  double-independent-scan bug that made the dry-run preview inconsistent with what `--apply` would
  actually do; (b) a name-rebuild bug (`why is mirgank showing up like this?`) where a co-member's name
  in a multi-member entity fell back to their raw `player_id` because the rebuild only had the two
  substituted players' names cached - fixed by threading a live `players_table` lookup through
  `rebuild_entity` instead. `fix_entity_names.py` repairs the live data already corrupted by that second
  bug; its own dry-run output caught a third bug before it was applied - it was about to rename entire
  squads (destroying custom names like "Smashers") by treating squads the same as reps/match entities,
  which the real `substitute_squad_player`/`_rebuild_entity_after_substitution` backend code never does
  (a squad's `name` is a custom organizer-chosen display name, not auto-derived from members) - fixed by
  excluding squads from the script entirely. Owner ran the corrected scripts against production data and
  confirmed both fixed ("goiit fized now").

- ✅ 2026-08-22 (v1.57.0) — **Manual-mode tournaments: flexible game targets, winner-highlight fix,
  cross-squad substitution repair, collapsible groups.** Four owner reports from the same live event.
  (1) *Scoring flexibility*: "group stages are played with 21 and the knockout are played with 3 set
  of 15 or 11 but it is not guaranteed" - manual-draft ties were hardcoded to `target=21`,
  `best_of=1` (never read from the creation request, unlike legacy `create_tournament`), so any 11 or
  15-point finish needed a manual per-submission "override" confirmation. New
  `MANUAL_DRAFT_ACCEPTED_TARGETS = (11, 15, 21)` /  `_is_valid_manual_draft_game_score`, wired into
  `_score_tie_match` so a decisive 11, 15, or 21-point finish is now accepted directly, no override
  prompt, while non-standard scores still require the existing override safety net. **Not done**: true
  best-of-3 (multiple games per match) is still not supported for this format - `best_of` remains
  hardcoded to 1, so "3 sets" would need a separate follow-up if that's actually the ask for
  knockout. (2) *Winner-highlight bug*: "few matches where the losing team is highlighted as the
  winner" on the tie's banner card, though standings elsewhere were correct. `renderTieMatchRow`'s
  winner computation used a raw point-sum comparison (`totalA > totalB`) instead of the backend's
  authoritative `m.winner_id` - every other winner-highlight in the app (legacy fixtures) already
  keys off a game-count/`winner_id` field, never a point sum. Fixed to
  `m.winner_id === m.player_a.player_id`. (3) *Cross-squad substitution not propagating*: "Aman...was
  replaced by...guddu...but it showcases aman still and also aman's profile got the elo update."
  `substitute_squad_player` only ever touched `squad['members']`/`member_ratings` - for cross-squad
  tournaments the fixed-pairs `squad['pairs']` and the `item['reps']` map (both frozen snapshots taken
  at group-generation time) were never touched, and its pending-match side-detection compared
  `tie['squad_a']` (a rep_id in cross-squad mode) directly against `squad_id`, which can never match,
  so it silently no-op'd on every cross-squad tie. New `_rebuild_entity_after_substitution` repairs a
  rep or match-player entity's `members`/`member_ratings`/`name` in place; `substitute_squad_player`
  now also fixes `squad['pairs']` and every affected rep, and repairs (rather than blanking) every
  not-yet-played match's player snapshot for cross-squad ties. **Known limitation**: this fixes future
  substitutions and unplayed matches going forward only - a match already scored before the fix, whose
  Elo was credited to the wrong (substituted-out) player, is not auto-corrected; needs manual
  identification per-match if the owner wants it reversed. (4) *Collapsible groups*: "make the groups
  banner sections collapsible so that i can see which player is under which group and then expand...to
  start or enter the matches" - each group in `renderDraftScheduleView` is now a `<details class="card">`
  with the group name + member list always visible in the `<summary>` (readable while collapsed) and
  the standings table + tie matches in the collapsible body; open/closed state tracked in a
  module-level `draftOpenGroups` Set (native `<details open>` state resets on every `innerHTML`
  rebuild, so this survives poll-triggered and score-triggered re-renders). New/updated tests:
  `/tmp/test_cross_squad_groups.py` grew from 23 to 31 checks (scoring flexibility x4, substitution
  repair x4, including a real Elo-delta check confirming the *replacement* player is credited);
  `/tmp/test_squad_roster_editing.py` updated for the new `pending_slots_repaired` audit field;
  `/tmp/test_manual_draft_groups_ui.js` grew with 6 new checks covering collapsibility (one `<details>`
  per group, member list visible in `<summary>`, collapsed by default, standings nested in the body,
  toggling sets `open`, and open/closed state survives a `renderTournament` re-render). Full existing
  backend (24 files) and Playwright (10 files) suites re-verified passing throughout.

- ✅ 2026-08-21 (v1.56.0) — **Manual-mode tournaments: live point-by-point scoring for tie matches,
  photo/VS banner cards, and submit-path hardening.** Owner report during the live Rally Royale event:
  "Use live point-by-point scoring for tournament matches" (the +1 A/+1 B/Undo/Submit game/Split-screen
  controls) and the photo/VS banner card - both already existed for legacy knockout/groups_then_knockout
  tournaments - had never been wired up for manual-draft tie matches, so checking the box did nothing
  there, and a tie match rendered as a bare name/score-input row instead of the banner every other
  tournament view uses. `renderTieMatchRow`: once both sides of a match are known (always true
  immediately for a cross-squad tie, since reps are fixed before groups are generated; true for any
  match once it's actually `played`), it now renders the same `renderVsCard`/`vsSideIds` banner used
  everywhere else, with live point-by-point controls (`renderLiveScoreControls`, reusing the existing
  `tournamentLivePoint`/`tournamentUndoPoint`/`openTournamentSplitScreen` machinery) swapped in for the
  plain score inputs whenever "Use live point-by-point scoring" is checked - both paths post through the
  same new `submitDraftTieScoreDirect` (an explicit-score sibling of `submitDraftTieScore`, needed
  because the live flow already knows the tally and has no DOM inputs to read it back from). New
  `finishDraftTieLiveGame` mirrors `finishGroupLiveGame`/`finishKnockoutLiveGame`. A squads-mode tie
  still shows the editable lineup picker up until it's played (a leader can change their own pick right
  up until then - `pick_tie_player` only rejects once `match.played`), now with live-mode support added
  to its score-entry step too. Submit-path hardening, from the same report and a second one in the same
  breath ("make sure live scoring does not fail at submit... if there is a stale session it should
  refresh and then send the data... someone tried the live score but since they had a stale session at
  the submit they were not able to submit the score"): (1) `authedFetch` now wraps its underlying
  `fetch()` in try/catch - previously a thrown network error (offline moment, a phone waking from sleep
  with a dead socket) propagated as an uncaught rejection, silently killing whichever async flow called
  it, with no error shown and no retry; it now returns a `{ok:false,status:0}` stand-in response so
  every existing `if (!res.ok)` caller keeps working unmodified, with a clear, retry-safe error message.
  Its existing near-expiry-refresh-before-send and forced-refresh-and-retry-once-on-401/403 behavior
  (unchanged) is what actually recovers a stale Cognito session for `submitDraftTieScoreDirect` and
  every other authenticated draft route. (2) `submitGroupScoreDirect`/`submitKnockoutScoreDirect`/
  `submitThirdPlaceScoreDirect` (legacy, unauthenticated routes - a "stale session" can't apply to auth
  there, but a bare network-level `fetch()` failure could still throw uncaught) gained the same
  try/catch, and all four submit-direct functions (plus the new draft-tie one) now return true/false.
  (3) Every `finish*LiveGame` function (`finishGroupLiveGame`/`finishKnockoutLiveGame`/
  `finishThirdPlaceLiveGame`/`finishDraftTieLiveGame`) now only deletes the recorded point log AFTER a
  confirmed successful submit - previously it deleted first, so a failed submit for any reason silently
  discarded every point that had been tapped in, forcing a manual recount from memory; now a failed
  attempt leaves the tally in place and "Submit game" can simply be pressed again. (4) `schedulePollTick`
  (the live-score-polling fix shipped earlier today) gained an additional guard: it now also skips a
  tick whenever any match has unsaved live-scored points sitting in memory, not just when the viewer's
  DOM focus is inside the panel - the point-tracker display and the split-screen overlay (which lives
  outside `#tournament-detail` and so wasn't caught by the focus check alone) are never blown away by a
  poll-triggered re-render mid-game. New `/tmp/test_draft_live_scoring_ui.js` (8 checks: the banner card
  renders once both reps are known, live mode swaps in the point controls, recording interleaved points
  displays correctly, a failed submit sends the right score and does NOT discard the recorded points,
  and retrying resends the same points and succeeds) and an updated `/tmp/test_draft_schedule_ui.js`
  (the played-match assertion now checks the banner's split score badges instead of combined "21 - 15"
  text, matching the new rendering). Full existing backend (18 files) and Playwright (10 files) suites
  re-verified passing throughout.

- ✅ 2026-08-21 (v1.55.0) — **Manual-mode tournaments: live score polling for the schedule view.**
  Owner report during the live Rally Royale event: "the live score is not coming up for these
  matches." Root cause: the group_stage/knockout/completed view was only ever fetched once, at page
  load - a score entered on the organizer's own device (or any leader's) never reached anyone else's
  already-open tab (spectators watching on their phones, other leaders) without a manual reload; no
  caching layer was involved (ruled out explicitly - neither the Lambda responses, API Gateway, nor
  CloudFront cache tournament API data; CloudFront only fronts the static frontend bundle). New
  `startSchedulePolling`/`stopSchedulePolling`/`schedulePollTick` mirror the existing manual-draft
  auction poller (`startDraftPolling`) but for the schedule view: a 5s interval, started whenever
  `renderManualDraftTournament` renders `group_stage`/`knockout`/`completed`, stopped on every other
  status and when the Tournaments tab is no longer active (wired into `activateTab`) - paused
  automatically while the browser tab is hidden or backgrounded, and given an immediate catch-up tick
  on `visibilitychange` back to visible, same as the auction poller. Uses the existing public,
  redaction-safe `fetchTournamentDetail`, so it works for logged-out spectators too, not just the
  organizer/leaders. Two things kept it from being disruptive rather than helpful: a tick is skipped
  entirely while the viewer's focus is inside the tournament detail panel (mid-typing a score, or a
  dropdown open) so an in-progress input is never yanked away every 5s; and a tick that finds no actual
  change to `group_stage`/`knockout`/`status`/`champion_squad_id` skips the re-render, so an idle tab
  isn't silently re-painting (and losing scroll position) for nothing. New
  `/tmp/test_schedule_live_poll_ui.js` (8 checks: polling auto-starts on group_stage, a real change
  re-renders with the new score, a no-change tick renders zero times, a tick is skipped while an input
  inside the panel has focus and lands on the next tick once focus moves away, and polling stops on
  every non-schedule status and on leaving the Tournaments tab). Full existing backend (18 files) and
  Playwright (9 files) suites re-verified passing throughout.

- ✅ 2026-08-21 (v1.54.0) — **Manual-mode tournaments: cross-squad groups ("one rep from every
  squad in every group").** Same-day rush build (owner's live tournament, Rally Royale, started the
  next morning) after the owner clarified their original ask more precisely: not whole squads split
  across groups (v1.53.0's design), but each squad first fixing exactly `num_groups` doubles pairs (or
  solo reps, for singles) upfront, then one of those fixed pairs from EVERY squad landing in EVERY
  group - so a group is a cross-squad mini-pool, not 2+ whole squads facing off - with results rolling
  back up to an overall per-squad leaderboard. Confirmed via two rounds of AskUserQuestion before
  building (group makeup: whole squads vs one rep per squad; and what "set the pairs first" meant:
  fixing doubles partnerships upfront vs previewing/editing a random split) given how much this
  diverges from what v1.53.0 already shipped and tested - and a third question on timing, given the
  event was less than 24 hours out, which the owner answered by choosing to rush it in rather than run
  the event on the existing squads-mode groups or delay. New `manual_draft.group_mode` (`'squads'`
  default - every existing tournament unaffected; `'cross_squad'` opts in). New
  `POST .../set-squad-pairs` (organizer or that squad's own leader) - validates exactly `num_groups`
  pairs, each the right size for `match_type`, each player an actual squad member used at most once.
  New `_build_cross_squad_group_stage`: for each squad, shuffles which of its pre-fixed pairs lands in
  which group (so it's not always the same pair in Group A), builds a rep entity per pair
  (`{squad_id}::rep{n}`, carrying `parent_squad_id`), and - since a rep is already fully known the
  instant it's built, unlike the leader-nominates-per-match flow the regular squads mode uses - a new
  shared `_fill_cross_squad_match_players` helper pre-fills every match's players immediately, applied
  everywhere a cross-squad tie gets built (the group stage, the first knockout round, later knockout
  rounds, the third-place match) so score entry is available right away, no lineup step. New
  `_tie_side_leader_id` resolves a tie's squad_a/squad_b (a rep_id here) back to the real leader id, so
  `_authorize_tie_scorer` and the champion banner (`champion_squad_id`, plus a new `champion_rep_id`
  naming exactly which pair won it) still work per-squad without a leader needing to know their rep's
  synthetic id. `compute_squad_standings` now reads `item['squads']` merged with `item['reps']` (a
  no-op for every non-cross-squad tournament); new `compute_squad_standings_by_parent` rolls a squad's
  several reps back into one overall row - used for `get_tournament`'s `squad_standings` specifically
  when `group_stage.cross_squad` is set, while the per-group breakdown (`group_standings`) stays
  rep-level, since that's genuinely who plays within a group. `regenerate-schedule` (the repair action
  from the fix earlier today) also accepts `group_mode`, so the owner's actual in-progress tournament
  can switch from the already-generated squads-mode schedule into cross-squad mode in place, using the
  same locked squads, no re-auction - switching back to `'squads'` clears the now-stale `item['reps']`.
  Frontend: new "Pairing" panel (squads_locked and group_stage, organizer or that squad's own leader,
  shown whenever there's more than one group regardless of the tournament's current `group_mode` - since
  pairs must exist before a tournament can ever switch into `cross_squad`) with pair pickers per squad,
  labeled plainly "Pair 1:", "Pair 2:", etc - deliberately NOT "Group 1:"/"Group 2:", since which pair
  lands in which named group is decided randomly at generation time, not chosen here (an owner complaint,
  caught same-day: the first version of this panel implied the user picked the group, and a stale
  degenerate group_stage view - each of 4 named groups showing a single lone squad name with 0-0-0
  standings, left over from a pre-cross-squad `num_groups` misconfiguration - was rendering above it,
  which read as broken/hallucinated). A single "Generate groups (random, one pair from every squad in
  each)" button now lives directly in the Pairing panel and drives both first-time generation
  (`generate-schedule`) and in-place repair (`regenerate-schedule`) via one function
  (`generateCrossSquadGroups`), since `generate_schedule` now also accepts the same `group_mode`
  override `regenerate_schedule` already did. `renderDraftScheduleView` now detects a degenerate
  group_stage (`groups` present but zero total ties across all of them) and shows a plain "No
  group-stage matches yet - finish the setup below, then generate groups." message instead of the
  misleading per-group squad-name cards. A cross-squad tie's match rows always render plain read-only
  names (never a lineup picker, since there's nothing to nominate) and go straight to score entry;
  `draftSquadName` falls back to `item['reps']` and shows "`<parent squad>` - `<rep name>`" for a
  rep_id; the "Fix group-stage settings" repair panel gained a group-makeup selector defaulted to the
  tournament's current mode. New `/tmp/test_cross_squad_groups.py` (23 checks: pair-setting validation,
  one-rep-per-squad-per-group placement, pre-filled match players, scoring auth resolving rep→parent
  leader, a full group-stage-to-knockout playthrough, the parent-rolled-up vs rep-level standings split,
  champion resolution, the exact regenerate-schedule repair flow used to fix the live tournament, and
  `generate_schedule`'s own `group_mode` override) and `/tmp/test_cross_squad_ui.js` (16 checks,
  including the "Pair N:" vs "Group N:" labeling and the degenerate-view fallback message). Full
  existing backend (18 files) and Playwright (8 files) suites re-verified passing throughout, including
  after this same-day UX correction.

- ✅ 2026-08-21 (v1.53.0) — **Manual-mode tournaments: real separate groups for the group stage.**
  Owner asked for genuine World-Cup-style groups after describing a scenario ("4 groups... every team
  playing 3 matches... whoever comes on top proceeds to knockout") that, worked through, turned out to
  be ambiguous with the existing single-round-robin behavior — clarified via two follow-up questions:
  real separate groups (not the existing single round-robin), squads assigned randomly, and initially
  the top 2 per group move on to a combined knockout, later corrected (same day) to default to just the
  group winner - see below. New `manual_draft.num_groups` (default 1, so every existing manual-draft
  tournament is completely unaffected — `generate_schedule` only takes the new code path when
  `num_groups > 1`) and `manual_draft.advance_per_group` (default 1), both settable at creation. When grouped,
  `generate_schedule` randomly splits squads into `num_groups` named groups (A, B, C... via the same
  `ascii_uppercase` slicing the legacy `groups_then_knockout` format already uses) as evenly as
  possible, and builds a round-robin only within each group — every tie is tagged with its `group`.
  Rather than inventing a new mechanism, the two hardest parts — a genuine boundary tie needing a
  tiebreaker instead of a guess, and de-clustering the knockout draw so two squads from the same group
  don't rematch in round one where avoidable — directly mirror the *existing* legacy
  `groups_then_knockout` format's own `inject_tiebreakers_if_needed`/`advance_to_knockout` design
  (found already solving this exact problem for the old per-player group format): new
  `_inject_group_tiebreakers_if_needed` appends an extra tie between exactly two squads level on both
  `ties_won` and `point_diff` at their group's `advance_per_group` boundary; `record_group_tie_score`'s
  "all decided" hook now branches on whether `group_stage.groups` is set, calling this new pair of
  functions instead of the old single-standings `_generate_knockout_from_group_stage` when it is.
  `compute_squad_standings(item, squad_ids=None)` grew an optional filter (every existing tie-walking
  line already guarded with `if a in stats`, so scoping it to one group's squads needed no other
  change) and now backs both the overall table and a new per-group breakdown. `get_tournament` attaches
  `group_standings: {name: [...]}` alongside the existing `squad_standings` for a grouped tournament.
  Frontend: the manual-draft creation form gained "Number of groups"/"Squads advancing per group"
  fields (with tooltips); the schedule view renders one labeled "Group A"/"Group B"/... section per
  group (its own standings table + only its own ties) instead of one flat list when
  `group_stage.groups` is present, and a tiebreaker tie is visibly labeled as such. Also clarified the
  "Group-stage matches per tie" tooltip and changed its default from 2 to 1, after the owner described
  wanting exactly one decisive game per opponent ("a pair... supposed to play 3 games inside the group
  stage, not 3 times with each opposition") rather than the previous default of 2 games against the
  SAME opponent per tie. New `/tmp/test_manual_draft_groups.py` (12 checks: config validation, the
  random group split, that num_groups=1 stays byte-identical to the pre-existing flat shape, only the
  top-N-per-group advancing, the same-group-rematch avoidance in round one, per-group standings via
  `get_tournament`, and — the trickiest case — a contrived 3-way level tie at a group's boundary
  provably injecting a tiebreaker tie rather than silently guessing) and `/tmp/test_manual_draft_groups_ui.js`
  (4 checks: labeled group sections, one standings table per group not one combined table, the
  tiebreaker label, and that an ungrouped tournament's rendering is completely unaffected). Full
  existing backend (17 files) and Playwright (4 files) suites re-verified passing throughout.

  **Two same-day follow-up fixes, folded into this same version rather than tagged separately since
  none of it had been deployed yet:** (1) `advance_per_group`'s default changed from 2 to 1 -
  owner picked "top 2" during design, then realized after the fact they only wanted the single group
  winner to advance, and the field defaulting to 2 in the creation form made it easy to leave there by
  accident (pure default-value change - `create_manual_draft_tournament`'s fallback and the creation
  form's `#draft_advance_per_group` default; the field was always fully configurable per-tournament, so
  every other code path needed no change). (2) Two real bugs found from actually using this on a live
  tournament (owner's "Rally Royale", 4 squads, `num_groups=4`): first, the organizer-nominates-a-tie's-
  lineup feature (v1.52.0, below) was backend-only - `pick_tie_player` already accepted an organizer's
  `squad_id` to disambiguate which squad they're picking for, but `renderTieMatchRow`'s picker only ever
  rendered for a tie's own two leaders, so an organizer viewing a tie had no on-screen control to use it
  at all; fixed so a squad's own leader still gets the same editable picker as before, the *other*
  squad's leader still just sees read-only text (editing an opposing squad's lineup was never coherent),
  and anyone else (organizer or a spectator) now also gets an editable picker with `squad_id` sent
  explicitly so the organizer's pick is disambiguated server-side - matches this app's existing "render
  for everyone, let the server 403 a non-organizer" convention used for every other organizer-only
  control. Second, `num_groups` validation only rejected values greater than the squad count, not values
  that leave a group with just 1 squad - 4 squads split into `num_groups=4` silently produced 4 named
  groups of 1 squad each, so 0 ties, 0 matches, nothing playable, no error at creation or schedule-
  generation time; `generate_schedule`'s schedule-building logic was extracted into a shared
  `_build_group_stage(item)` (used by both `generate_schedule` and the new endpoint below) which now
  also rejects any `num_groups` where `len(squad_ids) < num_groups * 2`, naming the largest valid
  `num_groups` for that squad count in the error. Because the owner's actual tournament was already
  stuck in that degenerate state with real locked squads, also added
  `POST /tournament-draft/{id}/regenerate-schedule` - organizer-only, only while still `group_stage` and
  genuinely nothing in it has been played yet (checked directly, so a real result is never silently
  discarded); optionally updates `manual_draft.num_groups`/`advance_per_group` from the request body,
  then reruns `_build_group_stage` to rebuild the schedule from the tournament's existing (untouched)
  squads - no need to redo the whole leader/pool/auction process over a group-count mistake. New "Fix
  group-stage settings" panel on the frontend's group-stage view (organizer-only, server-enforced)
  offers this inline. `/tmp/test_manual_draft_groups.py` grew from 12 to 18 checks covering both fixes;
  `/tmp/test_draft_schedule_ui.js` gained an organizer-viewpoint check (sees editable pickers on both
  sides of a tie, with `squad_id` included) and a regenerate-panel-presence check.

- ✅ 2026-08-21 (v1.52.0) — **Manual-mode tournaments: organizer can also set a tie's lineup, squad
  renaming, and (i) tooltips on the creation form.** Three owner requests handled together.
  **(1) Organizer/admin can also set a tie's pairing/lineup.** `pick_tie_player` was deliberately
  leader-only from Phase C ("only this tie's own leader can nominate, by design") — the owner asked
  for the organizer to also be able to do it, in consultation with the leader (same "the app
  shouldn't be a hard stop if not everyone has it open" reasoning as organizer-assign during the
  auction). A leader still nominates for their own squad exactly as before, no request-body change.
  The organizer has no "own squad" to infer a side from, so they now also send `squad_id` (must match
  one of the tie's two squads) to say which side's lineup they're setting; omitting it as organizer
  returns a 400 asking for it, not a 403. Updated `/tmp/test_draft_auth_matrix.py`'s check #7 (used to
  assert organizer/SuperAdmin were flatly rejected here - now asserts the disambiguation behavior
  instead) plus its doc comment; the rest of the auth matrix (17 other checks) still passes unchanged.
  **(2) Squad renaming.** Squads got an auto-generated `"Team <leader>"` name the instant the auction
  auto-froze, with no way to change it. New `POST /tournament-draft/{id}/rename-squad` (organizer OR
  that squad's own leader; available from `squads_locked` onward through `completed`, since a name is
  cosmetic and there's no reason to lock it once the schedule starts) validates a non-empty name up to
  60 characters. Frontend: a "Rename" button per squad inside the existing `renderSquadRosterEditPanel`
  (renamed conceptually from a roster-editing-only panel to squads' general "Edit squads" panel),
  using `nwPrompt` - it now always renders once squads exist, independent of whether there's anyone
  eligible to move/substitute below it (previously the whole panel silently didn't render at all if
  `pickedOptions` was empty, which would have hidden renaming too for a squad with only its leader and
  no other picks).
  **(3) (i) tooltips on the manual-draft creation form** (owner: "even i'm getting confused what each
  is... make it understandable"). New `.info-tip` CSS component (a small bordered circle, hover or
  tap/focus to reveal a popover - `tabindex="0"` so it works on touch, not just desktop hover) added
  next to every manual-draft field: budget, number of pools, picks per pool, group-stage matches per
  tie, knockout matches per tie, and match type - each rewritten in plain language explaining what it
  actually does and why (e.g. "Group-stage matches per tie" now explains what a "tie" even is: the
  whole squad-vs-squad matchup, decided over N individual games, tiebroken by total points if split).
  New `/tmp/test_squad_editing_ui.js` checks (rename buttons scoped to `#tournament-detail` so they
  don't collide with the pre-existing unrelated admin "Rename [a player]" control elsewhere on the
  page; verifies the rename control submits the right `squad_id`/`name`, and that it still appears
  during `group_stage` alongside the substitute-only panel). Full existing backend (14 files) and
  Playwright (3 files) suites re-verified passing.
  **Left open, not built:** the owner's message also described a "4 groups... every team playing 3
  matches... whoever comes on top proceeds to knockout" scenario that, worked through with 4 squads,
  is actually consistent with the *existing* single-round-robin group stage (3 ties per squad,
  knockout seeded by standings) - so it's unclear whether this is a new "real sub-groups, top-N
  advance" ask or just the owner confirming their understanding of what's already built. Flagged back
  to the owner rather than guessed at, since building the wrong one would be substantial wasted work.

- ✅ 2026-08-21 (v1.51.0) — **Manual-mode tournaments: squad roster editing/substitution/doubles
  pairing ("not a hard stop"), plus a real fix for a non-playing organizer getting stuck unable to
  lock pools.** Two owner requests handled together.
  **(1) The "not a hard stop" ask** (owner, after hitting the Decimal crash live: "user should be able
  to set their teams as well as in pairing as well not a hard stop at it" — clarified via multi-select
  to mean all three: post-auction roster editing, doubles pairing, real substitution).
  `move_squad_player` (new route `POST /tournament-draft/{id}/move-squad-player`, organizer-only,
  `squads_locked` only) moves a picked (non-leader) squad member to a different squad — no budget
  bookkeeping needed since the auction is already over, just a plain roster edit; restricted to before
  the schedule exists because once ties reference `squad_a`/`squad_b` by id, moving a player to a
  different squad mid-tournament would change who's on which side of an already-scheduled fixture,
  which isn't coherent. `substitute_squad_player` (new route `POST
  /tournament-draft/{id}/substitute-squad-player`, organizer-only, `squads_locked` through `knockout`)
  is real substitution: swaps a current squad member out for a brand-new replacement who isn't already
  on any squad, rejects substituting the leader themselves, and — the one non-obvious part — walks
  every group-stage/knockout tie this squad appears in and clears any not-yet-played match slot where
  the outgoing player was already (but not yet) nominated, back to `None`, so a departed player can't
  still take the court; an already-*played* match's recorded player snapshot is left untouched,
  mirroring the legacy `substitute_player`'s own "don't touch history" behavior. Logs each swap to a
  new `item['squad_substitutions']` audit trail. Doubles pairing needed a new
  `manual_draft.match_type` field ('singles'/'doubles', settable at tournament creation, defaults to
  'singles' so every existing tournament is unaffected): `pick_tie_player` now accepts either a single
  `player_id` (singles) or a `player_ids` pair (doubles) matching the tournament's configured type,
  validates both nominees are on the caller's own squad and distinct, and builds a synthetic pair
  entity (`{player_id: <fresh uuid>, name: "X & Y", members: [p1, p2], member_ratings: [...]}`) — the
  same shape the legacy manual-teams doubles path already produces. Two follow-on fixes were required
  for the pair entity to work correctly rather than just render: `_score_tie_match` was hardcoding
  `update_elo_and_log('singles', ...)` regardless of the tournament's actual match type, which would
  have silently used the wrong K-factor/pairing-count Elo path for every doubles match — now reads
  `manual_draft.match_type` and passes it through. `compute_player_tournament_scores`'s `apply_match`
  assumed `entity['player_id']` was always a real player (true for singles, but for a doubles pair
  that id is the pair's own synthetic uuid, not a person) — generalized to credit every id in
  `entity['members']` individually, with a small name-splitting helper so each player gets their own
  name in the tournament leaderboard rather than the pair's combined "X & Y" label. Frontend: a new
  "Edit squads" panel (`renderSquadRosterEditPanel`) offers the move control at `squads_locked` and a
  substitute-only control (moving between squads is dropped once a schedule exists) through
  `group_stage`/`knockout` — both organizer-only routes render unconditionally for any viewer and rely
  on the existing server-403 convention this app already uses for "Lock pools"/"Generate schedule".
  `draftPlayerPickerHtml` now branches on `match_type`: doubles renders two `<select>`s plus an
  explicit "Set pair" button (deliberately not firing on every `onchange`, which would submit a
  lopsided pair) via a new `pickTiePlayerPair`. The tournament-creation form's existing (previously
  manual-draft-hidden) `#tournament_match_type` field is now shown and wired through for manual-draft
  too. New `/tmp/test_squad_roster_editing.py` (12 checks: move/substitute auth and phase gating, the
  pending-slot-clearing behavior, and that an already-played match is never touched by a later
  substitution), `/tmp/test_doubles_tie_pairing.py` (8 checks: pair validation, the correct entity
  shape, the doubles Elo path actually running, and per-member leaderboard credit), and a new
  `/tmp/test_squad_editing_ui.js` Playwright pass (panel rendering, leaders excluded from the
  move/substitute lists, the move-only-pre-schedule / substitute-anytime split) — plus the full
  existing backend and Playwright suites re-verified passing.
  **(2) Roster-exclusion gap for a non-playing organizer** (owner-reported live bug: "it does not give
  option to select who all are playing for that selected group... i'm not playing due to health
  reason but i needed to be owner... tried to proceed to auction but said all players must be
  segregated in pool"). Root cause: `create_manual_draft_tournament` dumps every group member into
  `pools.unassigned` at creation with no way to take anyone back out, and `lock_pools` refuses to
  proceed until that tray is empty — an organizer who is the group owner/admin but isn't personally
  playing had no way to excuse themselves (or anyone else not participating) from that required
  roster, and would get permanently stuck. Fixed with a new `POST
  /tournament-draft/{id}/remove-player` route (organizer-only, `pools_open` only, rejects removing a
  player who is currently a leader with a clear error rather than silently cascading) that strips a
  player out of the unassigned tray or whatever pool they're in — they're simply never required to
  reappear anywhere. Frontend: a small "×" button on every pool-board chip while pools are open, with
  a confirm dialog. New `/tmp/test_remove_draft_player.py` (7 checks, including the exact end-to-end
  reproduction: owner removes themselves, assigns everyone else, and `lock_pools` now succeeds) and a
  Playwright check in the new `/tmp/test_squad_editing_ui.js` (the "×" renders, calls the route with
  the right `player_id`, and the board re-renders without the removed player).

- ✅ 2026-08-21 (v1.50.0) — **Manual-mode tournaments: pool/auction privacy, a real production crash
  fix, and an organizer-assign convenience fix.** Three owner requests handled together in one pass.
  **(1) Pool/auction privacy** (owner: "make that the pool and the auction amount is visible only to
  the owner, admin and leaders... should be hidden post it passes that phase"). The gap: `GET
  /tournaments/{id}` is unauthenticated - literally anyone browsing tournaments (including guests) could
  see pool assignments and every leader's remaining budget/bid history for a manual-draft tournament,
  live or historical, forever. Fixed with a new `_redact_pool_auction_detail(item)` that strips `pools`
  down to `{locked, redacted:true}` and `draft` down to `{status, redacted:true}` on that public route,
  unconditionally, for every manual-draft tournament at every status - and a new Cognito-gated `GET
  /tournament-draft/{id}` (`get_draft_sensitive_detail`) that's the only route that ever returns the real
  thing, gated by a new `_authorize_pool_auction_viewer`: organizer (SuperAdmin/owner/admin) always, a
  leader ONLY while their phase (`pools_open`/`pools_locked`/`auction`) is still live - once it passes
  (`squads_locked` onward), a leader who isn't also the organizer loses access too, exactly matching the
  owner's explicit choice when asked. `get_draft_state` (the auction polling endpoint) was refactored
  onto this same shared check instead of its old ad-hoc one. Also closed a subtler leak: `pick_tie_player`
  /`record_group_tie_score`/`record_knockout_tie_score` are reachable by a tie's own leader even after
  their pool/auction access has expired (they're inherently post-phase-only routes) - their *response
  bodies* carried the real `pools`/`draft` back to that leader even though no UI reads it from there, so
  a browser network tab could still leak it. Fixed with `_hide_pool_auction_from_non_organizer`, applied
  to all three response bodies. Caught one real bug while building this: `_redact_pool_auction_detail`
  originally mutated the `item` dict it was handed in place - harmless against real DynamoDB (a fresh
  deserialized object every call) but a landmine if anything ever changed that assumption, and it
  immediately corrupted the FakeTable test harness's stored record the moment a test called the public
  route before the privileged one. Fixed to return a new dict instead of mutating, verified by the same
  test that caught it. New `/tmp/test_pool_auction_privacy.py` (17 checks: redaction on the public route
  during every phase, the privileged route's full organizer/leader/phase authorization matrix, the
  three-action response-body leak fix, and confirming a group admin - not just the owner - retains
  historical access same as the owner does). Frontend: `renderManualDraftTournament` now checks
  `t.pools.redacted`/`t.draft.redacted` (the signal a viewer isn't currently entitled, not an error) and
  renders a plain status message ("Pools are being organized" / "The auction is underway...") instead of
  the real pool board/auction room when redacted - also skips starting the auction poll loop in that
  case, so a non-privileged viewer's browser doesn't hammer a route it will just get 403'd from. New
  `fetchTournamentDetail(tournamentId)` wraps the public read and, when logged in, additionally tries the
  privileged endpoint and merges its real `pools`/`draft` in on success, silently keeping the redacted
  stub on a 403 (the expected, normal outcome for most viewers) - both call sites that load a tournament
  from a bare fetch (the "Load tournament" button and the schedule-view background refresh) now go
  through it. Verified with a new `/tmp/test_pool_auction_privacy_ui.js` Playwright pass (redacted vs.
  real board/room rendering, no polling started when redacted) plus the full existing Phase A/B/C/
  organizer-assign Playwright suites (all still passing).
  **(2) Decimal-from-DynamoDB crash on "Generate schedule"** - a real bug the owner hit live and
  reported with a screenshot: `'decimal.Decimal' object cannot be interpreted as an integer`. Root cause:
  DynamoDB always round-trips stored numbers as `decimal.Decimal` on read, even though
  `create_manual_draft_tournament` writes plain Python `int`s - `generate_schedule` read
  `group_matches_per_tie` fresh from the stored item and passed it straight into `build_tie`'s
  `range(matches_per_tie)`, which only accepts a real `int`. This was invisible to every existing test
  because the hand-rolled `FakeTable` test harness never round-trips through Decimal - a new
  `/tmp/test_decimal_matches_per_tie.py` reproduces it by hand-storing `decimal.Decimal` config values
  (what a real DynamoDB read actually returns) and confirms the exact same error message before
  reproducing the fix. Fixed with an explicit `int(...)` cast at every read site
  (`generate_schedule`, `_generate_knockout_from_group_stage`, `_advance_knockout_ties_if_round_complete`)
  plus a defensive `int(matches_per_tie)` inside `build_tie` itself, so the crash can't resurface even if
  a future caller forgets to cast.
  **(3) Organizer-assign leader dropdown convenience fix** (owner: "why is that once a leader['s] entire
  slots are full they are still shown in the dropdown... they should get removed... for convenience").
  The leader `<select>` in the organizer-assign panel previously always listed every leader regardless of
  whether they had room left in the relevant pool, so picking one and submitting would just get rejected
  by the backend's own quota check. New `draftAssignEligibleLeaders(t, pool)`/`draftAssignLeaderOptionsHtml`
  filter the dropdown to leaders who still have room in the SELECTED player's pool (a leader who has
  filled every pool's quota is naturally excluded from every pool this way, no separate check needed);
  the player `<select>` now carries each option's pool as a `data-pool` attribute and a new
  `updateDraftAssignLeaderOptions()` re-filters the leader list live via `onchange` when the selected
  player changes pools. Verified by extending `/tmp/test_organizer_assign_ui.js` (now checks that a
  quota-full leader is excluded, and that switching the player selection live-updates who's offered).
  Files: `backend/lambdas/tournaments/index.py`, `frontend/js/app.js`, `docs/BACKLOG.md`,
  `docs/CODEBASE_MAP.md`. No `template.yaml` change needed - the new `GET /tournament-draft/{id}` route
  is covered by the same existing `/tournament-draft{proxy+}` ANY-method resource.
- ✅ 2026-08-21 (v1.49.0-phaseD) — **Manual-mode tournaments, Phase D: auth-hardening matrix +
  `substitute_player` fixes (Owner request, "proceed further") - the whole feature is now complete.**
  Last slice of the tournament feature from `cuddly-forging-ocean.md`'s plan: an authorization sweep
  across every route added in Phases A/B/C plus organizer-assign, and the `substitute_player` bugfix the
  plan flagged from the start.
  **Auth matrix** (`/tmp/test_draft_auth_matrix.py`, 17 checks, no application code changed by this part
  - it confirmed existing behavior and closed test-coverage gaps the per-phase suites had left):
  anonymous (no `claims` at all) is rejected across all 15 manual-draft routes spanning every phase, not
  just tournament creation - `handle_draft_route`'s single top-of-function check already covered this
  uniformly, now proven route-by-route rather than assumed. A group **`admin`** role (distinct from
  `owner`, never separately exercised before) can set leaders and lock pools, confirming
  `_authorize_tournament_organizer`'s role check truly isn't owner-only. **SuperAdmin** (a caller who
  isn't even a member of the tournament's group) is confirmed to bypass the organizer check on every
  organizer-only route across every phase: start-auction, open-lot, organizer-assign (including driving
  a full auction to `squads_locked` this way), generate-schedule. `get_draft_state` is confirmed to work
  for an organizer who is **not** also a leader (the existing Phase B test's "organizer" happened to
  always double as a leader, so that path was never actually isolated before). `pick_tie_player` is
  confirmed to have **no** organizer/SuperAdmin bypass at all - by design, only the tie's own leader can
  nominate their own squad's player, the one route in this whole tree that deliberately behaves
  differently from every other organizer-adjacent route. `group-tie-score`'s "organizer OR either tie
  leader" dual auth is exercised specifically via the organizer/SuperAdmin side, plus a plain member
  (neither) still gets 403.
  **`substitute_player` bugfix** (`backend/lambdas/tournaments/index.py`): two real issues, one already
  known from the plan and one found while investigating it. (1) `apply_substitution`'s name-rebuild only
  special-cased exactly 2 members - a 3+-member entity had its **entire** team name silently overwritten
  with just the incoming player's name, dropping every other member. Fixed by always rebuilding from
  every current member's current name via `' & '.join(...)`, matching the exact convention
  `create_tournament` already uses at team-creation time regardless of team size - the len==2 special
  case is gone, one code path now handles 1, 2, or N members correctly. (2) While tracing whether this
  was actually reachable, found that `substitute_player` was never guarded against `format='manual_draft'`
  tournaments at all - its loop walks `item['knockout']['rounds']` expecting each entry to be a legacy
  `{player_a, player_b}` fixture, but a manual-draft knockout's `rounds` entries are **ties**
  (`{squad_a, squad_b, matches: [...]}`), which don't have a `player_a` key at that level - so calling
  `POST /tournaments/{id}/substitute` on any manual-draft tournament that had reached the knockout stage
  raised a raw, unhandled `KeyError` (a 500, not a clean error). Fixed with an explicit early check that
  rejects manual-draft tournaments with a clear 400 ("player substitution is not supported for
  manual-draft squads yet") instead of crashing - squad substitution isn't designed or requested yet, so
  refusing honestly is the right scope, not a half-built workaround. Verified with a new
  `/tmp/test_substitute_squad.py` (5 checks: the existing 2-member case still works unchanged, a
  hand-built 3-member entity now rebuilds its full name correctly instead of getting clobbered, the new
  manual-draft 400 guard, and the existing 404 error paths for an unknown team/player still work) plus
  the manual-draft guard is exercised a second time from the auth-matrix test above. The `format`-based
  early return means the actual crash site's `player_a`/`player_b` shape mismatch is naturally never
  reached - no attempt was made to teach `apply_substitution` to understand ties, since that's real new
  feature work nobody has asked for, not this bugfix's scope.
  Full existing regression suite (Phase A/B/C backend `/tmp/test_manual_draft_*.py` +
  `/tmp/test_organizer_assign.py`, and Phase A/B/C/organizer-assign frontend Playwright suites) re-run
  and still passing - Phase D touched only `substitute_player`/`apply_substitution`, nothing else in the
  file or in `frontend/js/app.js` changed.
  Files: `backend/lambdas/tournaments/index.py`. No frontend change (no UI currently calls
  `/tournaments/{id}/substitute` for a manual-draft tournament, so there was nothing to wire up - the
  fix is purely defensive/correctness). No `template.yaml` change needed.
- ✅ 2026-08-21 (v1.48.0-organizer-assign) — **Manual-mode tournaments: organizer-assign, out-of-band
  bidding (Owner request, "i need somewhere where the organiser itself enters the bidding amount and
  assigns to the leader, in case not all of them opens or access the app").** Real-world gap in the
  Phase B auction: it assumed every leader is live in the app bidding for themselves, but at an actual
  club night some leaders won't have the app open, so someone still needs to be able to run their
  bidding for them. `organizer-assign` covers that - organizer picks a still-queued player, picks which
  leader gets them, types the winning amount, submits - one call, no lot needs to be opened first, no
  leader needs to be signed in at all. Works either as the *only* way an auction is run (organizer
  tracks a verbal/whiteboard auction and enters results as they go) or mixed with normal live bidding
  (some leaders bid for themselves in the app, others get entered manually by the organizer).
  **Backend** (`backend/lambdas/tournaments/index.py`): `organizer_assign(tournament_id, event, claims)`
  - organizer-only (`_authorize_tournament_organizer`), requires `status='auction'` +
  `draft.status='in_progress'`, and requires **no lot currently open** (rejects with a clear "close or
  skip it first" error otherwise, so it can never race a live bid on a different player). Body is
  `{player_id, leader_id, amount}`; validates the player is still queued and undecided, the leader
  exists, the amount doesn't exceed that leader's `remaining_budget`, and that leader's pool quota for
  that player's pool isn't already full - the exact same checks `submit_bid`/`close_lot` already apply,
  just organizer-supplied instead of leader-authenticated. On success it does exactly what `close_lot`
  does to award a lot (deduct budget, increment `pool_picks`, append to `squad_member_ids`), including
  the auto-freeze into `squads_locked` once every leader's every pool quota is met. That freeze logic was
  duplicated in `close_lot` before this change; extracted into a shared `_maybe_freeze_squads(item, draft)`
  helper so `close_lot` and `organizer_assign` can't drift out of sync. New `organizer-assign` branch
  added to `handle_draft_route`. Verified with a new `/tmp/test_organizer_assign.py` (17 checks: full
  authz matrix, every validation rule including budget/quota/unknown-leader/already-sold/lot-currently-
  open, a full auction driven end-to-end through `organizer-assign` alone with the same auto-freeze +
  squad-building assertions Phase B's own suite checks, and rejection once the auction has already
  completed) plus the existing regression suite (Phase A/B/C backend + frontend, all still passing).
  **Frontend**: `renderDraftOrganizerAssignPanel` - a new card in the auction room (only rendered while
  no lot is open and undecided players remain) with a player `<select>`, a leader `<select>`, an amount
  `<input>`, and an "Assign" button; `organizer-assign` submits and re-renders from the full tournament
  response exactly like `close_lot`/`skip_lot` already do. `updateDraftLiveStatus` (the ~1.75s poll tick)
  now also disables the assign panel's controls whenever a lot gets opened elsewhere, mirroring how it
  already disables the queue-picker's buttons. Verified with a new `/tmp/test_organizer_assign_ui.js`
  Playwright pass (panel renders with the right player/leader options, hidden while a lot is open,
  hidden once no undecided players remain, blocks submission with a missing amount without ever calling
  the API, and a full submit sends the exact `{player_id, leader_id, amount}` body and re-renders from
  the response) plus the existing Phase A/B/C Playwright suites (all still passing unmodified).
  Files: `backend/lambdas/tournaments/index.py`, `frontend/js/app.js`. No `template.yaml` change needed -
  same existing `/tournament-draft{proxy+}` API Gateway resource tree covers this new route too.
- ✅ 2026-08-21 (v1.47.0-phaseC) — **Manual-mode tournaments (leaders + pool draft/auction), Phase C:
  tie-based schedule generation + tie-card UI (Owner request, "work on the next phase as well").**
  Third slice of the tournament feature - turns `squads_locked` into a full squad-vs-squad round robin
  followed by an auto-seeded knockout, all the way to a champion. Phase D (auth-hardening pass + the
  `substitute_player` name-rebuild bugfix) is the only piece left.
  **Backend** (`backend/lambdas/tournaments/index.py`): a "tie" is the new container - two squads,
  `group_matches_per_tie` (or `knockout_matches_per_tie`) individual match slots, running `wins_a/b` +
  `point_diff_a/b`, and a `decided`/`winner_squad_id` outcome. Deliberately reuses the *exact* fixture
  shape (`player_a`/`player_b`/`games`/`games_won_a`/`games_won_b`/`played`/`winner_id`) every other
  match in this file already uses for each individual match inside a tie, so `_submit_game` and
  `update_elo_and_log` needed **zero changes** - Elo is completely untouched, still updates globally per
  individual match exactly as today; only the tie CONTAINER around those matches is new.
  `POST .../generate-schedule` (organizer, requires `squads_locked`, rejects <2 squads) builds a full
  round robin of ties via `build_tie_round_robin`. `POST .../pick-tie-player` lets **only that tie's own
  two squad leaders** (never the organizer - owner-confirmed) nominate which of their own squad's
  members plays a given match slot; rejects nominating an opposing squad's player or changing an
  already-played match's lineup. `POST .../group-tie-score` / `.../knockout-tie-score` (organizer or
  either of the tie's own two leaders - new `_authorize_tie_scorer`) submit one match's score via
  `_score_tie_match`, which rejects scoring before both sides have nominated, then re-derives the tie's
  `wins_a/b`/`point_diff_a/b` and decides it once every match slot is played
  (`_update_tie_progress`): match-wins first, aggregate point differential (cricket-NRR style) as the
  tiebreak - a genuine deadlock on both is left `decided: False` for the organizer to resolve manually,
  same philosophy as an unsold Phase-B auction player, never guessed at silently. `tie_id` is a UUID
  unique across the whole tournament, so `_find_tie` locates a tie in group stage, any knockout round,
  or the third-place match without the caller needing to say which. Once every group-stage tie is
  decided, `_generate_knockout_from_group_stage` auto-seeds a knockout tie-bracket from
  `compute_squad_standings` (ties_won desc, point_diff desc) via `build_knockout_tie_round` (byes
  generalized the same power-of-2 way `build_knockout_round` already handles them - `_bye_tie` mirrors
  `_bye_match`). `_advance_knockout_ties_if_round_complete` mirrors the existing single-match knockout's
  round-advance + third-place-auto-creation logic, but for ties: a 1-tie round sets `status: 'completed'`
  + `champion_squad_id`; a 2-tie round also spins up the third-place match (kept playable even after
  `status` flips to `completed`, matching the legacy knockout route's own status check, so the final can
  finish before the third-place tie is played). `compute_player_tournament_scores` (new) is the
  tournament-scoped, **non-Elo** per-player leaderboard asked for alongside this feature - a read-time-
  only aggregation over every individual tie-match, exactly like `compute_all_standings`/
  `compute_squad_standings`, never persisted so it can't drift stale. `get_tournament` now attaches
  `squad_standings` + `player_tournament_stats` for a manual-draft tournament once a schedule exists -
  same "computed fresh on every read, never written to the item" convention the legacy `standings`
  field already uses. Verified with `/tmp/test_manual_draft_schedule.py` (24 checks: schedule generation
  incl. the <2-squads rejection, the full lineup-nomination authz matrix, scoring authz + validation
  incl. rejecting a score before both sides nominate and re-scoring an already-decided match, a genuine
  point-diff-tiebreak tie (1-1 on wins, decided by aggregate point differential) with real Elo deltas and
  a real match-log entry confirming the shared pipeline actually ran, full knockout progression through
  a 2-tie semifinal round -> third-place auto-creation -> final -> champion_squad_id, a dedicated 3-squad
  case exercising the bye path, and `get_tournament`'s standings-attachment behavior) plus the existing
  regression suite (all still passing, including Phase A/B's own suites).
  **Frontend**: `renderDraftSquadsReview` (the `squads_locked` view) gained a "Generate schedule"
  button. New `group_stage`/`knockout`/`completed` branches in `renderManualDraftTournament` dispatch to
  `renderDraftScheduleView`, which renders (in order) a champion banner once `completed`, a squad-
  standings table, a tie section per stage (`renderTieSection` -> `renderTieCard` -> one
  `renderTieMatchRow` per match slot - a bye tie renders as a one-liner, no match rows), and a player-
  leaderboard table. Each match row shows a `<select>` lineup picker (`draftPlayerPickerHtml`) **only**
  to the viewer if they lead one of this tie's two squads, and **only** offers that leader's own squad's
  members - the other side (and everyone else) sees plain text or "TBD". Score inputs only appear once
  both sides have nominated a player for that slot; submitting reuses the existing "offer to override an
  invalid game score" confirm-retry pattern already used by the legacy knockout/group score submitters.
  `squad_standings`/`player_tournament_stats` only ever arrive via the plain, unauthenticated `GET
  /tournaments/{id}` read (never on a write response, matching the backend's "never persisted" design),
  so the very first paint of a schedule view is missing them until a new `fetchAndRenderTournamentDetail`
  background fetch lands and re-renders - guarded against a stale response landing after the viewer has
  already navigated elsewhere. Verified with a new Playwright pass against the real served `frontend/`
  tree: the Generate-schedule button, tie cards rendering both squad names, the lineup picker correctly
  scoped to "my own squad only", a full pick -> both-nominated -> score-submit flow via stubbed
  `authedFetch`, the bye-tie one-liner, the squad-standings table, and the champion banner + player
  leaderboard on a completed tournament. The existing Phase A (`/tmp/test_draft_ui.js`) and Phase B
  (`/tmp/test_draft_auction_ui.js`) Playwright suites still pass unmodified.
  Files: `backend/lambdas/tournaments/index.py`, `frontend/js/app.js`. No `template.yaml` change needed -
  the existing `/tournament-draft{proxy+}` API Gateway resource tree already covers these new routes.
- ✅ 2026-08-21 (v1.46.0-phaseB) — **Manual-mode tournaments (leaders + pool draft/auction), Phase B:
  the auction engine + auction room/bidding UI (Owner request, "please go ahead").** Second slice of
  the big tournament feature - turns a `pools_locked` manual-draft tournament into `squads_locked` via
  an organizer-paced, leader-bid point-budget auction. Phase C (tie-based schedule generation) and
  Phase D (auth-hardening pass + the `substitute_player` name-rebuild bugfix) are still to come.
  **Backend** (`backend/lambdas/tournaments/index.py`): six new routes under `/tournament-draft{proxy+}`
  - `POST .../start-auction` (organizer; requires `pools_locked`; builds the pool-ordered nomination
  `queue` with leaders excluded since they're pre-owned, and seeds each leader's `pool_picks` with
  their own pool already at 1 so the ordinary quota check works unmodified everywhere else, no special-
  casing needed for "my own pool"), `POST .../open-lot` (organizer; rejects a second lot while one's
  already open, rejects an already-decided player), `POST .../bid` (**leader-only**, new
  `_authorize_leader()` helper matching a leader's `custom:player_id` against `item['leaders']`; the
  only route in this Lambda that uses an atomic conditional `update_item` instead of the file's usual
  read-modify-write `put_item` - `ConditionExpression='draft.current_lot.player_id=:pid AND
  draft.current_lot.high_bid<:nb'`, `list_append` for `bid_history`; a losing race gets HTTP 409 via
  `ConditionalCheckFailedException`, not a silently-overwritten bid - no new DynamoDB table needed
  since at most one lot is ever open at a time, organizer-serialized), `POST .../close-lot` (organizer;
  awards to the high bidder, deducts budget, increments `pool_picks`; once **every** leader's **every**
  pool quota is met, auto-freezes: builds the `squads` dict from each leader's accumulated
  `squad_member_ids`, `draft.status -> 'completed'`, tournament `status -> 'squads_locked'` - a leftover
  un-opened/unsold queue player does NOT block this, only quota completion matters), `POST .../skip-lot`
  (organizer; rejects a lot that already has a bid - must `close-lot` instead), `GET .../state` (leader
  or organizer; deliberately small polling payload - `current_lot`, each leader's `remaining_budget`/
  `pool_picks`, and just counts for `queue_length`/`decided_count`/`unsold_count` - not the full item,
  since leaders' clients hit this every ~1.75s). Design deviation from the original plan doc: dropped
  the persistent `queue.queue_index` field in favor of a dynamically-computed `_draft_decided_ids(draft)`
  helper (union of every leader's `squad_member_ids` minus the leader themself, plus `unsold`) - simpler
  and can't drift out of sync with the actual awarded/skipped state. Verified with
  `/tmp/test_manual_draft_auction.py` (31 checks: queue/budget/pool_picks seeding, full authz matrix,
  bid validation incl. budget/quota/tie/stale-bid rejection, a **genuine** atomic-conditional-update
  collision forced via a monkeypatched `FakeTable.update_item` - not just the ordinary pre-validation
  stale-bid path, which is a different code path entirely - confirming the real race-safety mechanism
  returns 409 instead of silently overwriting, close/skip-lot behavior, the auto-freeze-into-
  `squads_locked` transition with correct squad membership, and that a leftover queued/unsold player
  doesn't block completion) plus the existing regression suite (all still passing).
  **Frontend**: `renderManualDraftTournament()` gained `auction` and `squads_locked` branches. Auction
  room (`renderDraftAuctionRoom`): a queue picker grouped by pool (organizer-only in intent, shown to
  everyone per this feature's established "show to all, let the server 403" convention - same as
  Phase A's pool board), a live current-lot/leaders-status panel (`#draft-live-status`), and - only for
  a logged-in leader - a bid box with quick +10/+20/+50 buttons that read the live high bid off a
  `data-high-bid` attribute rather than a stale closure value. Polling (`startDraftPolling`/
  `stopDraftPolling`/`pollDraftStateTick`, ~1.75s): starts when the auction view renders, stops when
  leaving the Tournaments tab (`activateTab`) or the tournament leaves `auction` status, no-ops while
  `document.visibilityState !== 'visible'` and fires an immediate extra refresh on the existing
  `visibilitychange` listener the moment the tab wakes back up. Each poll tick (and a successful bid
  response) only replaces the `#draft-live-status` subtree via `outerHTML` - the bid input box is a
  **separate, never-touched container**, so an in-progress bid keystroke is never lost; verified in
  Playwright by tagging the actual input DOM node before a live-status update and confirming it's the
  *same* node afterward (not just a coincidentally-matching value). `close-lot`/`skip-lot`/`open-lot`
  and the "Start auction" button all trigger a full `renderTournament(data)` refresh (consistent with
  every other write in this app), which also cleanly restarts or permanently stops polling depending on
  the new status. `squads_locked` renders a simple read-only roster per squad (schedule generation is
  Phase C). Verified with a new Playwright pass against the real served `frontend/` tree: queue picker
  rendering, open/bid/close/skip flows via a stubbed `authedFetch`, close/skip button enable-state
  syncing with bid presence, the bid-bump-button-reads-live-value behavior, the squads_locked roster
  view, and the polling lifecycle (`isDraftPollingActiveFor()` - a small inspector added since the
  timer's own tracking variable is a script-scoped `let`, not a `window` property) starting on auction
  render and stopping on tab switch - plus a real (non-mocked) 4-second wait confirming the actual
  `setInterval` fires on schedule, not just via manually-invoked handlers. The existing Phase A
  Playwright suite (`/tmp/test_draft_ui.js`) still passes unmodified.
  Files: `backend/lambdas/tournaments/index.py`, `frontend/js/app.js`. No `template.yaml` change needed
  - the Phase A `/tournament-draft{proxy+}` API Gateway resource tree already covers these new routes.
- ✅ 2026-08-21 (v1.45.0-phaseA) — **Manual-mode tournaments (leaders + pool draft/auction), Phase A:
  data model + leader/pool endpoints + drag/tap pool board (Owner request).** First slice of the
  big "leaders draft squads via a live auction, then group stage + knockout auto-generate" feature -
  see the phased plan for the full design. This phase only covers `pools_open -> pools_locked`; the
  auction engine (Phase B), tie-based schedule generator (Phase C), and an auth-hardening pass
  (Phase D) are still to come.
  **Backend** (`backend/lambdas/tournaments/index.py`): new `format: 'manual_draft'` tournament shape
  (`manual_draft` config, `leaders`, `pools.assignments`/`pools.unassigned`) alongside the existing
  `knockout`/`groups_then_knockout` formats - no new DynamoDB table, everything nests onto the same
  tournament item. New Cognito-authorized route tree `/tournament-draft{proxy+}` (separate from
  `/tournaments{proxy+}`, which is `AuthorizationType: NONE` at the gateway and so never gets real
  caller identity - matches the same reasoning as the existing `/create-tournament` resource):
  `POST /tournament-draft` (create), `POST .../leaders`, `POST .../add-player` (wires an existing or
  freshly `/register-and-join`'d player into the unassigned tray), `PUT .../pools` (full-replace one
  pool's membership - a player moved into a pool is stripped from wherever they were before, and a
  player dropped from a pool with no `player_ids` re-adding them returns to `unassigned`, so nobody is
  ever silently lost or duplicated), `POST .../lock-pools` (validates every player is assigned and
  each pool has enough non-leader-owned members for every other leader to fill their pick quota from
  it). New `_authorize_tournament_organizer()` - ported from `groups/index.py`'s
  `_authorize_group_action` (SuperAdmin, or owner/admin of the tournament's group) - tournaments had
  **no role-based auth anywhere** before this (not even DELETE, which is gated only by the shared
  `CONFIRMATION_CODE` secret). Verified with `/tmp/test_manual_draft_pools.py` (18 checks: shell
  creation, add-player, leader/pool validation, move-between-pools correctness, lock-quota rejection,
  and a full authz matrix) plus the existing regression suite (all still passing, confirming the new
  dispatch branch doesn't disturb `/create-tournament`/`/tournaments{proxy+}`).
  **Frontend**: `#tournament_format` gained a "Manual mode" option with its own config block (budget,
  pool count, picks/pool, matches-per-tie x2 - the last two aren't used until Phase C but are captured
  at creation so they don't need a later migration). `renderTournament()` now dispatches manual-draft
  tournaments to `renderManualDraftTournament()`, which renders a leader checklist and a pool board:
  tap-a-player-then-tap-a-pool is the primary interaction (works on phones - this app's actual usage),
  native HTML5 drag-and-drop is layered on top for `draggable`-capable pointers as a bonus, not a
  requirement (this app had no drag-and-drop before). "Add new player" reuses the existing
  `/register-and-join` route rather than inventing new player-creation logic. Verified with a
  Playwright pass against the real served `frontend/` tree (not `file://`): format-toggle visibility,
  every new function defined, pool/chip DOM renders correctly from a hand-built tournament object,
  leader-chip styling, tap-select -> tap-pool triggers exactly one correctly-shaped `PUT .../pools`
  call, and a regression check that a normal (non-manual-draft) tournament still renders unaffected.
  Files: `backend/lambdas/tournaments/index.py`, `infrastructure/template.yaml` (new
  `TournamentDraftResource`/`{proxy+}` API Gateway tree, Cognito-authorized, same `TournamentsFunction`
  - no new Lambda or IAM policy needed), `frontend/index.html`, `frontend/js/app.js`,
  `frontend/css/styles.css`.
- ✅ 2026-08-21 — **Staging deploy never set `--cache-control` on the frontend sync (prod's did) -
  likely explanation for v1.44's FAB "working on mobile but not desktop" report.**
  Owner reported the new floating record-match button rendered top-left and unpinned on desktop
  (staying in normal document flow, scrolling away) but correctly bottom-right/fixed on mobile, and
  that clicking it did nothing on desktop. Reproduced the real `frontend/` tree under a local HTTP
  server (not `file://`, which resolves the site's absolute `/css/styles.css` against the filesystem
  root instead of the page - a red herring caught before it wasted a fix) and drove it headlessly at a
  1440x900 viewport: `#record-match-fab` computed to `position: fixed`, bottom-right, `border-radius:
  50%`, stayed pinned through a scroll, and a click correctly switched to the Matches tab
  (`location.hash` -> `#matches`). The shipped code has no bug. The most likely explanation left is
  caching: `deploy-staging.yml`'s frontend sync step never set `--cache-control`, unlike `deploy.yml`
  (prod) which always has - with no header at all, a browser or CloudFront can keep serving whatever it
  cached from an earlier visit with no revalidation forced, so a desktop browser that had staging open
  earlier in the session could easily still be running pre-v1.44 assets while a fresh mobile browser
  fetches the real ones. Matched the staging sync to prod's `--cache-control "no-cache"` on all three
  `aws s3 cp` calls. **Confirmed 2026-08-21** - owner hard-refreshed the affected desktop browser and
  the FAB now works correctly, consistent with this being a stale-cache issue rather than a code bug
  (a hard refresh bypasses cache regardless of server headers either way, so this alone can't prove the
  `--cache-control` change was the fix vs. simply clearing the same stale cache - but the shipped
  v1.44.0 code was already independently verified correct above, and this staging-only header fix is
  zero-risk and matches prod's existing behavior, so it's being kept rather than reverted).
  Files: `.github/workflows/deploy-staging.yml`.
- ✅ 2026-08-21 (v1.44.2) — **Admin "rename a player" control (Owner request).** `PUT /players/{id}`
  already accepted a `name` change (gated by the existing shared confirmation code, `update_player`,
  `backend/lambdas/players/index.py:1754-1838`) but no admin UI ever called it with `name` - only
  `adminSetPrivacy()` used that route, and only for `privacy_private`. Added a "Rename a player" control
  in Settings admin tools, next to the existing "force a player's visibility" block: a player picker +
  new-name field + Rename button, prompting for the confirmation code the same way
  `removePlayerFromGroup()` does (`nwPrompt`), then `PUT`s `{name, confirm}`. Deliberately does **not**
  touch `nickname` (the unique id) - this is purely for fixing a confusing/typo'd real name. No backend
  change needed. Files: `frontend/index.html`, `frontend/js/app.js`
  (`populateAdminRenameSelect`/`adminRenamePlayer`).
- ✅ 2026-08-20 (v1.44.0) — **Floating "record a match" shortcut + enlarge-on-select tabs + redesigned
  the Flame card frame (all three Owner-requested).**
  (1) **Always-floating "+" to record a match.** A fixed `#record-match-fab` circular button
  (`.record-fab`, bottom-right, riding above page content but below the header dropdown/any modal)
  is now visible on every tab except Matches itself - clicking it jumps straight to the record-match
  form instead of requiring the Matches tab first. Refactored the `.tab-btn` click listener (previously
  one inline closure) into a named `activateTab(tabName)` so the FAB's `jumpToRecordMatch()` can reuse
  the exact same tab-switch + on-open side effects (lazy loads, finance auto-unlock, URL hash, etc.)
  instead of duplicating any of that logic - behavior is identical either way, just two triggers.
  Reuses the Matches tab's existing guest/unlinked-account notices, so a logged-out or unlinked caller
  sees the same prompts as navigating there directly; no separate auth handling needed in the FAB.
  (2) **Tabs now enlarge on selection.** `.tab-btn` gained a `transform: scale(1.12)` + bounce-easing
  transition on `.active`, anchored to the bottom edge (where the underline is) so it grows in place
  rather than drifting. Pure CSS - the existing `.active` class toggle already did all the state
  management needed.
  (3) **Redesigned the "Flame" card frame (Owner: "what is this monstrosity ... i was expecting it to
  be like this in Steam").** The old flame frame drew a jagged, animated flame-tongue SVG
  (`FLAME_BORDER_SVG`/`svgTongue` for the live preview, `drawFlame`/`flameEdge`/`flameTongue` for the
  canvas export) covering most of the card's border on all 4 edges - a cartoonish sawtooth shape wildly
  out of step with every other frame preset (Gold/Ruby/Chrome/etc.), which all use a clean, tasteful
  gradient-stroke border. Removed all of that dead code and replaced it with the same two-stroke
  gradient-border treatment as Gold/Carbon, just in warm ember tones (amber → orange-red → gold) with a
  soft glow - matches the quality bar of every other frame instead of standing out as an outlier, and
  no longer needs its own `ANIM_FRAMES` entry or per-frame `t`-driven flicker animation since it's now a
  static gradient like Gold/Ruby/Chrome. Verified by rendering the exact shipped CSS rule and canvas
  function (byte-for-byte, not reimplemented) in a headless-browser harness for both the live-preview
  and PNG-export paths - screenshots confirm a clean warm-gradient border, no jagged shapes.
  Files: `frontend/index.html` (1), `frontend/css/styles.css` (1, 2), `frontend/js/app.js` (1),
  `frontend/js/card-share.js` (3).
- ✅ 2026-08-20 (v1.43.0) — **Backlog/defect sweep: finished the scan-pagination fix everywhere,
  scrubbed the committed AWS account id, added a CI guard against a destructive frontend sync
  (Owner asked to pick up whatever was left off/in the backlog/a defect).**
  (1) **`table.scan()` pagination (KNOWN_ISSUES #15) was NOT actually finished club-wide.** The
  2026-08-09 "RESOLVED" note only ever covered the matches and players lambdas - `_scan_all()` existed
  in those two files only. Auditing every lambda for a bare `.scan(` (the same grep the original fix
  recommended, never actually re-run afterward) turned up 17 more unpaginated full-table scans: 4 in
  `finance/index.py` (`groups_table` x2, `finance_table`, `matches_table`), 5 in `groups/index.py`
  (`players_table` x2, `groups_table` x3), 5 in `tournaments/index.py` (`tournaments_table`,
  `players_table`, `matches_table` x3 - the one with the least bounded, fastest-growing table of any
  lambda), 2 in `progress_scheduler/index.py` (`matches_table`, `groups_table`), 1 in
  `register_player/index.py` (nickname-uniqueness check), plus 9 still-bare `claim_requests_table`/
  `groups_table` calls left in `players/index.py` itself that the 2026-08-09 fix's own "only the small
  claim_requests/groups scans remain bare" note had flagged but never closed. Copied `_scan_all()` into
  all 5 lambdas that lacked it and routed every one of those 17 scans (plus the 9 in `players/index.py`)
  through it - every full-table scan in the codebase is now paginated. Verified with a fixture
  `PagingFakeTable` (2 items/page, unlike every other test's single-page fake, so the
  `LastEvaluatedKey` loop is actually exercised) confirming correct pagination across all 6 lambdas.
  (2) **Committed AWS account id (KNOWN_ISSUES #3).** `current-policy.json` / `networth-deploy-policy.json`
  each had one ARN hardcoding the account id where every other ARN in the same files already used a
  `*:*` wildcard; replaced to match (same effect, zero behavior change - IAM doesn't need the account
  segment when scoping to the calling account's own resources). Neither file is read by any script or
  workflow, confirmed by grepping `.github/workflows/`, so this was a pure cleanup, not a live-config
  risk.
  (3) **CI guard against a destructive frontend sync (KNOWN_ISSUES #9).** Both `deploy.yml` and
  `deploy-staging.yml` gained a step, right after checkout and before AWS credentials are even
  configured, that fails the run if `.github/workflows/*.yml` ever combines an s3 sync with the
  destructive removal flag - the bucket also serves user-uploaded cosmetics under `uploads/`, and every
  deploy step already deliberately uses `aws s3 cp` instead. Written so the check's own two search terms
  are built from concatenated string parts rather than one literal token - a first pass written the
  naive way tripped on its own step name/comments, since describing the danger in prose necessarily
  contains the same text the grep was searching for. Verified the guard is silent against the current
  repo and does fire against a deliberately-injected offending line in a throwaway copy.
  Files: `backend/lambdas/finance/index.py`, `backend/lambdas/groups/index.py`,
  `backend/lambdas/tournaments/index.py`, `backend/lambdas/progress_scheduler/index.py`,
  `backend/lambdas/register_player/index.py`, `backend/lambdas/players/index.py`,
  `current-policy.json`, `networth-deploy-policy.json`, `.github/workflows/deploy.yml`,
  `.github/workflows/deploy-staging.yml`.
- ✅ 2026-08-20 — **Non-members table was invisible to anyone with no walk-in fee record
  (Owner-reported: played with us, isn't a slot member, doesn't show up).**
  `insights()`'s guest table was built ENTIRELY from `walkins` records - a player who's played
  matches but was never a Yes member anywhere AND never had a walk-in fee logged for them (no one got
  around to it, or they simply hadn't paid yet) never entered the `guests` dict at all, so they were
  completely absent from the one table meant to catch exactly that case. Added a pass after the
  walk-in-derived guests are built: anyone appearing in the match log (`active_days`, already computed
  for the cost-per-member table) who isn't a `member_pids` and isn't already a guest gets added with
  `sessions: 0, fees_paid: 0` and their real days-attended - so they now surface with a full
  attendance count and (once a default walk-in fee is set) a real pending amount, instead of not
  existing in this view. Also simplified/fixed the days-attended pid lookup while in there: it used to
  re-scan every walk-in record per guest to recover their player_id from the dict key; since the key
  literally IS the player_id whenever one exists (by construction), that's now a direct check instead
  of a nested scan - also fixes it for these newly-added match-only guests, who have no walk-in record
  to scan in the first place. Verified with a fixture (a player with 2 match-log days, no walk-in
  record, not a member -> now appears with `days_attended: 2, sessions: 0, fees_paid: 0`).
  File: `backend/lambdas/finance/index.py`.
- ✅ 2026-08-20 — **Group-wide expense/walk-in split reworked (expense evenly per unique member,
  walk-in earnings weighted by slot-count) + walk-in entries now editable (Owner-requested).**
  (1) **Split rework.** A slot-less ("(whole group)") expense or walk-in record used one combined
  `residual_per_head`, split evenly across the month's DISTINCT Yes members regardless of how many
  slots each was in. Per the owner's worked example (3 slots x 6 = 18 slot-enrollments, 12 unique
  members: 2 in all 3 slots, 2 in 2 slots, 8 in exactly 1), that's now two different splits: the
  EXPENSE side (`cost_per_head` and the expense-driven half of `residual_per_head`) stays an even
  per-unique-member split (rare, doesn't scale with slot count - e.g. a one-off shuttle-box buy); the
  WALK-IN side (`extra_collected`) is now weighted by each member's slot-count that month (walk-ins
  occupy court time per slot, so someone in 3 slots is exposed to more of that than someone in 1).
  `_settlement_rows` (finance lambda) gained `member_slot_counts` and a post-pass that overwrites the
  GROUP_SLOT bucket's `residual_per_head` to be expense-only and adds a per-member `walkin_shares`
  dict (falls back to an even split if a group-wide walk-in exists with zero real slot-enrollments on
  record, so money is never silently dropped). `my_settlement` and `insights()` both sum
  `residual_per_head + walkin_shares[ident]` wherever they used to read the old combined
  `residual_per_head` alone, so a member's own dues/relief already reflect the new split with no
  frontend math needed. Frontend: the Monthly settlement summary table shows the two pieces
  separately for `(whole group)` rows instead of one now-misleading average. Verified against the
  owner's exact example (1200 expense / 12 = 100 even; 900 walk-in split 3/2/1 parts -> ₹150 for the
  two triple-slot members, ₹100 for the two double-slot members, ₹50 each for the eight single-slot
  members) plus a `my_settlement` end-to-end check.
  (2) **Walk-in entries are editable.** Only had Delete before (Owner-reported: "i added a few
  accidentally multiple times" - no way to fix a typo or duplicate without delete-then-redo). Added an
  Edit button mirroring the existing expense-edit pattern (`editingWalkinId`, `resetWalkinEdit`,
  `finance-cancel-walkin-edit-btn`), backed by the `PUT /walkins/{id}` route that already existed
  server-side (`update_record` is generic per record type - no backend change needed). Wired into the
  write-tier visibility gate (`fin-edit-walkin` added alongside `fin-edit-exp` in
  `applyFinanceRoleVisibility`) so view-only finance roles don't see it, same as expense edit.
  Files: `backend/lambdas/finance/index.py` (1), `frontend/index.html` (2, one button),
  `frontend/js/app.js` (1 summary table, 2 walk-in list).
- ✅ 2026-08-20 — **Non-member attendance-vs-fees tracking + collapsible Finance sections
  (Owner-requested).**
  (1) **Non-members: days attended vs. fees collected, with expected/pending.** The existing
  Insights "walk-in conversion" table (guest sessions + fees paid) had no way to answer "did they
  actually pay for every day they showed up" - `insights()` now cross-references each guest's match
  log attendance (`active_days`, the same source `cost_rows` already uses for members) against their
  walk-in fee records. New optional club-wide **default walk-in fee** setting (Finance settings card,
  `default_walkin_fee` on the shared `settings` record) drives an **expected** figure
  (`days_attended × fee`) and a **pending** figure (`expected − fees_paid`) per guest; left unset, the
  table still shows days-attended/sessions/fees-collected with no guessed rupee amount. A guest
  entered as a free-text name (never linked to a roster player) can't be attendance-matched against
  the match log (matches only ever reference player_id) - their days-attended column shows "-",
  unchanged otherwise. Retitled the section "🎯 Non-members: attendance, fees & conversion" to make
  clear it now covers this, not just conversion. Verified with a fixture (2 match-log days, 1 walk-in
  fee record, ₹80 default fee → expected ₹160, pending ₹80) and a settings round-trip test (set,
  partial-update preserves it, explicit clear).
  (2) **Collapsible Finance sections.** Generalized Stats' existing `makeStatsCollapsible` (tap a
  card's heading to expand/collapse) into `makeCardsCollapsible(containerId)` and reused it for
  `#finance-content` via `makeFinanceCollapsible()`, called on both finance-unlock paths. Only the
  unlocked ledger cards (Monthly settlement, Expenses, Memberships, Walk-ins, Insights, Finance
  settings) collapse; the lock/unlock card and the always-visible "My dues" card sit outside
  `#finance-content` and are deliberately left as-is, since they're the small single-glance entry
  points, not long scrollable lists like Stats' cards.
  Files: `backend/lambdas/finance/index.py` (1), `frontend/index.html` (1 settings field, no other
  markup changes needed for 2 - collapsibility is pure JS), `frontend/js/app.js` (1, 2).
  **Not built this round:** the match-timing/slot-mismatch and match-count-vs-points-density anomaly
  metrics the owner also raised - explicitly flagged by them as speculative/secondary ("just as a
  metric," "we can have this as a secondary"), and genuinely under-specified (grace-period threshold,
  what counts as a mismatch, whether entry timestamp vs. actual play time is even a reliable enough
  signal to act on). Asked the owner whether/how they want it scoped before building it.
  **(Note: that question was never actually surfaced to the owner in a reply - see 2026-08-20 v1.42.0
  below, where they said "you parked something, please proceed" and it got built anyway, resolved by
  making both checks fail-safe/skip-on-ambiguity instead of blocking on the missing answer.)**
- ✅ 2026-08-20 (v1.42.0) — **Finance-role reporting bug (Owner-reported "a deletion is not enabled,
  i can only see edit option") + month-wise non-member scoping + the parked slot-timing/match-density
  anomaly checks (all three from the same owner message).**
  (1) **Finance delete buttons invisible to group owners.** `finance_key_for_caller` (the
  `/finance-access` handler that tells the frontend which role to render buttons for) only ever
  called `_finance_level`/`_finance_role` - the LEGACY/GLOBAL-only check - and never
  `_group_finance_level`, the function that correctly grants a group owner/admin `delete` tier on
  their own group. A group owner whose only reported role came from a stale/lower legacy grant (or
  none at all) never got told they had delete access, so `.fin-del` buttons - which already existed
  in the walk-in/expense/membership row templates, gated purely on `myFinanceRole` - stayed hidden;
  only `.fin-edit*` (write-tier) showed. Not a rendering bug: the buttons were always there, the role
  string reaching the browser was just wrong. Added `_effective_finance_role(claims, group_id)`
  (finance lambda) - reports the higher of the caller's legacy/global role and their per-group role
  for the group being viewed; real enforcement is untouched, this only decides what the UI shows -
  and wired `group_id` through from `finance_key_for_caller`'s query params. Frontend: added
  `refreshFinanceRoleForGroup()` and wired it into `tryAutoFinanceUnlock()`, `financeUnlock()` (which
  was *also* missing `populateFinanceGroups()` entirely on the manual-key-entry path, so
  `currentFinanceGroupId` stayed null and no group-scoped role check was even possible there - added
  that call too), the finance-group `<select>`'s change handler, and the Finance tab reopen path.
  Verified with a fixture: an owner with only a legacy `finance_role='write'` now gets `'delete'`
  when their own group's `group_id` is passed, unchanged `'write'` with no group_id (backward compat).
  (2) **Non-members table now scopes to the Month+Year picker, not just lifetime** (Owner-requested:
  "sashi did not pay anything for now as we discussed he would pay at the end" - a guest who's agreed
  to settle up later shouldn't read as a standing lifetime debt). `insights()`'s guest/conversion
  block now filters walk-in records, match-log attendance (`active_days`), and the derived
  sessions/fees/days-attended/expected/pending figures to the same `f_month`/`f_year` filter the
  ghosts/cost-rows tables already used, when both are set; leaving Year blank keeps the lifetime view
  unchanged for anyone not using the picker. `conversion.scoped_to_month` tells the frontend which
  mode it's in; `renderInsights()` shows a one-line note either way ("Showing July 2026 only..." /
  "Showing the lifetime total..."). Verified with a fixture: a guest with match-log days in both July
  and August shows 3 lifetime / 2 July-only / 1 August-only.
  (3) **Slot-timing mismatch + match-density anomaly flags** (the item parked 2026-08-19, built now
  on explicit go-ahead: "i think you parked something, please proceed with that as well"). Two
  best-effort, non-authoritative diagnostics under a new collapsed-by-default "⏱️ Slot-timing & match-
  density flags" section in Insights, clearly labeled as heuristic:
    - **Timing mismatches** - a match whose local kickoff time falls outside every parseable slot
      window (± 15 min grace, `SLOT_GRACE_MINUTES`) its participants are assigned to. New
      `_parse_slot_window(label)` regex-parses free-text slot labels ("7AM-8AM", "7-8AM",
      "19:00-20:00", crossing-midnight windows) into a local minute-of-day range, returning `None`
      (skip, never flag) on anything it can't parse. New `_local_minutes_of_day(iso_ts, offset)`
      converts a match's stored UTC timestamp using a new club-wide `club_utc_offset_minutes` setting
      (defaults to IST, +330 - inferred from context: rupee currency, Indian names throughout; a
      Finance-settings field lets the owner correct this if wrong). A match is skipped entirely
      (never flagged) unless local time, the group's parsed slots, AND every participant's slot
      assignment are all available - ambiguity means "can't check," never a false flag.
    - **Density flags** - per (date, inferred slot), whether the assumed total playing time (sum of
      `ASSUMED_MINUTES_PER_GAME` - 15 min for a 21-point game, 8 for an 11-point game, 12 default -
      across that bucket's matches) exceeds the slot's parsed duration by more than 1.3x
      (`DENSITY_FLAG_RATIO`). Since match records carry no direct `slot` field, a match only
      contributes to a bucket when ALL of its participants share exactly one common assigned,
      parseable slot; matches spanning mixed or unassigned players are excluded rather than guessed
      at.
    New `_timing_checks(matches, group, offset_minutes, target_ym)` (finance lambda) returns both
    lists, scoped by the same month/year filter as the rest of `insights()`; wired into its response
    as `timing_mismatches`/`density_flags`. `insights()` now also fetches the group record (for
    `slots`/`slot_members`) when a `group_id` is given. Verified with fixtures: an on-time match in
    its assigned window doesn't flag, one 3.5 hours off does; 6 assumed-15-min games crammed into a
    60-min slot (1.5x) flags, 2 games (0.5x) don't; unparseable/no-group/no-slot-assignment inputs
    all degrade to empty results rather than errors.
  Files: `backend/lambdas/finance/index.py` (all 3), `frontend/js/app.js` (1, 2, 3),
  `frontend/index.html` (3, one settings field).
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
