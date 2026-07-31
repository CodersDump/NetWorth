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
| `networth-tournaments` | `tournament_id` (HASH) | fixtures/brackets, entities, standings, format, `group_id` |
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
- Auth split across the API: ~54 method-resources are `NONE`, ~37 are `COGNITO_USER_POOLS`.
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

#### `register_player` — 98 LOC
_NetWorth - register_player Lambda_

| Function | Args | Line | What it does |
|---|---|---|---|
| `sanitize_nickname` | raw | 23 | Hard format rule: lowercase, alphanumeric + underscore only. |
| `_caller_claims` | event | 32 | — |
| `handler` | event, context | 36 | — |
| `_response` | status_code, body_dict | 89 | — |

#### `players` — 1399 LOC
_NetWorth - players Lambda (list all, update one, delete one)_

**Module constants:** `CLAIM_REQUESTS_TABLE`, `USER_POOL_ID`, `UPLOADS_BUCKET`, `CONFIRMATION_CODE`, `ALLOWED_AVATARS`, `ALLOWED_BANNERS`, `ALLOWED_BACKGROUNDS`, `_APP_SETTINGS_ID`, `_STORE_CATALOG_ID`, `_STORE_ITEM_TYPES`, `FINANCE_LEVELS`, `ALLOWED_UPLOAD_TYPES`, `UPLOAD_KINDS`, `MAX_UPLOADS_PER_KIND`, `FREE_RENAMES`

| Function | Args | Line | What it does |
|---|---|---|---|
| `sanitize_nickname` | raw | 33 | Same rule as register_player's version (duplicated on purpose - |
| `handler` | event, context | 42 | — |
| `lookup_email_for_login` | identifier | 89 | Resolves a player_id, exact name, or exact nickname to the email |
| `list_players` |  | 124 | — |
| `_caller_claims` | event | 161 | — |
| `_can_self_rename` | claims | 165 | Placeholder gate - the achievement/level system this is meant to |
| `claim_player` | event | 190 | Self-service: link my Cognito account to an EXISTING, UNCLAIMED |
| `_is_super_admin` | claims | 258 | — |
| `_linked_player_is_live` | claims | 265 | True only if the caller's custom:player_id resolves to a player that |
| `create_claim_request` | event | 275 | Anyone logged in but not yet linked can ASK to be linked to an |
| `list_claim_requests` | event | 321 | — |
| `create_action_request` | event | 334 | A non-SuperAdmin asking for a destructive action instead of doing |
| `_create_new_profile_request` | claims, body | 401 | Creating a brand-new profile. By default this is a REQUEST an admin |
| `_get_app_setting` | key, default | 484 | App-wide flags live in one reserved row of the players table, keyed |
| `get_app_settings` | event | 493 | — |
| `set_app_setting` | event | 502 | — |
| `_load_catalog` |  | 527 | — |
| `list_store` | event | 532 | Public read - anyone can browse the store. Returns the catalog. |
| `save_store_item` | event | 538 | — |
| `delete_store_item` | event | 573 | — |
| `purchase_store_item` | event | 586 | A player spends coins on an item. Coins are deducted by bumping |
| `_create_edit_name_request` | claims, body | 631 | Renaming is now self-service-only: the target is always the |
| `_approve_edit_name` | req, claims | 677 | — |
| `_approve_new_profile` | req, claims | 693 | Creates the player only at approval time, and links it to the |
| `_create_match_request` | claims, body, action_type | 729 | A match edit or delete, filed as a request rather than executed. The |
| `_create_finance_access_request` | claims, body | 777 | A member asking for a finance role (view / write / delete). Approving |
| `_approve_finance_access` | req | 812 | — |
| `decide_claim_request` | event | 822 | Approve or reject. On approval this writes the link on BOTH sides: |
| `create_upload_url` | event | 965 | Hands back a short-lived presigned PUT. The browser uploads straight |
| `_valid_upload_key` | value, player_id, kind | 1029 | An uploaded image is referenced by key, and the key is checked |
| `_owns_store_cosmetic` | player, key, kind | 1039 | True if `key` is the image of a store cosmetic the player OWNS whose |
| `_rotate_uploads` | player_id, kind, new_key | 1064 | Maintains the player's short list of custom images, newest first, |
| `update_my_card` | event | 1096 | Self-service avatar/banner customization for the CALLER'S OWN |
| `_consume_perk` | player, player_id, effect_kind | 1192 | Spends one token of a perk the player owns (by store item effect |
| `rename_self` | event | 1221 | Self-service nickname change for the CALLER'S OWN linked player. |
| `update_player` | player_id, event | 1261 | — |
| `delete_player` | player_id, event | 1333 | — |
| `_cognito_username_for_email` | cognito, email | 1382 | The username is not always the email, so it has to be looked up. |
| `_response` | status_code, body_dict | 1390 | — |

#### `groups` — 601 LOC
_NetWorth - groups Lambda_

**Module constants:** `CONFIRMATION_CODE`, `VALID_ROLES`

| Function | Args | Line | What it does |
|---|---|---|---|
| `sanitize_nickname` | raw | 42 | Same rule as register_player's version (duplicated - separate |
| `handler` | event, context | 53 | — |
| `_authorize_group_action` | group_id, claims | 111 | Shared check for Epic 4's group-scoped write actions: SuperAdmin, or |
| `delete_group_enforced` | group_id, event | 127 | Dual-gated (Epic 4 increment 3): a valid Cognito identity that's |
| `remove_player_enforced` | group_id, player_id, event | 139 | Same dual-gate as delete_group_enforced, for member removal. |
| `_requires_linked_member` | claims | 147 | Signing up is not the same as being a member. Cognito self-signup is |
| `register_and_join` | event | 170 | Combined 'register a friend' + 'quick-add during match setup' |
| `add_player_enforced` | group_id, event | 254 | Requires SuperAdmin, or already owner/admin of THIS group - reuses |
| `create_group_enforced` | event | 263 | Requires a valid Cognito login (any authenticated account - no |
| `_consume_extra_group_perk` | player_id | 297 | Spend one extra_group token if the player owns one. Mirrors the |
| `visible_players_for_caller` | event | 321 | For populating the Profile tab's player picker: SuperAdmin gets |
| `create_group` | event | 380 | — |
| `list_groups` |  | 404 | — |
| `get_group` | group_id | 419 | — |
| `update_group_defaults` | group_id, event | 440 | Save a group's default tournament creation settings (format, points, |
| `delete_group` | group_id, event | 461 | Deletes only the group record itself. Player records are never |
| `add_player` | group_id, event | 475 | — |
| `remove_player` | group_id, player_id, event | 517 | — |
| `_caller_claims` | event | 539 | Claims API Gateway's Cognito Authorizer attaches to the request. |
| `_is_super_admin` | claims | 547 | — |
| `set_role` | group_id, player_id, event | 552 | Set (or change) a member's role within this group. |
| `_response` | status_code, body_dict | 592 | — |

#### `matches` — 2127 LOC
_NetWorth - matches Lambda (singles + doubles)_

**Module constants:** `K_FACTOR`, `XP_PLAYED`, `XP_WIN_BONUS`, `XP_TOURNAMENT_WIN`, `XP_MARGIN_PER_POINTS`, `XP_MARGIN_CAP`, `XP_LEVEL_COEFF`, `COINS_PER_LEVEL`, `_EVENTS_ROW_ID`, `_QUESTS_ROW_ID`, `QUEST_TYPES`, `COMEBACK_BONUS_THRESHOLD`, `COMEBACK_BONUS_PER_POINT`, `COMEBACK_BONUS_CAP`, `CONFIRMATION_CODE`

| Function | Args | Line | What it does |
|---|---|---|---|
| `level_from_xp` | xp | 77 | Inverse of xp = 5*N^2, floored: the highest level fully paid for by |
| `xp_for_level` | level | 86 | Total XP needed to reach a given level - used for progress bars. |
| `xp_for_match` | stage, won, margin | 91 | Base XP a single player earns for one match (before any event |
| `_load_quests` |  | 127 | — |
| `_week_bounds_utc` | now | 132 | Monday 00:00 (inclusive) to next Monday (exclusive), as ISO date |
| `_evaluate_quest` | quest, player_id, week_matches, player_rating | 142 | Returns how many times the player has satisfied this quest's condition |
| `list_quests` | event | 187 | Returns this week's quests with the caller's progress and claim state. |
| `save_quest` | event | 227 | — |
| `delete_quest` | event | 256 | — |
| `claim_quest` | event | 269 | Player claims a completed quest's reward. Verified server-side against |
| `_load_events` |  | 323 | — |
| `event_multiplier_for_date` | date_str, events | 327 | The XP multiplier active on a given match date (default 1.0). Pass a |
| `display_name` | player_item, fallback | 348 | Single source of truth for name formatting: 'Nickname (Real Name)' |
| `compute_comeback_bonus` | momentum | 365 | Extra rating-point bonus for the winning side, on top of the |
| `_is_valid_completed_game` | score_a, score_b, target | 380 | BWF-style badminton scoring: first to `target` points wins, but must lead |
| `_caller_claims` | event | 397 | Claims API Gateway's Cognito Authorizer attaches to the request. |
| `_is_super_admin` | claims | 403 | — |
| `_can_view_profile` | claims, target_player_id | 408 | SuperAdmin sees everyone. Anyone can view their own profile. A |
| `_requires_linked_member` | claims | 428 | Signing up is not the same as being a member. Cognito self-signup is |
| `record_match_enforced` | event | 451 | — |
| `profile_view_enforced` | event | 461 | Entry point for the isolated /profile-secure/{proxy+} catch-all. |
| `handler` | event, context | 483 | — |
| `list_events` | event | 546 | Public read - the frontend shows an active-event banner to everyone. |
| `save_event` | event | 554 | SuperAdmin creates or updates an event (upsert by event_id). |
| `delete_event` | event | 584 | — |
| `recompute_now` | event | 597 | SuperAdmin-only: replay every match to rebuild ratings, XP, levels |
| `reorder_matches` | event | 607 | Reorders a set of matches by reassigning their timestamps. |
| `record_match` | event | 675 | — |
| `update_match` | match_id, event | 716 | Fix a mis-entered score on an already-recorded standalone match. |
| `delete_match` | match_id, event | 753 | Permanently delete a mis-recorded match - e.g. the wrong player was |
| `recompute_all_ratings` |  | 774 | Elo is path-dependent - each match's rating change depends on the |
| `compute_momentum_stats` | point_log, winner | 895 | Longest scoring streak per team, and how big a deficit the winner overcame. |
| `compute_adaptive_k` | pairing_count | 942 | Higher K for a fresh/novel doubles pairing (each match together is |
| `get_pairing_count` | team_ids, exclude_match_id | 958 | How many prior doubles matches has this exact 2-player team played |
| `_play_and_log` | match_type, team_a_ids, team_b_ids, score_a,  | 978 | — |
| `list_matches` | event | 1074 | — |
| `compute_partnerships` | player_id, items | 1153 | For a given player, tally win/loss record with each doubles partner |
| `get_group_member_ids` | group_id | 1195 | The set of player_ids belonging to a group, used to filter WHO shows |
| `compute_attendance` | items, group_id_filter | 1206 | Per-player attendance/consistency: total matches, distinct calendar |
| `compute_hall_of_fame` | items, group_id_filter | 1274 | Highlight stats computed from full chronological match history: |
| `compute_achievements` | player_id, matches, tournaments | 1595 | Milestone/tiered achievement progress for one player: total matches |
| `compute_top_opponents` | player_id, matches, top_n | 1713 | This player's win/loss record against every opponent they've ever |
| `compute_overall_record` | player_id, matches | 1755 | This player's total win/loss record, split by singles and doubles. |
| `compute_head_to_head` | player_id, opponent_id, matches | 1784 | One player's win/loss record specifically as an OPPONENT of another |
| `compute_recent_form` | player_id, matches, limit | 1816 | A player's last N matches, in chronological order (oldest to |
| `compute_diversity` | items, group_id_filter | 1873 | For every player: how concentrated their doubles partnerships are. |
| `compute_progress_history_summary` | scope_label, period_name | 1918 | Reads the permanent, locked-in weekly/monthly/yearly winner history |
| `compute_progress_badges` | items, group_id_filter | 1995 | For each of the last week/month/year: who improved their rating the |
| `compute_partner_distribution` | player_id, items, top_n | 2071 | For the radar/spider chart: one player's doubles partners, sorted by |
| `_response` | status_code, body_dict | 2118 | — |

#### `tournaments` — 1059 LOC
_NetWorth - tournaments Lambda (singles or doubles)_

**Module constants:** `K_FACTOR`, `COMEBACK_BONUS_THRESHOLD`, `COMEBACK_BONUS_PER_POINT`, `COMEBACK_BONUS_CAP`, `CONFIRMATION_CODE`

| Function | Args | Line | What it does |
|---|---|---|---|
| `compute_comeback_bonus` | momentum | 46 | Extra rating-point bonus for the winning side, on top of the |
| `compute_momentum_stats` | point_log, winner | 58 | Longest scoring streak per team, and how big a deficit the winner overcame. |
| `_is_valid_completed_game` | score_a, score_b, target | 106 | Same BWF-style rule as the standalone matches Lambda: win by 2 at |
| `_caller_claims` | event | 120 | Same pattern as matches lambda - see that file's comment for |
| `create_tournament_enforced` | event | 126 | — |
| `handler` | event, context | 132 | — |
| `seeded_order` | players | 173 | Sort by current rating, descending. New players just use their |
| `pair_for_balance` | ordered_players | 183 | Given a skill-ordered list, pair strongest with weakest (snake |
| `create_tournament` | event | 198 | — |
| `build_round_robin` | entities | 340 | — |
| `build_knockout_round` | entities | 357 | — |
| `_bye_match` | entity | 392 | — |
| `list_tournaments` | event | 408 | — |
| `get_tournament` | tournament_id | 432 | — |
| `recompute_all_ratings` |  | 441 | Elo is path-dependent - each match's rating change depends on the |
| `delete_tournament` | tournament_id, event | 519 | Deletes this tournament AND every match record tagged with its |
| `compute_standings` | fixtures, entities | 553 | — |
| `compute_all_standings` | item | 585 | — |
| `_submit_game` | fixture, score_a, score_b, best_of, target, o | 591 | Append one game's score to a fixture/match. Returns True if the match is now decided. |
| `record_group_score` | tournament_id, event | 620 | — |
| `inject_tiebreakers_if_needed` | item | 674 | Checks each subgroup for a genuine tie (same wins AND point_diff) at |
| `advance_to_knockout` | item | 725 | — |
| `record_knockout_score` | tournament_id, event | 750 | — |
| `compute_adaptive_k` | pairing_count | 861 | Higher K for a fresh/novel doubles pairing (each match together is |
| `get_pairing_count` | team_ids | 875 | How many prior doubles matches has this exact 2-player team played |
| `update_elo_and_log` | match_type, entity_a, entity_b, score_a, scor | 893 | — |
| `substitute_player` | tournament_id, event | 971 | Swap a player out of a team for all of that team's FUTURE (unplayed) |
| `_response` | status_code, body_dict | 1050 | — |

#### `finance` — 801 LOC
_NetWorth - finance Lambda_

**Module constants:** `VIEW_KEY`, `CONFIRMATION_CODE`, `MONTHS`, `FINANCE_LEVELS`, `ALLOWED_FIELDS`, `NUMERIC_FIELDS`, `REQUIRED_FIELDS`, `AVG_GAMES_PER_SESSION`, `SESSION_RATE`, `ACTIVE_DAYS_THRESHOLD`

| Function | Args | Line | What it does |
|---|---|---|---|
| `_caller_claims` | event | 75 | Claims API Gateway's Cognito Authorizer attaches to the request. |
| `_is_super_admin` | claims | 83 | — |
| `_finance_role` | claims | 97 | — |
| `_finance_level` | claims | 113 | — |
| `_has_finance_access` | claims | 117 | View or better - the gate for reading finance at all. |
| `finance_key_for_caller` | event | 122 | Hands the shared view key to any caller with view access or better, |
| `set_finance_access` | event | 133 | SuperAdmin sets a player's finance role directly. |
| `handler` | event, context | 157 | — |
| `_scan_type` | record_type | 264 | — |
| `_num` | v, default | 269 | — |
| `_clean` | record_type, data | 292 | — |
| `_resolve_name` | pid_cache, player_id | 304 | — |
| `list_records` | record_type, params | 315 | — |
| `create_records` | record_type, body | 356 | — |
| `update_record` | record_type, record_id, body | 376 | — |
| `delete_record_enforced` | record_type, record_id, event | 416 | Triple-gated: SuperAdmin identity + FINANCE_VIEW_KEY + the existing |
| `delete_record` | record_type, record_id, body | 439 | — |
| `get_settings` |  | 451 | — |
| `put_settings` | body | 460 | — |
| `public_upi` |  | 475 | The pay card is shown to guests (they pay walk-in fees), so the UPI |
| `public_walkins` |  | 483 | — |
| `_settlement_rows` |  | 501 | Per (month, year, slot): the exact math from the Calculations sheet. |
| `summary` |  | 577 | — |
| `insights` |  | 594 | Per-member monthly economics, ghosts, and walk-in conversion. |
| `_response` | status_code, body_dict | 792 | — |

#### `progress_scheduler` — 220 LOC
_NetWorth - progress_scheduler Lambda_

| Function | Args | Line | What it does |
|---|---|---|---|
| `get_group_member_ids` | group_id | 33 | The set of player_ids belonging to a group - used to decide WHO is |
| `_approve_closed_week_matches` | matches, today | 42 | Marks every match whose week has fully closed as approved=True. |
| `handler` | event, context | 74 | — |
| `compute_period_snapshot` | matches, period_start_dt, period_end_dt, memb | 121 | Rating change and match count for every player within a fixed, |
| `write_history_entry` | scope_label, group_id, period_name, period_st | 181 | — |
<!-- AUTOGEN:BACKEND END -->

---

## 6. Frontend function reference

<!-- AUTOGEN:FRONTEND START (regenerated by tools/generate_codebase_map.py — do not hand-edit below) -->
### Frontend (`frontend/js/app.js` — 6608 LOC, single IIFE, ~258 functions)

_Loaded by `index.html` after an inline `<script>` defines the globals `API_BASE_URL`, `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, `UPI_ID`, `FINANCE_VIEW_KEY` placeholders. All functions share one closure; most are wired to `onclick=` in the HTML._


**Auth/token core (top of file)**  (from L0)
- `getAuthHeaders()` — L9
- `isLoggedIn()` — L12

**Token freshness & authedFetch**  (from L14)
- `tokenSecondsRemaining()` — L28
- `ensureFreshToken(force = false)` — L33
- `authedFetch(url, options = {})` — L67
- `send()` — L68
- `describeApiError(res, data)` — L107
- `isSuperAdmin()` — L114
- `myPlayerId()` — L119
- `hasLinkedPlayer()` — L133
- `myRoleInGroup(group)` — L143
- `canManageGroup(group)` — L149

**Nickname/name display toggle**  (from L154)
- `formatPlayerLabel(name, nickname)` — L168
- `toggleDisplayMode()` — L173

**Data-load helpers**  (from L226)
- `populateSelect(selectEl, items, valueKey, labelKey, pla)` — L228
- `loadPlayers()` — L247
- `loadGroups()` — L270
- `loadGroupMembers(groupId)` — L306
- `applyGroupDefaultsToForm(prefix, settings)` — L341
- `setIfPresent(suffix, value)` — L343
- `renderAddPlayersChecklist()` — L354
- `removePlayerFromGroup(groupId, playerId)` — L367
- `populateTeamSelects()` — L396
- `refreshTeamSelectOptions()` — L422
- `applyMatchTypeVisibility()` — L445
- `updateMatchGroupCache()` — L459
- `randomizeTeams(showAlertOnFail)` — L478

**Live point-by-point scoring**  (from L510)
- `isGameOver(a, b, target)` — L515
- `updateLiveScoreDisplay()` — L523

**Split-screen live scoring**  (from L591)
- `getTeamDisplayName(selectId)` — L593
- `getSplitTeamNames()` — L599
- `updateSplitScreenScores(a, b, over)` — L614
- `openSplitScreenGeneric(config)` — L623
- `closeSplitScreen()` — L632
- `openSplitScreen()` — L638
- `openTournamentSplitScreen(matchKey, target, nameA, nameB, finishFn)` — L663

**Delete / edit player**  (from L776)
- `prefillEditForm()` — L817

**Matches (record/list/game-log)**  (from L979)
- `myGroups()` — L999
- `defaultMatchGroup()` — L1009

**Voice match entry**  (from L1016)
- `applyVoiceVisibility()` — L1025
- `nwPhon(s)` — L1032
- `nwLev(a, b)` — L1043
- `nwScorePlayer(token, p)` — L1050
- `nwMatchPlayerToken(tokenRaw)` — L1069
- `nwWordsToNums(t)` — L1084
- `nwParseMatchTranscript(raw)` — L1092
- `nwApplyParsedToForm(p)` — L1124
- `set(id, entry)` — L1128
- `nwVoicePreviewHtml(p)` — L1138
- `nwVoiceMatchInit()` — L1150
- `stopListening()` — L1179

**Team pairing preview**  (from L1264)
- `nwSeeded(p)` — L1268
- `nwShuffle(a)` — L1269
- `nwPairingRefreshList()` — L1271
- `nwPairingUpdateCount()` — L1282
- `nwPairingRender()` — L1287
- `nwPairingInit()` — L1319
- `nameFor(pid)` — L1391
- `showMatchOutcome(ok, message)` — L1460

**Unsaved-match safety net**  (from L1472)
- `savePendingMatch(payload, meta)` — L1479
- `loadPendingMatch()` — L1483
- `clearPendingMatch()` — L1486
- `handleSessionExpired()` — L1493
- `ensureRestoreHost()` — L1516
- `offerPendingMatchRestore()` — L1525

**Game log & CSV export**  (from L1558)
- `loadGameLog()` — L1560
- `matchPermissions(m)` — L1636
- `matchGroupLabel(m)` — L1657
- `requestMatchChange(matchId, type, label, groupId, extra)` — L1662
- `editMatchScore(matchId, currentScoreA, currentScoreB, e)` — L1678
- `deleteMatch(matchId, encLabel, groupId)` — L1713
- `downloadCSV(filename, rows)` — L1744
- `loadRankings()` — L1778
- `fetchRatingHistory(playerId)` — L1826
- `loadVisiblePlayers(opts = {})` — L1837

**Profile card customization**  (from L1901)
- `resolveBannerId(id)` — L1964
- `bgCss(id, url)` — L2038
- `updatePageBackground()` — L2043
- `applyPageBackground(player)` — L2054
- `renderProfileCardBanner(player)` — L2061
- `toggleHeaderMenu()` — L2105
- `openSettingsModal()` — L2119
- `loadFinanceAccessList()` — L2132
- `opt(v, label)` — L2150
- `setFinanceRole(playerId, role)` — L2163
- `closeSettingsModal()` — L2174
- `renderSettingsPickers(player)` — L2178
- `swatch(field, id, css, selected)` — L2187
- `submitClaimRequest()` — L2216
- `checkApprovalStatus()` — L2241
- `recomputeNow()` — L2254
- `loadAppSettings()` — L2268
- `setXpPublic(value)` — L2284
- `setVoiceEnabled(value)` — L2299
- `setInstantCreate(value)` — L2314

**Quests**  (from L2326)
- `loadQuests()` — L2327
- `claimQuest(questId)` — L2361
- `loadQuestsAdmin()` — L2377
- `saveQuest()` — L2398
- `deleteQuest(questId)` — L2419

**Store & events admin**  (from L2430)
- `loadStore()` — L2431
- `buyStoreItem(itemId)` — L2466
- `onStoreImagePick(input)` — L2480
- `loadStoreAdmin()` — L2488
- `onStoreTypeChange()` — L2509
- `onStoreEffectChange()` — L2521
- `uploadStoreImage(file)` — L2528
- `saveStoreItem()` — L2545
- `deleteStoreItem(itemId)` — L2591
- `loadEventsAdmin()` — L2602
- `editEvent(e)` — L2624
- `saveEvent()` — L2632
- `deleteEvent(eventId)` — L2655
- `refreshEventBanner()` — L2667
- `loadClaimRequests()` — L2682
- `decideClaimRequest(requestId, action, requestType)` — L2722
- `escapeHtml(s)` — L2760

**Image uploads**  (from L2765)
- `resizeImage(file, kind)` — L2777
- `isAnimatedImage(file)` — L2812
- `uploadCardImage(kind, fileInput)` — L2824
- `imageSrc(key)` — L2879
- `loadStoreCatalogOnce()` — L2885
- `renderStoreCosmeticStrip(kind, player)` — L2895

**Profile bundle / cards / charts**  (from L2918)
- `renderUploadStrip(kind, player)` — L2919
- `vsPlayerVisual(pid, snapshot)` — L2951
- `vsAvatarHtml(v, isWinner)` — L2967
- `teamBanner(side)` — L2984
- `gameScore(game, side)` — L2995
- `renderVsCard(idsA, idsB, opts = {})` — L3001
- `won(side)` — L3006
- `vsSideIds(side)` — L3031
- `setMyCardField(field, value)` — L3041
- `loadProfileBundle(playerId)` — L3109
- `renderTieredCard(icon, name, unit, tiers, currentValue)` — L3224
- `renderBinaryCard(icon, name, desc, achieved, detail)` — L3252
- `loadProfileRatingChart(playerId)` — L3317
- `loadProfilePartnershipsAndRadar(playerId)` — L3366
- `loadProfileHeadToHead(playerId)` — L3415
- `skeletonHTML(lines = 3)` — L3439
- `showProfileSkeletons()` — L3446
- `renderXpPanel(player)` — L3459
- `xpForLevel(n)` — L3467
- `updateHeaderCoins()` — L3493
- `loadProfile()` — L3505
- `refreshProfile()` — L3529
- `refreshProfileIfShowing(affectedPlayerIds)` — L3546
- `renderPartnerRadar(data, highlightTournament, svgId = 'rada)` — L3566
- `loadHistory()` — L3622
- `renderHistory(data)` — L3640
- `loadBadges()` — L3701
- `renderBadges(data)` — L3719
- `loadDiversity()` — L3752
- `renderDiversity(data)` — L3770
- `playerLabelById(playerId, fallbackName)` — L3791
- `playerLabelsById(playerIds, fallbackNames)` — L3795
- `loadHallOfFame()` — L3801
- `renderHallOfFame(data)` — L3823
- `loadAttendance()` — L3907
- `renderAttendance(data)` — L3926

**UPI payment card**  (from L3941)
- `refreshUpiCard()` — L3945
- `renderUpiCard()` — L3957
- `imageServiceFallback()` — L3979

**Finance tab (view-key + role gated)**  (from L3999)
- `xpVisible()` — L4007
- `applyFinanceRoleVisibility()` — L4013
- `finQS(extra)` — L4032
- `financeBaseUrl()` — L4042
- `finPost(path, method, bodyObj)` — L4046
- `tryAutoFinanceUnlock()` — L4061
- `requestFinanceAccess()` — L4083
- `financeUnlock()` — L4100
- `loadFinanceSummary()` — L4138
- `loadFinanceExpenses()` — L4159
- `resetExpenseEdit()` — L4197
- `addFinanceExpense()` — L4204
- `loadFinanceMembers()` — L4223
- `renderBulkRosterList()` — L4308
- `bulkAddFromRoster()` — L4322
- `copyPreviousMonthMembers()` — L4337
- `addFinanceMember()` — L4373
- `loadFinanceWalkins()` — L4394
- `addFinanceWalkin()` — L4421
- `loadFinanceInsights()` — L4459
- `renderInsights()` — L4473
- `saveFinanceSettings()` — L4545
- `loadPublicWalkins()` — L4582

**Match review & reorder (SuperAdmin)**  (from L4649)
- `loadReviewDay()` — L4654
- `reviewOrderChanged()` — L4698
- `renderReviewList()` — L4704
- `applyReviewOrder()` — L4753

**Auth UI (Cognito login/signup/session)**  (from L4780)
- `updateAuthUI()` — L4780
- `openAuthModal()` — L4856
- `closeAuthModal()` — L4857
- `showAuthView(view)` — L4858
- `setAuthSession(session, user, opts = {})` — L4866
- `openCompleteProfileModal()` — L4883
- `showCompleteProfileMode(mode, preselectPlayerId)` — L4898
- `populateClaimPicker(preselectPlayerId)` — L4906
- `submitClaimProfile()` — L4930
- `closeCompleteProfileModal()` — L4970
- `sanitizeNickname(raw)` — L4976
- `editDistance(a, b)` — L4981
- `checkForExistingPlayer(name, typedNickname, statusEl)` — L5003
- `submitCompleteProfile()` — L5058
- `finishRequestAndSignOut(message)` — L5134
- `doLogin()` — L5140
- `doNewPassword()` — L5193
- `doSignup()` — L5204
- `doConfirmSignup()` — L5221
- `doResendConfirmCode()` — L5252
- `doForgotPassword()` — L5263
- `doConfirmForgotPassword()` — L5278
- `doLogout()` — L5290
- `restoreSession()` — L5334

**Init & session restore**  (from L5346)
- `restoreTabFromHash()` — L5368

**Tournaments**  (from L5371)
- `addManualTeamRow()` — L5434
- `collectManualTeams()` — L5470
- `loadTournamentGroupOptions()` — L5483
- `loadTournamentParticipantsChecklist()` — L5492
- `updateParticipantsCount()` — L5522
- `collectTournamentParticipants()` — L5534
- `loadTournamentsList()` — L5538
- `submitTournamentCreation(payload)` — L5545
- `collectAllEntities(t)` — L5704
- `getAllTeamEntities(t)` — L5720
- `renderTeamCompositionBars(t, containerId)` — L5738
- `populateSubstitutionSection(t)` — L5773
- `updateSubOldPlayerOptions()` — L5784
- `formatGames(games)` — L5873
- `applyTournamentViewMode()` — L5880
- `matchTotals(match)` — L5886
- `truncateBracketName(name, maxChars = 22)` — L5894
- `renderBracketView(t)` — L5899
- `renderTournament(t)` — L6015
- `generateTournamentRecap(t)` — L6189
- `downloadTournamentImage()` — L6221
- `loadImg(src)` — L6248
- `sideVisuals(side)` — L6258
- `drawCard(x, y, w, match, isFinal)` — L6265
- `drawAvatars(ctx, x, y, side, isWinner)` — L6311
- `paintTeam(ctx, x, y, w, h, side, fallback)` — L6330
- `roundRect(ctx, x, y, w, h, r)` — L6358
- `copyTournamentRecap()` — L6368
- `item_has_third_place(t)` — L6379
- `submitGroupScore(tournamentId, subgroup, fixtureId)` — L6383
- `submitGroupScoreDirect(tournamentId, subgroup, fixtureId, score)` — L6389
- `submitKnockoutScore(tournamentId, roundIndex, matchIndex)` — L6408
- `submitKnockoutScoreDirect(tournamentId, roundIndex, matchIndex, sc)` — L6414

**Live scoring inside tournaments**  (from L6421)
- `submitThirdPlaceScore(tournamentId)` — L6433
- `submitThirdPlaceScoreDirect(tournamentId, score_a, score_b, override)` — L6439
- `getTournamentLiveLog(matchKey)` — L6462
- `tournamentLivePoint(matchKey, side, target)` — L6467
- `tournamentUndoPoint(matchKey, target)` — L6476
- `updateTournamentLiveDisplay(matchKey, target)` — L6482
- `finishGroupLiveGame(matchKey, tournamentId, subgroup, fixtur)` — L6500
- `finishKnockoutLiveGame(matchKey, tournamentId, roundIndex, matc)` — L6509
- `finishThirdPlaceLiveGame(matchKey, tournamentId)` — L6518
- `renderLiveScoreControls(matchKey, target, finishCallExpr, nameA,)` — L6527
- `applyTheme(theme)` — L6589
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
| `seed_finance_from_excel.py` | Seed finance table from the court-expenses xlsx |
| `set_group_owner.py` | Force-set a group's owner |
| `tag_july19_matches.py` | Tag the July-19 tournament's 15 matches |

---

## 8. Frontend ↔ backend coupling notes (read before editing either side)

- `app.js` is **one big IIFE** sharing a single closure; nearly every function is invoked from
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
