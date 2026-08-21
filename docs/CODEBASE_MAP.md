# NetWorth — Codebase Map (LLM Reference)

> **Purpose:** a single file an LLM (Claude, qwen3-coder + aider, Cursor, etc.) can read
> **instead of opening every source file**. Every function is listed with a one-line
> description and its line number, so a model can jump straight to the right place.
> Read this first. Only open actual source when you need the body of a specific function.
>
> **Repo:** `CodersDump/NetWorth` · region `us-east-1` · stack `networth-app`
> API GW `zywd1pvlm6` · CloudFront `d1mdot1vsm6xu6.cloudfront.net` · Cognito pool `us-east-1_svy5Sv8Av`
> **Regenerate this file:** re-run the extraction (AST for Python, regex for `app.js`) — see `docs/BACKLOG.md` → "tooling".
> **Last mapped:** repo snapshot 2026-07-29.

---

## 1. System at a glance

Serverless badminton-club manager. Static SPA on S3+CloudFront talks to API Gateway →
8 Python 3.12 Lambdas → 7 DynamoDB tables. Cognito user pool for auth; a Cognito
Authorizer gates the "secure" routes. User-uploaded cosmetics live in the same S3 bucket
under `uploads/`.

```
Browser (index.html + css/styles.css + js/app.js)
   |  fetch (Bearer idToken on secure routes)
   v
API Gateway (REST, stage: prod)  --Cognito Authorizer-->  claims{ email, custom:player_id, cognito:groups }
   |
   +-- whoami            -> whoami lambda
   +-- register / players-> players + register_player lambdas
   +-- groups (+ proxy)  -> groups lambda
   +-- matches (+ proxy) -> matches lambda      (+ EventBridge weekly -> progress_scheduler)
   +-- tournaments(proxy)-> tournaments lambda
   +-- finance (proxy)   -> finance lambda
   v
DynamoDB: players, groups, matches, tournaments, finance, claim-requests, progress-history
S3 bucket (website + uploads/)  <->  CloudFront
```

**Auth identity model:** every secure Lambda reads `event.requestContext.authorizer.claims`.
Two special dimensions:
- **SuperAdmin** = caller is in the Cognito group checked by `_is_super_admin` (via `cognito:groups`).
- **Linked player** = `claims['custom:player_id']` points at a live players-table row.
  A Cognito account can exist WITHOUT a linked player (self-signup) — `_requires_linked_member`
  is the gate that separates "signed up" from "actually a member".

---

## 2. API routes

Auth column: **NONE** = public (no Cognito Authorizer — code may still gate via view-key /
confirmation-code / SuperAdmin-inside-claims); **COGNITO** = Cognito Authorizer required.
Isolated top-level routes exist because API Gateway forbids a named path param as a sibling
of `{proxy+}` at the same parent — that constraint drives most of the odd route names below.

| Method + Path | Lambda | Auth | Purpose |
|---|---|---|---|
| GET `/whoami` | whoami | COGNITO | Echo caller claims (Epic-4 verification) |
| POST `/register` | register_player | COGNITO | Create a player (records creator) |
| GET `/players` | players | NONE | List all players |
| GET `/players?login_identifier=` | players | NONE | Resolve id/name/nickname → login email |
| PUT/DELETE `/players/{player_id}` | players | COGNITO | Update / delete one player |
| PUT `/rename-self` | players | COGNITO | Self-service nickname change |
| PUT `/update-my-card` | players | COGNITO | Self avatar/banner customization |
| POST `/claim-player` | players | COGNITO | Link my account to an existing unclaimed player |
| POST `/claim-request` | players | COGNITO | Ask an admin to link me |
| POST `/action-request` | players | COGNITO | File a destructive-action request for approval |
| GET `/claim-requests` | players | COGNITO | (Admin) list pending requests |
| POST `/claim-request-decide` | players | COGNITO | (Admin) approve/reject a request |
| GET/POST `/app-settings` | players | NONE/COGNITO | Read / (admin) set app flags |
| GET/POST/DELETE `/store` | players | NONE/COGNITO | Browse / (admin) edit store catalog |
| POST `/store-purchase` | players | COGNITO | Spend coins on an item |
| POST `/upload-url` | players | COGNITO | Presigned S3 PUT for cosmetic uploads |
| POST `/finance-access` | players | COGNITO | Request a finance role |
| GET/POST `/groups` … `/groups/{proxy+}` | groups | NONE | List/get/create/update/delete groups & members |
| POST `/group-create` | groups | COGNITO | Create group (any logged-in account) |
| POST `/group-add-player/{group_id}` | groups | COGNITO | Add player to group |
| DELETE `/group-delete/{group_id}` | groups | COGNITO | Delete group record |
| DELETE `/group-member-remove/{group_id}/{player_id}` | groups | COGNITO | Remove a member |
| PUT/DELETE `/group-role/{group_id}/{player_id}` | groups | COGNITO | Set role / remove member |
| GET `/visible-players` | groups | COGNITO | Player picker scoped to caller |
| POST `/register-and-join` | groups | COGNITO | Register a friend + join in one call |
| POST `/record-match` | matches | COGNITO | Record a match (linked members only) |
| GET/PUT/DELETE `/matches` `/matches/{match_id}` | matches | NONE/COGNITO | List / fix / delete matches |
| POST `/reorder-matches` | matches | COGNITO | (Admin) reorder by re-stamping timestamps |
| POST `/recompute` | matches | COGNITO | (Admin) full replay to rebuild ratings/XP |
| GET/POST/DELETE `/events` | matches | NONE/COGNITO | XP-multiplier events (read public) |
| GET/POST/DELETE `/quests` + POST `/quest-claim` | matches | NONE/COGNITO | Weekly quests + claim reward |
| ANY `/profile-secure/{proxy+}` | matches | COGNITO | Gated profile-data catch-all |
| POST `/create-tournament` | tournaments | COGNITO | Create tournament |
| GET/DELETE `/tournaments` `/tournaments/{proxy+}` | tournaments | NONE | List/get/delete + score submission |
| ANY `/tournament-draft` `/tournament-draft/{proxy+}` | tournaments | COGNITO | Manual-draft mode: leaders, pool board (Phase A); `remove-player` — organizer excuses a group member (e.g. themselves, if not playing) from the roster entirely, `pools_open` only, rejects removing a current leader; auction (`start-auction`/`open-lot`/`bid`/`close-lot`/`skip-lot`/`state`, Phase B); tie-based schedule (`generate-schedule`/`pick-tie-player`/`group-tie-score`/`knockout-tie-score`, Phase C) — `generate-schedule` builds either the original flat round-robin (`manual_draft.num_groups<=1`, the default, byte-identical to before) or real separate named groups (A, B, C...) with squads randomly split and a round-robin only within each group, top `manual_draft.advance_per_group` per group advancing to a combined knockout (boundary ties injected as an extra tie via `_inject_group_tiebreakers_if_needed`, round-1 same-group rematches avoided where possible via `_advance_squads_to_knockout_from_groups` — both mirror the legacy `groups_then_knockout` format's own `inject_tiebreakers_if_needed`/`advance_to_knockout`); `pick-tie-player` nominates either one `player_id` (singles) or a `player_ids` pair (doubles, per `manual_draft.match_type`); a leader nominates for their own squad, and the organizer can now also nominate for either squad given `squad_id` to disambiguate; `rename-squad` — organizer or that squad's own leader renames it, `squads_locked` onward (cosmetic, never locked); `move-squad-player` — organizer rebalances a picked player to a different squad, `squads_locked` only (before a schedule exists); `substitute-squad-player` — organizer swaps a squad member for a new replacement, `squads_locked` through `knockout`, clears any not-yet-played lineup pick for the outgoing player, leaves played-match history untouched; `organizer-assign` — organizer directly awards a queued player to a chosen leader for a chosen amount, no open lot / leader bid required (for leaders without app access); `GET /tournament-draft/{id}` — privileged pool/auction detail (organizer always, a leader only while their phase is still live), the only route that ever returns real `pools`/`draft` data — the public `GET /tournaments/{id}` always redacts both for manual-draft tournaments |
| ANY `/finance/{proxy+}` | finance | **NONE (legacy open)** | View-key/confirmation-code gated finance ops |
| ANY `/finance-secure/{proxy+}` | finance | COGNITO | Same ops, Cognito-gated by finance role |
| DELETE `/finance-delete/{record_type}/{record_id}` | finance | COGNITO | Triple-gated delete |
| GET `/finance/walkins/public`, `/finance/upi/public` | finance | NONE | Public walk-ins list + UPI pay card |

> ⚠️ The dual `/finance` (open) vs `/finance-secure` (Cognito) surface is a deliberate
> backwards-compat legacy — see `docs/KNOWN_ISSUES.md`.

---

## 3. Data model (DynamoDB, all `PAY_PER_REQUEST`)

| Table | PK | Notable attributes |
|---|---|---|
| `networth-players` | `player_id` (HASH) | `name`, `nickname` (unique id, lowercase [a-z0-9_]), `rating` (Elo, start 1000), `skill_level`, `xp`, `level`, `coins`, `coins_earned`, `previous_rating`, avatar/banner/background keys, owned-cosmetics, perk tokens, `created_by`, `created_at`. Reserved rows: app-settings row, store-catalog row. |
| `networth-groups` | `group_id` (HASH) | `name`, `roles` map (player_id→owner/admin/member), default tournament settings |
| `networth-matches` | `match_id` (HASH) | `date`(ISO), `match_type`(singles/doubles), `team_a`/`team_b`(ids), `team_a_names`/`team_b_names`, `score_a`/`score_b`, `points_to_win`, `winner`, `ratings_after`, optional `group_id`, `tournament_id`+`stage`, `point_log`+`momentum`, `approved` |
| `networth-tournaments` | `tournament_id` (HASH) | fixtures/brackets, entities, standings, format, `group_id`. `format: 'manual_draft'` items (new) additionally nest `manual_draft` (config), `leaders`, `pools` (`assignments`/`unassigned`/`locked`), `draft` (Phase B - `status`, `queue`, `current_lot`, per-leader `remaining_budget`/`pool_picks`/`squad_member_ids`, `unsold`), `squads` (Phase B - built when the draft auto-completes), `group_stage`/`knockout` (Phase C - squad-vs-squad `ties`, each holding `matches_per_tie` fixture-shaped matches + `wins_a/b`/`point_diff_a/b`/`decided`/`winner_squad_id`; `knockout` reuses the same top-level key the legacy single-match format already uses), `champion_squad_id` (Phase C, set on completion) - no new table, same item shape philosophy as `subgroups`/`knockout` |
| `networth-finance` | `record_id` (HASH) | typed records (expense/member/walkin/settings) — `record_type` prefix scan |
| `networth-claim-requests` | `request_id` (HASH) | pending claim / new-profile / edit-name / match-action / finance-access requests |
| `networth-progress-history` | `history_id` (HASH) | locked-in weekly/monthly/yearly winner snapshots (written by scheduler) |

**Elo is path-dependent:** any edit/delete/reorder of a historical match requires a full
replay — `recompute_all_ratings()` (present in both matches and tournaments lambdas).

---

## 4. Infrastructure (`infrastructure/template.yaml`, ~2800 lines)

- **8 Lambdas** (all `python3.12`, handler `index.handler`): whoami, register-player, players,
  groups, finance, matches, tournaments, progress-scheduler.
- **Cognito**: `UserPool` + `CognitoAuthorizer` (IdentitySource = `Authorization` header).
- **EventBridge rule** → `progress-scheduler` (weekly match-approval backfill + winner snapshots).
- **S3 `WebsiteBucket`** doubles as the uploads bucket (`uploads/` prefix) → deploy must NEVER
  `s3 sync --delete` or it wipes user cosmetics (workflow uses explicit `cp`).
- **Env-var wiring** each Lambda gets only the tables it needs (e.g. finance gets FINANCE/PLAYERS/MATCHES
  + `FINANCE_VIEW_KEY` + `CONFIRMATION_CODE`; players gets `USER_POOL_ID`, `UPLOADS_BUCKET`).
- Auth split across the API: 58 method-resources are `NONE`, 47 are `COGNITO_USER_POOLS` (includes
  the new `/tournament-draft` + `/tournament-draft/{proxy+}` ANY methods, both Cognito-gated).
- `infrastructure/staging.yaml` — the separate staging environment stack.

---


## 5. Backend function reference

<!-- AUTOGEN:BACKEND START (regenerated by tools/generate_codebase_map.py — do not hand-edit below) -->
### Backend Lambdas (`backend/lambdas/<name>/index.py`)


#### `whoami` — 55 LOC
_NetWorth - Epic 4 verification only: GET /whoami_

| Function | Args | Line | What it does |
|---|---|---|---|
| `handler` | event, context | 20 | — |
| `_response` | status_code, body_dict | 46 | — |

#### `register_player` — 116 LOC
_NetWorth - register_player Lambda_

| Function | Args | Line | What it does |
|---|---|---|---|
| `_scan_all` | table | 23 | Full-table scan that follows LastEvaluatedKey - a bare .scan() returns |
| `sanitize_nickname` | raw | 41 | Hard format rule: lowercase, alphanumeric + underscore only. |
| `_caller_claims` | event | 50 | — |
| `handler` | event, context | 54 | — |
| `_response` | status_code, body_dict | 107 | — |

#### `players` — 1907 LOC
_NetWorth - players Lambda (list all, update one, delete one)_

**Module constants:** `CLAIM_REQUESTS_TABLE`, `USER_POOL_ID`, `UPLOADS_BUCKET`, `GROUPS_TABLE`, `CONFIRMATION_CODE`, `ALLOWED_AVATARS`, `ALLOWED_BANNERS`, `ALLOWED_BACKGROUNDS`, `OWNER_DECIDABLE_TYPES`, `_APP_SETTINGS_ID`, `_STORE_CATALOG_ID`, `_STORE_ITEM_TYPES`, `FINANCE_LEVELS`, `ALLOWED_UPLOAD_TYPES`, `UPLOAD_KINDS`, `FREE_CARD_LAYOUTS`, `MAX_UPLOADS_PER_KIND`, `FREE_RENAMES`

| Function | Args | Line | What it does |
|---|---|---|---|
| `sanitize_nickname` | raw | 40 | Same rule as register_player's version (duplicated on purpose - |
| `handler` | event, context | 49 | — |
| `lookup_email_for_login` | identifier | 104 | Resolves a player_id, exact name, or exact nickname to the email |
| `list_players` |  | 139 | — |
| `_caller_claims` | event | 182 | — |
| `_can_self_rename` | claims | 186 | Placeholder gate - the achievement/level system this is meant to |
| `claim_player` | event | 211 | Self-service: link my Cognito account to an EXISTING, UNCLAIMED |
| `_scan_all` | table | 279 | Full-table scan that follows LastEvaluatedKey - a bare .scan() returns |
| `_is_super_admin` | claims | 293 | — |
| `_linked_player_is_live` | claims | 300 | True only if the caller's custom:player_id resolves to a player that |
| `create_claim_request` | event | 310 | Anyone logged in but not yet linked can ASK to be linked to an |
| `_caller_owned_group_ids` | claims | 380 | The set of group_ids where the caller's linked player is owner or admin. |
| `_player_group_ids` | player_id | 396 | Every group_id whose roles map contains this player. |
| `_owner_may_decide` | req, owned_group_ids | 404 | True if a group owner/admin (owning owned_group_ids) may act on req: |
| `_audit_attr` | user, name | 421 | — |
| `_cognito_users_all` | cognito | 425 | — |
| `audit_claims` | event | 435 | SuperAdmin: audit Cognito account <-> player linkage (the claim_audit.py |
| `claim_audit_action` | event | 507 | SuperAdmin link/unlink. link: point an account at a player AND stamp the |
| `list_unconfirmed_users` | event | 560 | SuperAdmin-only: Cognito accounts stuck in UNCONFIRMED (signed up but |
| `delete_unconfirmed_user` | event | 592 | SuperAdmin-only: delete a single UNCONFIRMED Cognito account by username. |
| `list_claim_requests` | event | 618 | — |
| `create_action_request` | event | 637 | A non-SuperAdmin asking for a destructive action instead of doing |
| `_create_new_profile_request` | claims, body | 704 | Creating a brand-new profile. By default this is a REQUEST an admin |
| `_get_app_setting` | key, default | 787 | App-wide flags live in one reserved row of the players table, keyed |
| `get_app_settings` | event | 796 | — |
| `set_app_setting` | event | 810 | — |
| `_load_catalog` |  | 864 | — |
| `list_store` | event | 869 | Public read - anyone can browse the store. Returns the catalog. |
| `save_store_item` | event | 875 | — |
| `delete_store_item` | event | 910 | — |
| `purchase_store_item` | event | 923 | A player spends coins on an item. Coins are deducted by bumping |
| `_create_edit_name_request` | claims, body | 968 | Renaming is now self-service-only: the target is always the |
| `_approve_edit_name` | req, claims | 1014 | — |
| `_approve_new_profile` | req, claims | 1030 | Creates the player only at approval time, and links it to the |
| `_create_match_request` | claims, body, action_type | 1066 | A match edit or delete, filed as a request rather than executed. The |
| `_create_finance_access_request` | claims, body | 1114 | A member asking for a finance role (view / write / delete) IN A GROUP. |
| `_approve_finance_access` | req | 1167 | — |
| `decide_claim_request` | event | 1193 | Approve or reject. On approval this writes the link on BOTH sides: |
| `create_upload_url` | event | 1349 | Hands back a short-lived presigned PUT. The browser uploads straight |
| `_valid_upload_key` | value, player_id, kind | 1413 | An uploaded image is referenced by key, and the key is checked |
| `_owns_store_cosmetic` | player, key, kind | 1423 | True if `key` is the image of a store cosmetic the player OWNS whose |
| `_owns_card_layout` | player, layout | 1449 | True if the player may use this stats layout - free ones always, |
| `_owns_value_cosmetic` | player, kind, value | 1457 | True if the player owns a store item of this cosmetic `kind` whose |
| `_rotate_uploads` | player_id, kind, new_key | 1478 | Maintains the player's short list of custom images, newest first, |
| `update_my_card` | event | 1510 | Self-service avatar/banner customization for the CALLER'S OWN |
| `_consume_perk` | player, player_id, effect_kind | 1685 | Spends one token of a perk the player owns (by store item effect |
| `rename_self` | event | 1714 | Self-service nickname change for the CALLER'S OWN linked player. |
| `update_player` | player_id, event | 1754 | — |
| `delete_player` | player_id, event | 1841 | — |
| `_cognito_username_for_email` | cognito, email | 1890 | The username is not always the email, so it has to be looked up. |
| `_response` | status_code, body_dict | 1898 | — |

#### `groups` — 781 LOC
_NetWorth - groups Lambda_

**Module constants:** `CONFIRMATION_CODE`, `VALID_ROLES`, `FINANCE_ROLE_LEVELS`

| Function | Args | Line | What it does |
|---|---|---|---|
| `sanitize_nickname` | raw | 42 | Same rule as register_player's version (duplicated - separate |
| `_scan_all` | table | 53 | Full-table scan that follows LastEvaluatedKey - a bare .scan() returns |
| `handler` | event, context | 71 | — |
| `_authorize_group_action` | group_id, claims | 137 | Shared check for Epic 4's group-scoped write actions: SuperAdmin, or |
| `delete_group_enforced` | group_id, event | 153 | Dual-gated (Epic 4 increment 3): a valid Cognito identity that's |
| `remove_player_enforced` | group_id, player_id, event | 165 | Same dual-gate as delete_group_enforced, for member removal. |
| `_requires_linked_member` | claims | 173 | Signing up is not the same as being a member. Cognito self-signup is |
| `register_and_join` | event | 196 | Combined 'register a friend' + 'quick-add during match setup' |
| `add_player_enforced` | group_id, event | 280 | Requires SuperAdmin, or already owner/admin of THIS group - reuses |
| `create_group_enforced` | event | 289 | Requires a valid Cognito login (any authenticated account - no |
| `_consume_extra_group_perk` | player_id | 323 | Spend one extra_group token if the player owns one. Mirrors the |
| `visible_players_for_caller` | event | 347 | For populating the Profile tab's player picker: SuperAdmin gets |
| `create_group` | event | 406 | — |
| `list_groups` |  | 430 | — |
| `get_group` | group_id | 449 | — |
| `update_group_defaults` | group_id, event | 475 | Save a group's default tournament creation settings (format, points, |
| `set_group_slots` | group_id, event | 496 | Owner/admin group settings via the Cognito-authorized PUT |
| `delete_group` | group_id, event | 598 | Deletes only the group record itself. Player records are never |
| `add_player` | group_id, event | 612 | — |
| `remove_player` | group_id, player_id, event | 654 | — |
| `_caller_claims` | event | 676 | Claims API Gateway's Cognito Authorizer attaches to the request. |
| `_is_super_admin` | claims | 684 | — |
| `set_role` | group_id, player_id, event | 689 | Set (or change) a member's role within this group. |
| `set_finance_role` | group_id, player_id, event | 732 | Set a member's per-group FINANCE role (none/view/write/delete) in this |
| `_response` | status_code, body_dict | 772 | — |

#### `matches` — 2592 LOC
_NetWorth - matches Lambda (singles + doubles)_

**Module constants:** `K_FACTOR`, `XP_PLAYED`, `XP_WIN_BONUS`, `XP_TOURNAMENT_WIN`, `XP_MARGIN_PER_POINTS`, `XP_MARGIN_CAP`, `XP_LEVEL_COEFF`, `COINS_PER_LEVEL`, `_EVENTS_ROW_ID`, `_QUESTS_ROW_ID`, `_APP_SETTINGS_ID`, `_PRIVATE_ID_KEYS`, `QUEST_TYPES`, `_SEASON_ROW_PREFIX`, `COMEBACK_BONUS_THRESHOLD`, `COMEBACK_BONUS_PER_POINT`, `COMEBACK_BONUS_CAP`

| Function | Args | Line | What it does |
|---|---|---|---|
| `level_from_xp` | xp | 77 | Inverse of xp = 5*N^2, floored: the highest level fully paid for by |
| `xp_for_level` | level | 86 | Total XP needed to reach a given level - used for progress bars. |
| `xp_for_match` | stage, won, margin | 91 | Base XP a single player earns for one match (before any event |
| `_scan_all` | table | 114 | Full-table scan that follows LastEvaluatedKey - a bare .scan() returns |
| `_load_private_ids` |  | 129 | player_ids currently flagged private - only when the feature flag is on, |
| `_entry_is_private` | x, private_ids | 141 | — |
| `_scrub_private` | obj, private_ids | 144 | Recursively drop leaderboard/distribution entries belonging to a private |
| `_load_quests` |  | 180 | — |
| `_week_bounds_utc` | now | 185 | Monday 00:00 (inclusive) to next Monday (exclusive), as ISO date |
| `_evaluate_quest` | quest, player_id, week_matches, player_rating | 195 | Returns how many times the player has satisfied this quest's condition |
| `_season_config` |  | 243 | Season definitions + soft-reset k live in the shared app-settings row |
| `_resolve_season` | resolved, which | 262 | — |
| `_ensure_season_baseline` | season, k, items | 276 | Freeze, once, each player's lifetime rating as of the season start |
| `compute_season_leaderboard` | season, items, k, min_games | 307 | Derived climb board: everyone starts the season at a soft-reset baseline |
| `_season_board_leaders` | season, items, k | 351 | Leaders for a season: sealed (frozen) if it has ended, else live. |
| `_season_badges_for` | player_id, leaders | 369 | A player's standing + earned badges on one season board. |
| `compute_player_season_summary` | player_id, items | 392 | Per-season standing + badges for one player, across started seasons. |
| `_quest_period` | quest | 413 | (bounds, claim_prefix, label) for a quest by scope. Season-scoped quests |
| `list_quests` | event | 428 | Returns this week's quests with the caller's progress and claim state. |
| `save_quest` | event | 471 | — |
| `delete_quest` | event | 501 | — |
| `claim_quest` | event | 514 | Player claims a completed quest's reward. Verified server-side against |
| `_load_events` |  | 571 | — |
| `event_multiplier_for_date` | date_str, events | 575 | The XP multiplier active on a given match date (default 1.0). Pass a |
| `display_name` | player_item, fallback | 596 | Single source of truth for name formatting: 'Nickname (Real Name)' |
| `compute_comeback_bonus` | momentum | 613 | Extra rating-point bonus for the winning side, on top of the |
| `_is_valid_completed_game` | score_a, score_b, target | 627 | BWF-style badminton scoring: first to `target` points wins, but must lead |
| `_caller_claims` | event | 644 | Claims API Gateway's Cognito Authorizer attaches to the request. |
| `_is_super_admin` | claims | 650 | — |
| `_caller_may_edit_match` | claims, match | 655 | Who may directly edit/delete a match (PUT/DELETE /matches/{id}): |
| `_can_view_profile` | claims, target_player_id | 687 | SuperAdmin sees everyone. Anyone can view their own profile. A |
| `_requires_linked_member` | claims | 707 | Signing up is not the same as being a member. Cognito self-signup is |
| `record_match_enforced` | event | 730 | — |
| `profile_view_enforced` | event | 740 | Entry point for the isolated /profile-secure/{proxy+} catch-all. |
| `handler` | event, context | 767 | — |
| `list_events` | event | 830 | Public read - the frontend shows an active-event banner to everyone. |
| `save_event` | event | 838 | SuperAdmin creates or updates an event (upsert by event_id). |
| `delete_event` | event | 868 | — |
| `recompute_now` | event | 881 | SuperAdmin-only: replay every match to rebuild ratings, XP, levels |
| `reorder_matches` | event | 891 | Reorders a set of matches by reassigning their timestamps. |
| `record_match` | event | 959 | — |
| `update_match` | match_id, event | 1000 | Fix a mis-entered score on an already-recorded standalone match. |
| `delete_match` | match_id, event | 1060 | Permanently delete a mis-recorded match - e.g. the wrong player was |
| `recompute_all_ratings` |  | 1080 | Elo is path-dependent - each match's rating change depends on the |
| `compute_momentum_stats` | point_log, winner | 1204 | Longest scoring streak per team, and how big a deficit the winner overcame. |
| `compute_adaptive_k` | pairing_count | 1251 | Higher K for a fresh/novel doubles pairing (each match together is |
| `get_pairing_count` | team_ids, exclude_match_id | 1267 | How many prior doubles matches has this exact 2-player team played |
| `_play_and_log` | match_type, team_a_ids, team_b_ids, score_a,  | 1287 | — |
| `list_matches` | event | 1384 | — |
| `compute_partnerships` | player_id, items | 1544 | For a given player, tally win/loss record with each doubles partner |
| `get_group_member_ids` | group_id | 1586 | The set of player_ids belonging to a group, used to filter WHO shows |
| `compute_attendance` | items, group_id_filter | 1597 | Per-player attendance/consistency: total matches, distinct calendar |
| `compute_hall_of_fame` | items, group_id_filter | 1665 | Highlight stats computed from full chronological match history: |
| `compute_achievements` | player_id, matches, tournaments | 1986 | Milestone/tiered achievement progress for one player: total matches |
| `compute_top_opponents` | player_id, matches, top_n | 2127 | This player's win/loss record against every opponent they've ever |
| `compute_overall_record` | player_id, matches | 2169 | This player's total win/loss record, split by singles and doubles. |
| `compute_head_to_head` | player_id, opponent_id, matches | 2198 | One player's win/loss record specifically as an OPPONENT of another |
| `compute_with_partner` | player_id, partner_id, matches | 2230 | One player's win/loss record when partnered WITH another player on |
| `compute_recent_form` | player_id, matches, limit | 2281 | A player's last N matches, in chronological order (oldest to |
| `compute_diversity` | items, group_id_filter | 2338 | For every player: how concentrated their doubles partnerships are. |
| `compute_progress_history_summary` | scope_label, period_name | 2383 | Reads the permanent, locked-in weekly/monthly/yearly winner history |
| `compute_progress_badges` | items, group_id_filter | 2460 | For each of the last week/month/year: who improved their rating the |
| `compute_partner_distribution` | player_id, items, top_n | 2536 | For the radar/spider chart: one player's doubles partners, sorted by |
| `_response` | status_code, body_dict | 2583 | — |

#### `tournaments` — 2747 LOC
_NetWorth - tournaments Lambda (singles or doubles)_

**Module constants:** `K_FACTOR`, `COMEBACK_BONUS_THRESHOLD`, `COMEBACK_BONUS_PER_POINT`, `COMEBACK_BONUS_CAP`, `CONFIRMATION_CODE`

| Function | Args | Line | What it does |
|---|---|---|---|
| `_scan_all` | table | 42 | Full-table scan that follows LastEvaluatedKey - a bare .scan() returns |
| `compute_comeback_bonus` | momentum | 66 | Extra rating-point bonus for the winning side, on top of the |
| `compute_momentum_stats` | point_log, winner | 78 | Longest scoring streak per team, and how big a deficit the winner overcame. |
| `_is_valid_completed_game` | score_a, score_b, target | 126 | Same BWF-style rule as the standalone matches Lambda: win by 2 at |
| `_caller_claims` | event | 140 | Same pattern as matches lambda - see that file's comment for |
| `_is_super_admin` | claims | 149 | Ported from groups/index.py - identical logic, kept in sync by hand |
| `_authorize_tournament_organizer` | item, claims | 156 | Shared check for every manual-draft organizer-only write (set |
| `_authorize_pool_auction_viewer` | item, claims | 177 | Who may see pool assignments / auction budgets & bids for a |
| `create_tournament_enforced` | event | 198 | — |
| `handler` | event, context | 204 | — |
| `seeded_order` | players | 252 | Sort by current rating, descending. New players just use their |
| `pair_for_balance` | ordered_players | 262 | Given a skill-ordered list, pair strongest with weakest (snake |
| `create_tournament` | event | 277 | — |
| `build_round_robin` | entities | 419 | — |
| `build_knockout_round` | entities | 436 | — |
| `_bye_match` | entity | 471 | — |
| `handle_draft_route` | event | 498 | — |
| `_draft_get_tournament` | tournament_id | 557 | Shared load+validate for every route below: must exist and must be |
| `_draft_everyone` | item | 568 | Every player currently accounted for in this tournament's pool |
| `create_manual_draft_tournament` | event, claims | 577 | Creates the shell for a manual-mode tournament: leaders, pools, the |
| `set_leaders` | tournament_id, event, claims | 661 | — |
| `add_draft_player` | tournament_id, event, claims | 687 | Lets the organizer drop a player into the unassigned tray while |
| `remove_draft_player` | tournament_id, event, claims | 717 | The inverse of add_draft_player: drops someone out of this |
| `set_pool_assignment` | tournament_id, event, claims | 755 | Full replace of one pool's member list - the simplest, idempotent |
| `lock_pools` | tournament_id, event, claims | 815 | — |
| `_draft_decided_ids` | draft | 876 | Every player_id that's no longer available to auction: already won |
| `_authorize_leader` | item, claims | 887 | Caller must be one of THIS tournament's registered leaders (matched |
| `start_auction` | tournament_id, event, claims | 898 | — |
| `open_lot` | tournament_id, event, claims | 950 | — |
| `submit_bid` | tournament_id, event, claims | 987 | — |
| `_maybe_freeze_squads` | item, draft | 1044 | Shared by close_lot and organizer_assign: once every leader's every |
| `close_lot` | tournament_id, event, claims | 1075 | — |
| `organizer_assign` | tournament_id, event, claims | 1107 | Lets the organizer record a winning bid and award a player entirely |
| `skip_lot` | tournament_id, event, claims | 1167 | — |
| `get_draft_state` | tournament_id, event, claims | 1189 | The polling endpoint - a small payload (no bid_history/full item) |
| `get_draft_sensitive_detail` | tournament_id, event, claims | 1215 | The privileged counterpart to the public GET /tournaments/{id}, |
| `build_tie` | squad_a_id, squad_b_id, matches_per_tie | 1253 | — |
| `build_tie_round_robin` | squad_ids, matches_per_tie | 1276 | — |
| `_bye_tie` | squad_id | 1284 | Mirrors _bye_match: auto-decided the instant it's created, no |
| `build_knockout_tie_round` | squad_ids, matches_per_tie | 1294 | Generalizes build_knockout_round: same power-of-2/byes-needed |
| `_update_tie_progress` | tie | 1319 | Recomputes wins_a/wins_b/point_diff_a/point_diff_b from the tie's |
| `_score_tie_match` | item, tie, match_index, score_a, score_b, ove | 1348 | Submits one individual match's score within a tie. Raises ValueError |
| `_find_tie` | item, tie_id | 1380 | A tie_id is a UUID unique across the whole tournament, so it can be |
| `_authorize_tie_scorer` | item, tie, claims | 1397 | Organizer, or one of THIS tie's own two squad leaders - matches the |
| `compute_squad_standings` | item, squad_ids | 1411 | Squad-level standings: sorted by (ties_won desc, aggregate point |
| `compute_player_tournament_scores` | item | 1446 | A tournament-scoped, non-Elo per-player score/leaderboard - a |
| `rename_squad` | tournament_id, event, claims | 1520 | Squads get an auto-generated name ("Team <leader>") the instant the |
| `move_squad_player` | tournament_id, event, claims | 1554 | Organizer-only roster rebalancing between two squads, before the |
| `substitute_squad_player` | tournament_id, event, claims | 1600 | Organizer-only real substitution for a manual-draft squad: swaps a |
| `generate_schedule` | tournament_id, event, claims | 1688 | — |
| `pick_tie_player` | tournament_id, event, claims | 1739 | A leader nominates which of their own squad's members plays a given |
| `_generate_knockout_from_group_stage` | item | 1841 | — |
| `_inject_group_tiebreakers_if_needed` | item | 1849 | Real-separate-groups sibling of the legacy groups_then_knockout |
| `_advance_squads_to_knockout_from_groups` | item | 1893 | Real-separate-groups sibling of the legacy groups_then_knockout |
| `record_group_tie_score` | tournament_id, event, claims | 1922 | — |
| `_advance_knockout_ties_if_round_complete` | item | 1963 | Mirrors record_knockout_score's round-advancement + third-place- |
| `record_knockout_tie_score` | tournament_id, event, claims | 1988 | — |
| `list_tournaments` | event | 2029 | — |
| `_redact_pool_auction_detail` | item | 2053 | GET /tournaments/{id} is unauthenticated - literally anyone browsing |
| `_hide_pool_auction_from_non_organizer` | item, claims | 2076 | pick_tie_player/record_group_tie_score/record_knockout_tie_score are |
| `get_tournament` | tournament_id | 2090 | — |
| `recompute_all_ratings` |  | 2115 | Elo is path-dependent - each match's rating change depends on the |
| `delete_tournament` | tournament_id, event | 2193 | Deletes this tournament AND every match record tagged with its |
| `compute_standings` | fixtures, entities | 2227 | — |
| `compute_all_standings` | item | 2259 | — |
| `_submit_game` | fixture, score_a, score_b, best_of, target, o | 2265 | Append one game's score to a fixture/match. Returns True if the match is now decided. |
| `record_group_score` | tournament_id, event | 2294 | — |
| `inject_tiebreakers_if_needed` | item | 2348 | Checks each subgroup for a genuine tie (same wins AND point_diff) at |
| `advance_to_knockout` | item | 2399 | — |
| `record_knockout_score` | tournament_id, event | 2424 | — |
| `compute_adaptive_k` | pairing_count | 2535 | Higher K for a fresh/novel doubles pairing (each match together is |
| `get_pairing_count` | team_ids | 2549 | How many prior doubles matches has this exact 2-player team played |
| `update_elo_and_log` | match_type, entity_a, entity_b, score_a, scor | 2567 | — |
| `substitute_player` | tournament_id, event | 2645 | Swap a player out of a team for all of that team's FUTURE (unplayed) |
| `_response` | status_code, body_dict | 2738 | — |

#### `finance` — 1571 LOC
_NetWorth - finance Lambda_

**Module constants:** `GROUPS_TABLE`, `DEFAULT_GROUP_NAME`, `GROUP_SLOT`, `VIEW_KEY`, `CONFIRMATION_CODE`, `MONTHS`, `FINANCE_LEVELS`, `ALLOWED_FIELDS`, `NUMERIC_FIELDS`, `REQUIRED_FIELDS`, `DEFAULT_CLUB_UTC_OFFSET_MINUTES`, `AVG_GAMES_PER_SESSION`, `SESSION_RATE`, `ACTIVE_DAYS_THRESHOLD`, `SLOT_GRACE_MINUTES`, `ASSUMED_MINUTES_PER_GAME`, `DEFAULT_ASSUMED_MINUTES_PER_GAME`, `DENSITY_FLAG_RATIO`, `_SLOT_LABEL_RE`

| Function | Args | Line | What it does |
|---|---|---|---|
| `_scan_all` | table | 76 | Full-table scan that follows LastEvaluatedKey - a bare .scan() returns |
| `_caller_claims` | event | 104 | Claims API Gateway's Cognito Authorizer attaches to the request. |
| `_is_super_admin` | claims | 112 | — |
| `_finance_role` | claims | 126 | — |
| `_finance_level` | claims | 142 | — |
| `_has_finance_access` | claims | 146 | View or better - the gate for reading finance at all. |
| `_default_group_id` |  | 151 | The group_id of the 'Club (default)' group that the pre-migration |
| `_group_for_request` | params, body | 167 | The group_id this finance op targets. Falls back to the default group |
| `_group_finance_level` | claims, group_id | 174 | A caller's finance level (0-3) FOR A SPECIFIC GROUP. |
| `_slot_key` | slot | 200 | Normalize a record's slot for bucketing/comparison: a missing/blank |
| `_member_assigned_slots` | pid, group | 207 | The set of slots (raw, already-normalized strings) a player is |
| `_view_scope_slots` | claims, group_id, level | 216 | Stage 4c: a plain 'view'-level grant only sees their own assigned |
| `_has_any_group_finance` | claims | 242 | True if the caller has finance access in ANY group (owner/admin, or a |
| `_effective_finance_role` | claims, group_id | 257 | The role name to REPORT to the frontend for button visibility: the |
| `finance_key_for_caller` | event | 282 | Hands the shared view key to any caller with finance access - global |
| `set_finance_access` | event | 299 | SuperAdmin sets a player's finance role directly. |
| `handler` | event, context | 323 | — |
| `_scan_type` | record_type, group_id | 441 | — |
| `_num` | v, default | 452 | — |
| `_clean` | record_type, data | 475 | — |
| `_resolve_name` | pid_cache, player_id | 487 | — |
| `_prev_period` | month, year | 498 | — |
| `_next_period` | month, year | 503 | — |
| `_member_relief` | settlement, memberships, ident, month, year,  | 508 | Relief a member gets in (month, year): the previous month's residual. |
| `list_records` | record_type, params, group_id, scope_slots | 531 | — |
| `create_records` | record_type, body, group_id | 593 | — |
| `update_record` | record_type, record_id, body, group_id | 615 | — |
| `delete_record_enforced` | record_type, record_id, event | 678 | Triple-gated: SuperAdmin identity + FINANCE_VIEW_KEY + the existing |
| `delete_record` | record_type, record_id, body, group_id | 701 | — |
| `get_settings` |  | 718 | — |
| `put_settings` | body | 746 | — |
| `public_upi` |  | 775 | The pay card is shown to guests (they pay walk-in fees), so the UPI |
| `my_settlement` | claims, group_id | 783 | A single member's own dues in a group: for every (month, slot) where |
| `public_walkins` |  | 913 | — |
| `_settlement_rows` | group_id | 933 | Per (month, year, slot): the exact math from the Calculations sheet. |
| `summary` | group_id, scope_slots | 1097 | — |
| `_parse_slot_window` | label | 1138 | Best-effort parse of a free-form slot label ('7AM-8AM', '19:00-20:00', |
| `_local_minutes_of_day` | iso_ts, offset_minutes | 1174 | Convert a stored ISO-8601 UTC match timestamp to local minute-of-day |
| `_minute_in_window` | minute, window, grace_minutes | 1194 | Whether `minute` (local minute-of-day) falls inside `window` (start, |
| `_timing_checks` | matches, group, offset_minutes, target_ym | 1207 | Best-effort, non-authoritative diagnostics only (see module note |
| `insights` | group_id | 1287 | Per-member monthly economics, ghosts, and walk-in conversion. |
| `_response` | status_code, body_dict | 1562 | — |

#### `progress_scheduler` — 238 LOC
_NetWorth - progress_scheduler Lambda_

| Function | Args | Line | What it does |
|---|---|---|---|
| `_scan_all` | table | 33 | Full-table scan that follows LastEvaluatedKey - a bare .scan() returns |
| `get_group_member_ids` | group_id | 51 | The set of player_ids belonging to a group - used to decide WHO is |
| `_approve_closed_week_matches` | matches, today | 60 | Marks every match whose week has fully closed as approved=True. |
| `handler` | event, context | 92 | — |
| `compute_period_snapshot` | matches, period_start_dt, period_end_dt, memb | 139 | Rating change and match count for every player within a fixed, |
| `write_history_entry` | scope_label, group_id, period_name, period_st | 199 | — |
<!-- AUTOGEN:BACKEND END -->

---

## 6. Frontend function reference

<!-- AUTOGEN:FRONTEND START (regenerated by tools/generate_codebase_map.py — do not hand-edit below) -->
### Frontend (`frontend/js/app.js` — 9485 LOC, flat global script, ~423 functions)

_Loaded by `index.html` after an inline `<script>` defines the globals `API_BASE_URL`, `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, `UPI_ID`, `FINANCE_VIEW_KEY` placeholders. Functions live in global scope (not an IIFE); most are wired to `onclick=` in the HTML._


**Auth/token core (top of file)**  (from L0)
- `getAuthHeaders()` — L9
- `isLoggedIn()` — L12

**Token freshness & authedFetch**  (from L14)
- `tokenSecondsRemaining()` — L28
- `ensureFreshToken(force = false)` — L33
- `authedFetch(url, options = {})` — L67
- `send()` — L68
- `describeApiError(res, data)` — L115
- `isSuperAdmin()` — L122
- `myPlayerId()` — L127
- `hasLinkedPlayer()` — L141
- `myRoleInGroup(group)` — L151

**Nickname/name display toggle**  (from L154)
- `canManageGroup(group)` — L157
- `ownsAnyGroup()` — L165
- `canReviewRequests()` — L168
- `updateReviewTabScope()` — L175
- `formatPlayerLabel(name, nickname)` — L198
- `toggleDisplayMode()` — L203

**Data-load helpers**  (from L226)
- `_nwModal({ message, input, defaultValue, okText, )` — L255
- `cleanup(val)` — L274
- `onKey(e)` — L296
- `nwConfirm(message, opts = {})` — L306
- `nwAlert(message, opts = {})` — L309
- `nwPrompt(message, defaultValue = '', opts = {})` — L312
- `populateSelect(selectEl, items, valueKey, labelKey, pla)` — L323
- `bumpMatchesRev()` — L351
- `isTabActive(tab)` — L352
- `ensureFresh(key, loader)` — L357
- `ensureOnce(key, loader)` — L363
- `loadStatsBundle()` — L369
- `makeCardsCollapsible(containerId)` — L399
- `setOpen(open)` — L414
- `makeStatsCollapsible()` — L419
- `makeFinanceCollapsible()` — L426
- `ensureProfileFresh()` — L431
- `loadActiveTabData()` — L440
- `myPlayerRecord()` — L450
- `iAmPrivate()` — L451
- `privateHiddenIds()` — L454
- `renderPrivacyControl()` — L458
- `toggleMyPrivacy()` — L478
- `setPrivacyMode(value)` — L501

**Live point-by-point scoring**  (from L510)
- `setPrivacyCooldown(value)` — L515
- `statsFetch(query)` — L529
- `populateAdminPrivacySelect()` — L535
- `adminSetPrivacy(makePrivate)` — L545
- `populateAdminRenameSelect()` — L568
- `adminRenamePlayer()` — L578

**Split-screen live scoring**  (from L591)
- `seasonMedallion(rank, size)` — L605
- `seasonBadgeSvg(kind, rank, size)` — L619
- `loadPlayerSeasons(playerId)` — L635
- `loadSeasonsMeta()` — L662
- `loadSeasonBoard(seasonId)` — L684

**Player registration**  (from L711)
- `renderSeasonAdmin()` — L715
- `saveSeasons(list, statusElId, okMsg)` — L724
- `addSeason()` — L735
- `deleteSeason(id)` — L749
- `setSeasonsEnabled(value)` — L753
- `setSeasonK(value)` — L764
- `loadPlayers()` — L772

**Delete / edit player**  (from L776)
- `loadGroups()` — L795

**Groups**  (from L850)
- `loadGroupMembers(groupId)` — L858
- `opt(v, label)` — L910
- `opt(v, label)` — L934
- `nameOf(pid)` — L947

**Matches (record/list/game-log)**  (from L979)
- `applyGroupDefaultsToForm(prefix, settings)` — L982
- `setIfPresent(suffix, value)` — L984
- `renderAddPlayersChecklist()` — L995
- `removePlayerFromGroup(groupId, playerId)` — L1008

**Voice match entry**  (from L1016)
- `populateTeamSelects()` — L1043
- `refreshTeamSelectOptions()` — L1077
- `syncTeamSelectValues()` — L1091
- `handleTeamSelectChange(changedId)` — L1100
- `applyMatchTypeVisibility()` — L1114
- `updateMatchGroupCache()` — L1127
- `randomizeTeams(showAlertOnFail)` — L1146
- `isGameOver(a, b, target)` — L1184
- `updateLiveScoreDisplay()` — L1192
- `getTeamDisplayName(selectId)` — L1262

**Team pairing preview**  (from L1264)
- `getSplitTeamNames()` — L1268
- `updateSplitScreenScores(a, b, over)` — L1283
- `openSplitScreenGeneric(config)` — L1292
- `closeSplitScreen()` — L1301
- `openSplitScreen()` — L1307
- `openTournamentSplitScreen(matchKey, target, nameA, nameB, finishFn)` — L1332

**Unsaved-match safety net**  (from L1472)
- `prefillEditForm()` — L1486

**Game log & CSV export**  (from L1558)
- `myGroups()` — L1678
- `visibleGroupsForFilter()` — L1689
- `defaultMatchGroup()` — L1697
- `applyVoiceVisibility()` — L1713
- `nwPhon(s)` — L1720
- `nwLev(a, b)` — L1731
- `nwScorePlayer(token, p)` — L1738
- `nwMatchPlayerToken(tokenRaw)` — L1757
- `nwWordsToNums(t)` — L1772
- `nwParseMatchTranscript(raw)` — L1780
- `nwApplyParsedToForm(p)` — L1812
- `set(id, entry)` — L1816
- `nwVoicePreviewHtml(p)` — L1826
- `nwVoiceMatchInit()` — L1838
- `stopListening()` — L1867

**Profile card customization**  (from L1901)
- `nwSeeded(p)` — L1956
- `nwShuffle(a)` — L1957
- `nwPairingRefreshList()` — L1959
- `nwPairingUpdateCount()` — L1970
- `nwPairingRender()` — L1975
- `nwPairingInit()` — L2007
- `nameFor(pid)` — L2079
- `showMatchOutcome(ok, message)` — L2149
- `savePendingMatch(payload, meta)` — L2168
- `loadPendingMatch()` — L2172
- `clearPendingMatch()` — L2175
- `handleSessionExpired()` — L2182
- `ensureRestoreHost()` — L2205
- `offerPendingMatchRestore()` — L2214
- `loadGameLog()` — L2249
- `gameLogGoto(p)` — L2299
- `renderGameLog()` — L2301

**Quests**  (from L2326)
- `matchPermissions(m)` — L2358
- `matchGroupLabel(m)` — L2379
- `requestMatchChange(matchId, type, label, groupId, extra)` — L2384
- `editMatch(matchId, groupId)` — L2405
- `opts(sel)` — L2414
- `pickers(team, prefix)` — L2416

**Store & events admin**  (from L2430)
- `close()` — L2437
- `editMatchScore(matchId, currentScoreA, currentScoreB, e)` — L2462
- `deleteMatch(matchId, encLabel, groupId)` — L2498
- `downloadCSV(filename, rows)` — L2530
- `loadRankings()` — L2564
- `gp(p)` — L2590
- `fetchRatingHistory(playerId)` — L2635
- `loadVisiblePlayers(opts = {})` — L2646

**Image uploads**  (from L2765)
- `resolveBannerId(id)` — L2784
- `bgCss(id, url)` — L2858
- `updatePageBackground()` — L2863
- `applyPageBackground(player)` — L2874
- `renderProfileCardBanner(player)` — L2881

**Profile bundle / cards / charts**  (from L2918)
- `toggleHeaderMenu()` — L2925
- `openSettingsModal()` — L2939
- `loadFinanceAccessList()` — L2952
- `opt(v, label)` — L2970
- `setGroupFinanceRole(groupId, playerId, role)` — L2985
- `setGroupMemberRole(groupId, playerId, role, wasRole, isSelf)` — L3001
- `setFinanceRole(playerId, role)` — L3017
- `closeSettingsModal()` — L3028
- `renderSettingsPickers(player)` — L3032
- `swatch(field, id, css, selected)` — L3041
- `submitClaimRequest()` — L3070
- `checkApprovalStatus()` — L3095
- `recomputeNow()` — L3108
- `loadAppSettings()` — L3123
- `setXpPublic(value)` — L3153
- `setVoiceEnabled(value)` — L3168
- `setInstantCreate(value)` — L3183
- `loadQuests()` — L3196
- `_renderQuestRow(q)` — L3205
- `_hdr(t)` — L3229
- `claimQuest(questId)` — L3237
- `loadQuestsAdmin()` — L3253
- `saveQuest()` — L3274
- `deleteQuest(questId)` — L3296
- `loadStore()` — L3308
- `catOf(i)` — L3333
- `cardHtml(i)` — L3338
- `buyStoreItem(itemId)` — L3370
- `onStoreImagePick(input)` — L3384
- `loadStoreAdmin()` — L3392
- `onStoreTypeChange()` — L3420
- `onStoreEffectChange()` — L3432
- `uploadStoreImage(file)` — L3448
- `saveStoreItem()` — L3465
- `deleteStoreItem(itemId)` — L3515
- `loadEventsAdmin()` — L3526
- `editEvent(e)` — L3548
- `saveEvent()` — L3556
- `deleteEvent(eventId)` — L3579
- `refreshEventBanner()` — L3591
- `loadClaimAudit()` — L3608
- `relinkAccount(usernameEnc, presetPlayerId)` — L3675
- `unlinkAccount(usernameEnc)` — L3683
- `unlinkAndStrip(usernameEnc, playerId)` — L3688
- `_claimAuditAction(bodyObj)` — L3693
- `loadUnconfirmedUsers()` — L3704
- `deleteUnconfirmedUser(username, email)` — L3731
- `loadClaimRequests()` — L3744
- `decideClaimRequest(requestId, action, requestType)` — L3784
- `escapeHtml(s)` — L3822
- `resizeImage(file, kind)` — L3839
- `isAnimatedImage(file)` — L3874
- `uploadCardImage(kind, fileInput)` — L3886

**UPI payment card**  (from L3941)
- `imageSrc(key)` — L3941
- `loadStoreCatalogOnce()` — L3947
- `renderStoreCosmeticStrip(kind, player)` — L3957
- `renderUploadStrip(kind, player)` — L3981

**Finance tab (view-key + role gated)**  (from L3999)
- `vsPlayerVisual(pid, snapshot)` — L4013
- `vsAvatarHtml(v, isWinner)` — L4029
- `teamBanner(side)` — L4046
- `gameScore(game, side)` — L4057
- `renderVsCard(idsA, idsB, opts = {})` — L4063
- `won(side)` — L4068
- `vsSideIds(side)` — L4093
- `setMyCardField(field, value)` — L4103
- `loadProfileBundle(playerId)` — L4171
- `renderTieredCard(icon, name, unit, tiers, currentValue)` — L4286
- `renderBinaryCard(icon, name, desc, achieved, detail)` — L4314
- `resetRatingZoom()` — L4386
- `loadProfileRatingChart(playerId)` — L4392
- `loadProfilePartnershipsAndRadar(playerId)` — L4478
- `loadProfileHeadToHead(playerId)` — L4527
- `loadProfileWithPartner(playerId)` — L4551
- `partnerGamesGoto(p)` — L4583
- `renderPartnerGames()` — L4585
- `skeletonHTML(lines = 3)` — L4618
- `showProfileSkeletons()` — L4625
- `renderXpPanel(player)` — L4638
- `xpForLevel(n)` — L4646

**Match review & reorder (SuperAdmin)**  (from L4649)
- `updateHeaderCoins()` — L4672
- `loadProfile()` — L4684
- `refreshProfile()` — L4714
- `refreshProfileIfShowing(affectedPlayerIds)` — L4731
- `renderPartnerRadar(data, highlightTournament, svgId = 'rada)` — L4752

**Auth UI (Cognito login/signup/session)**  (from L4780)
- `loadHistory()` — L4808
- `renderHistory(data)` — L4826
- `loadBadges()` — L4887
- `renderBadges(data)` — L4905
- `loadDiversity()` — L4938
- `renderDiversity(data)` — L4956
- `playerLabelById(playerId, fallbackName)` — L4977
- `playerLabelsById(playerIds, fallbackNames)` — L4981
- `loadHallOfFame()` — L4987
- `renderHallOfFame(data)` — L5009
- `loadAttendance()` — L5093
- `renderAttendance(data)` — L5112
- `refreshUpiCard()` — L5131
- `renderUpiCard()` — L5143
- `imageServiceFallback()` — L5165
- `xpVisible()` — L5193
- `applyFinanceRoleVisibility()` — L5199
- `refreshFinanceRoleForGroup()` — L5230
- `finQS(extra)` — L5243
- `financeBaseUrl()` — L5254
- `finPost(path, method, bodyObj)` — L5258
- `populateFinanceSlots(group)` — L5282
- `_rememberedFinance(key)` — L5305
- `_rememberFinance(key, val)` — L5309
- `restoreFinanceMonth()` — L5315
- `populateFinanceGroups()` — L5324

**Init & session restore**  (from L5346)
- `reloadFinanceForGroup()` — L5352
- `tryAutoFinanceUnlock()` — L5357

**Tournaments**  (from L5371)
- `myFinanceGroups()` — L5387
- `populateMyDuesGroups()` — L5392
- `loadMyDues(groupId)` — L5410
- `manageGroupSlots(groupId)` — L5460
- `assignSlotMembers(groupId, slotEnc)` — L5478
- `transferGroupOwnership(groupId)` — L5506
- `setGroupPayee(groupId)` — L5525
- `requestFinanceAccess()` — L5548
- `financeUnlock()` — L5565
- `updateFinanceScopeNote(scopedTo)` — L5620
- `loadFinanceSummary()` — L5630
- `loadFinanceExpenses()` — L5664
- `resetExpenseEdit()` — L5702
- `addFinanceExpense()` — L5709
- `loadFinanceMembers()` — L5728
- `markMembersDirty()` — L5832
- `recalcMembers()` — L5839
- `renderBulkRosterList()` — L5851
- `bulkAddFromRoster()` — L5865
- `copyPreviousMonthMembers()` — L5880
- `addFinanceMember()` — L5916
- `resetWalkinEdit()` — L5940
- `loadFinanceWalkins()` — L5949
- `addFinanceWalkin()` — L5997
- `loadFinanceInsights()` — L6028
- `copyDuesForWhatsApp()` — L6042
- `pad(s, w)` — L6059
- `padL(s, w)` — L6060
- `line(n, o, r, p)` — L6061
- `done()` — L6071
- `fallbackCopy(text, cb)` — L6077
- `renderInsights()` — L6086
- `saveFinanceSettings()` — L6212
- `loadPublicWalkins()` — L6258
- `loadReviewDay()` — L6347
- `reviewOrderChanged()` — L6391
- `renderReviewList()` — L6397

**Live scoring inside tournaments**  (from L6421)
- `applyReviewOrder()` — L6458
- `updateAuthUI()` — L6486
- `hiddenNow(id, btn)` — L6514
- `refreshMySession(statusElId)` — L6599
- `setStatus(msg)` — L6600
- `openAchievementsModal()` — L6628
- `closeAchievementsModal()` — L6637
- `openAuthModal()` — L6638
- `closeAuthModal()` — L6639
- `showAuthView(view)` — L6640
- `setAuthSession(session, user, opts = {})` — L6648
- `closeCompleteProfileModal()` — L6668
- `openCompleteProfileModal()` — L6669
- `showCompleteProfileMode(mode, preselectPlayerId)` — L6684
- `populateClaimPicker(preselectPlayerId)` — L6692
- `submitClaimProfile()` — L6716
- `closeCompleteProfileModal()` — L6756
- `sanitizeNickname(raw)` — L6762
- `editDistance(a, b)` — L6767
- `checkForExistingPlayer(name, typedNickname, statusEl)` — L6789
- `submitCompleteProfile()` — L6844
- `finishRequestAndSignOut(message)` — L6920
- `doLogin()` — L6926
- `doNewPassword()` — L6979
- `doSignup()` — L6990
- `doConfirmSignup()` — L7007
- `doResendConfirmCode()` — L7038
- `doForgotPassword()` — L7049
- `doConfirmForgotPassword()` — L7064
- `doLogout()` — L7076
- `restoreSession()` — L7120
- `restoreTabFromHash()` — L7164
- `addManualTeamRow()` — L7239
- `collectManualTeams()` — L7275
- `loadTournamentGroupOptions()` — L7288
- `loadTournamentParticipantsChecklist()` — L7297
- `updateParticipantsCount()` — L7327
- `collectTournamentParticipants()` — L7339
- `loadTournamentsList()` — L7343
- `submitTournamentCreation(payload)` — L7350
- `submitManualDraftCreation(group_id, name)` — L7376
- `draftPlayerName(pid)` — L7415
- `draftEveryone(t)` — L7420
- `renderManualDraftTournament(t)` — L7426
- `fetchTournamentDetail(tournamentId)` — L7494
- `fetchAndRenderTournamentDetail(tournamentId)` — L7510
- `renderDraftLeaderPicker(t)` — L7520
- `saveDraftLeaders(tournamentId)` — L7540
- `renderDraftPoolBoard(t)` — L7550
- `chip(pid)` — L7555
- `draftChipTapped(pid, ev)` — L7602
- `draftPoolColumnTapped(tournamentId, poolName)` — L7609
- `draftChipDragStart(ev, pid)` — L7616
- `draftPoolDragOver(ev)` — L7621
- `draftPoolDrop(ev, tournamentId, poolName)` — L7626
- `moveDraftPlayerToPool(tournamentId, poolName, playerId)` — L7635
- `putDraftPool(tournamentId, poolName, playerIds)` — L7655
- `addNewDraftPlayer(tournamentId, groupId)` — L7665
- `removeDraftPlayer(tournamentId, playerId)` — L7691
- `lockDraftPools(tournamentId)` — L7704
- `stopDraftPolling()` — L7734
- `startDraftPolling(tournamentId)` — L7739
- `isDraftPollingActiveFor(tournamentId)` — L7752
- `pollDraftStateTick(tournamentId)` — L7754
- `draftDecidedIds(draft)` — L7765
- `renderDraftStartAuctionPanel(t)` — L7774
- `startDraftAuction(tournamentId)` — L7783
- `renderDraftAuctionRoom(t)` — L7793
- `draftAssignEligibleLeaders(t, pool)` — L7808
- `draftAssignLeaderOptionsHtml(t, pool)` — L7818
- `updateDraftAssignLeaderOptions()` — L7824
- `renderDraftOrganizerAssignPanel(t)` — L7833
- `organizerAssignPlayer(tournamentId)` — L7858
- `renderDraftLiveStatusHtml(tournamentId, draftLike)` — L7879
- `updateDraftLiveStatus(tournamentId, draftLike)` — L7914
- `renderDraftQueuePicker(t)` — L7927
- `openDraftLot(tournamentId, playerId)` — L7954
- `closeDraftLot(tournamentId)` — L7962
- `skipDraftLot(tournamentId)` — L7972
- `renderDraftBidBox()` — L7982
- `draftBidBump(delta)` — L7995
- `submitDraftBid(tournamentId)` — L8002
- `renderDraftSquadsReview(t)` — L8031
- `generateDraftSchedule(tournamentId)` — L8045
- `renderSquadRosterEditPanel(t, allowMove)` — L8066
- `renameSquadPrompt(tournamentId, squadId)` — L8121
- `moveSquadPlayer(tournamentId)` — L8138
- `substituteSquadPlayer(tournamentId)` — L8152
- `draftSquadName(t, squadId)` — L8181
- `renderDraftScheduleView(t)` — L8186
- `renderSquadStandingsTable(standings)` — L8220
- `renderPlayerTournamentStatsTable(stats)` — L8232
- `renderTieSection(title, ties, t, stageKind)` — L8245
- `renderTieCard(tie, t, stageKind)` — L8252
- `renderTieMatchRow(tie, m, idx, t, stageKind, iLeadA, iLead)` — L8271
- `draftPlayerPickerHtml(tournamentId, tieId, matchIndex, members)` — L8308
- `opts(selected)` — L8314
- `pickTiePlayer(tournamentId, tieId, matchIndex, playerI)` — L8332
- `pickTiePlayerPair(tournamentId, tieId, matchIndex)` — L8342
- `submitDraftTieScore(tournamentId, tieId, matchIndex, stageKi)` — L8356
- `collectAllEntities(t)` — L8517
- `getAllTeamEntities(t)` — L8533
- `renderTeamCompositionBars(t, containerId)` — L8551
- `populateSubstitutionSection(t)` — L8586
- `updateSubOldPlayerOptions()` — L8597
- `formatGames(games)` — L8686
- `applyTournamentViewMode()` — L8693
- `matchTotals(match)` — L8699
- `truncateBracketName(name, maxChars = 22)` — L8707
- `renderBracketView(t)` — L8712
- `renderTournament(t)` — L8828
- `generateTournamentRecap(t)` — L9003
- `downloadTournamentImage()` — L9035
- `loadImg(src)` — L9062
- `sideVisuals(side)` — L9072
- `drawCard(x, y, w, match, isFinal)` — L9079
- `drawAvatars(ctx, x, y, side, isWinner)` — L9125
- `paintTeam(ctx, x, y, w, h, side, fallback)` — L9144
- `roundRect(ctx, x, y, w, h, r)` — L9172
- `copyTournamentRecap()` — L9182
- `item_has_third_place(t)` — L9193
- `submitGroupScore(tournamentId, subgroup, fixtureId)` — L9197
- `submitGroupScoreDirect(tournamentId, subgroup, fixtureId, score)` — L9203
- `submitKnockoutScore(tournamentId, roundIndex, matchIndex)` — L9222
- `submitKnockoutScoreDirect(tournamentId, roundIndex, matchIndex, sc)` — L9228
- `submitThirdPlaceScore(tournamentId)` — L9247
- `submitThirdPlaceScoreDirect(tournamentId, score_a, score_b, override)` — L9253
- `getTournamentLiveLog(matchKey)` — L9276
- `tournamentLivePoint(matchKey, side, target)` — L9281
- `tournamentUndoPoint(matchKey, target)` — L9290
- `updateTournamentLiveDisplay(matchKey, target)` — L9296
- `finishGroupLiveGame(matchKey, tournamentId, subgroup, fixtur)` — L9314
- `finishKnockoutLiveGame(matchKey, tournamentId, roundIndex, matc)` — L9323
- `finishThirdPlaceLiveGame(matchKey, tournamentId)` — L9332
- `renderLiveScoreControls(matchKey, target, finishCallExpr, nameA,)` — L9341
- `activateTab(tabName)` — L9365
- `jumpToRecordMatch()` — L9460
- `applyTheme(theme)` — L9466
<!-- AUTOGEN:FRONTEND END -->

---

## 7. Ops scripts (`scripts/*.py`)

One-off maintenance/backfill tools (run locally with AWS creds; most are idempotent-ish but
read the top comment first). CSV inputs (`player_emails.csv`, `group_owner_overrides.csv`)
are gitignored where they contain PII.

| Script | Purpose |
|---|---|
| `add_third_place_match.py` | One-time: add a missing 3rd-place match to a tournament |
| `backfill_group_roles.py` | Backfill a `roles` map onto every existing group |
| `backfill_member_ratings.py` | Backfill member ratings for old tournaments |
| `backfill_nicknames.py` | Make nickname the unique player identifier |
| `backfill_progress_history.py` | Reconstruct historical progress-badge snapshots |
| `claim_audit.py` | Audit (and optionally repair) Cognito↔player claim linkage |
| `clear_bogus_momentum.py` | Fix a match with a bogus live-scoring point log |
| `list_matches.py` | Print all matches, date-sorted, human-readable |
| `provision_cognito_users.py` | Bulk-create a Cognito account per existing player |
| `reconstruct_july19_tournament.py` | One-time reconstruction of the July-19 tournament |
| `rename_player_history.py` | Correct a player's name in historical records |
| `repair_ratings_after.py` | Fix stale `ratings_after` snapshots |
| `backfill_finance_groups.py` | Group-scoped finance Stage 1: create "Club (default)" group + stamp `group_id` on finance records (dry-run default, idempotent) |
| `seed_finance_from_excel.py` | Seed finance table from the court-expenses xlsx |
| `set_group_owner.py` | Force-set a group's owner |
| `tag_july19_matches.py` | Tag the July-19 tournament's 15 matches |

---

## 8. Frontend ↔ backend coupling notes (read before editing either side)

- `app.js` is a **flat global script** (not an IIFE); nearly every function is invoked from
  `onclick=`/`onchange=` attributes inside `index.html`. Renaming a JS function means grepping
  `index.html` too.
- Config globals (`API_BASE_URL`, `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, and the UPI/
  finance-key placeholders) are declared by an **inline `<script>` in `index.html`** and read
  as globals by `app.js`. The deploy workflow `sed`-injects real values into `index.html` only.
- `authedFetch()` is the single choke-point for authenticated calls — it refreshes a near-expired
  Cognito token (5-min margin) before sending. Do not bypass it with raw `fetch` on secure routes.
- `_response()` in every Lambda sets CORS headers. If CloudFormation fails to refresh the API GW
  stage, integrations return **gateway-level 500s with NO CORS headers** — fix with
  `aws apigateway create-deployment --rest-api-id zywd1pvlm6 --stage-name prod` (the deploy
  workflow already does this as a dedicated step).
- Nickname rule (`sanitize_nickname`) is duplicated in 3 lambdas AND in `app.js` (`sanitizeNickname`)
  — keep all four in sync or logins/claims silently diverge.
