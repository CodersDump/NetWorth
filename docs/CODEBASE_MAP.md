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

#### `players` — 1566 LOC
_NetWorth - players Lambda (list all, update one, delete one)_

**Module constants:** `CLAIM_REQUESTS_TABLE`, `USER_POOL_ID`, `UPLOADS_BUCKET`, `GROUPS_TABLE`, `CONFIRMATION_CODE`, `ALLOWED_AVATARS`, `ALLOWED_BANNERS`, `ALLOWED_BACKGROUNDS`, `OWNER_DECIDABLE_TYPES`, `_APP_SETTINGS_ID`, `_STORE_CATALOG_ID`, `_STORE_ITEM_TYPES`, `FINANCE_LEVELS`, `ALLOWED_UPLOAD_TYPES`, `UPLOAD_KINDS`, `MAX_UPLOADS_PER_KIND`, `FREE_RENAMES`

| Function | Args | Line | What it does |
|---|---|---|---|
| `sanitize_nickname` | raw | 40 | Same rule as register_player's version (duplicated on purpose - |
| `handler` | event, context | 49 | — |
| `lookup_email_for_login` | identifier | 100 | Resolves a player_id, exact name, or exact nickname to the email |
| `list_players` |  | 135 | — |
| `_caller_claims` | event | 172 | — |
| `_can_self_rename` | claims | 176 | Placeholder gate - the achievement/level system this is meant to |
| `claim_player` | event | 201 | Self-service: link my Cognito account to an EXISTING, UNCLAIMED |
| `_is_super_admin` | claims | 269 | — |
| `_linked_player_is_live` | claims | 276 | True only if the caller's custom:player_id resolves to a player that |
| `create_claim_request` | event | 286 | Anyone logged in but not yet linked can ASK to be linked to an |
| `_caller_owned_group_ids` | claims | 342 | The set of group_ids where the caller's linked player is owner or admin. |
| `_player_group_ids` | player_id | 358 | Every group_id whose roles map contains this player. |
| `_owner_may_decide` | req, owned_group_ids | 366 | True if a group owner/admin (owning owned_group_ids) may act on req: |
| `list_unconfirmed_users` | event | 383 | SuperAdmin-only: Cognito accounts stuck in UNCONFIRMED (signed up but |
| `delete_unconfirmed_user` | event | 415 | SuperAdmin-only: delete a single UNCONFIRMED Cognito account by username. |
| `list_claim_requests` | event | 441 | — |
| `create_action_request` | event | 460 | A non-SuperAdmin asking for a destructive action instead of doing |
| `_create_new_profile_request` | claims, body | 527 | Creating a brand-new profile. By default this is a REQUEST an admin |
| `_get_app_setting` | key, default | 610 | App-wide flags live in one reserved row of the players table, keyed |
| `get_app_settings` | event | 619 | — |
| `set_app_setting` | event | 628 | — |
| `_load_catalog` |  | 653 | — |
| `list_store` | event | 658 | Public read - anyone can browse the store. Returns the catalog. |
| `save_store_item` | event | 664 | — |
| `delete_store_item` | event | 699 | — |
| `purchase_store_item` | event | 712 | A player spends coins on an item. Coins are deducted by bumping |
| `_create_edit_name_request` | claims, body | 757 | Renaming is now self-service-only: the target is always the |
| `_approve_edit_name` | req, claims | 803 | — |
| `_approve_new_profile` | req, claims | 819 | Creates the player only at approval time, and links it to the |
| `_create_match_request` | claims, body, action_type | 855 | A match edit or delete, filed as a request rather than executed. The |
| `_create_finance_access_request` | claims, body | 903 | A member asking for a finance role (view / write / delete) IN A GROUP. |
| `_approve_finance_access` | req | 956 | — |
| `decide_claim_request` | event | 982 | Approve or reject. On approval this writes the link on BOTH sides: |
| `create_upload_url` | event | 1132 | Hands back a short-lived presigned PUT. The browser uploads straight |
| `_valid_upload_key` | value, player_id, kind | 1196 | An uploaded image is referenced by key, and the key is checked |
| `_owns_store_cosmetic` | player, key, kind | 1206 | True if `key` is the image of a store cosmetic the player OWNS whose |
| `_rotate_uploads` | player_id, kind, new_key | 1231 | Maintains the player's short list of custom images, newest first, |
| `update_my_card` | event | 1263 | Self-service avatar/banner customization for the CALLER'S OWN |
| `_consume_perk` | player, player_id, effect_kind | 1359 | Spends one token of a perk the player owns (by store item effect |
| `rename_self` | event | 1388 | Self-service nickname change for the CALLER'S OWN linked player. |
| `update_player` | player_id, event | 1428 | — |
| `delete_player` | player_id, event | 1500 | — |
| `_cognito_username_for_email` | cognito, email | 1549 | The username is not always the email, so it has to be looked up. |
| `_response` | status_code, body_dict | 1557 | — |

#### `groups` — 648 LOC
_NetWorth - groups Lambda_

**Module constants:** `CONFIRMATION_CODE`, `VALID_ROLES`, `FINANCE_ROLE_LEVELS`

| Function | Args | Line | What it does |
|---|---|---|---|
| `sanitize_nickname` | raw | 42 | Same rule as register_player's version (duplicated - separate |
| `handler` | event, context | 53 | — |
| `_authorize_group_action` | group_id, claims | 115 | Shared check for Epic 4's group-scoped write actions: SuperAdmin, or |
| `delete_group_enforced` | group_id, event | 131 | Dual-gated (Epic 4 increment 3): a valid Cognito identity that's |
| `remove_player_enforced` | group_id, player_id, event | 143 | Same dual-gate as delete_group_enforced, for member removal. |
| `_requires_linked_member` | claims | 151 | Signing up is not the same as being a member. Cognito self-signup is |
| `register_and_join` | event | 174 | Combined 'register a friend' + 'quick-add during match setup' |
| `add_player_enforced` | group_id, event | 258 | Requires SuperAdmin, or already owner/admin of THIS group - reuses |
| `create_group_enforced` | event | 267 | Requires a valid Cognito login (any authenticated account - no |
| `_consume_extra_group_perk` | player_id | 301 | Spend one extra_group token if the player owns one. Mirrors the |
| `visible_players_for_caller` | event | 325 | For populating the Profile tab's player picker: SuperAdmin gets |
| `create_group` | event | 384 | — |
| `list_groups` |  | 408 | — |
| `get_group` | group_id | 423 | — |
| `update_group_defaults` | group_id, event | 444 | Save a group's default tournament creation settings (format, points, |
| `delete_group` | group_id, event | 465 | Deletes only the group record itself. Player records are never |
| `add_player` | group_id, event | 479 | — |
| `remove_player` | group_id, player_id, event | 521 | — |
| `_caller_claims` | event | 543 | Claims API Gateway's Cognito Authorizer attaches to the request. |
| `_is_super_admin` | claims | 551 | — |
| `set_role` | group_id, player_id, event | 556 | Set (or change) a member's role within this group. |
| `set_finance_role` | group_id, player_id, event | 599 | Set a member's per-group FINANCE role (none/view/write/delete) in this |
| `_response` | status_code, body_dict | 639 | — |

#### `matches` — 2180 LOC
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
| `compute_partnerships` | player_id, items | 1155 | For a given player, tally win/loss record with each doubles partner |
| `get_group_member_ids` | group_id | 1197 | The set of player_ids belonging to a group, used to filter WHO shows |
| `compute_attendance` | items, group_id_filter | 1208 | Per-player attendance/consistency: total matches, distinct calendar |
| `compute_hall_of_fame` | items, group_id_filter | 1276 | Highlight stats computed from full chronological match history: |
| `compute_achievements` | player_id, matches, tournaments | 1597 | Milestone/tiered achievement progress for one player: total matches |
| `compute_top_opponents` | player_id, matches, top_n | 1715 | This player's win/loss record against every opponent they've ever |
| `compute_overall_record` | player_id, matches | 1757 | This player's total win/loss record, split by singles and doubles. |
| `compute_head_to_head` | player_id, opponent_id, matches | 1786 | One player's win/loss record specifically as an OPPONENT of another |
| `compute_with_partner` | player_id, partner_id, matches | 1818 | One player's win/loss record when partnered WITH another player on |
| `compute_recent_form` | player_id, matches, limit | 1869 | A player's last N matches, in chronological order (oldest to |
| `compute_diversity` | items, group_id_filter | 1926 | For every player: how concentrated their doubles partnerships are. |
| `compute_progress_history_summary` | scope_label, period_name | 1971 | Reads the permanent, locked-in weekly/monthly/yearly winner history |
| `compute_progress_badges` | items, group_id_filter | 2048 | For each of the last week/month/year: who improved their rating the |
| `compute_partner_distribution` | player_id, items, top_n | 2124 | For the radar/spider chart: one player's doubles partners, sorted by |
| `_response` | status_code, body_dict | 2171 | — |

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

#### `finance` — 888 LOC
_NetWorth - finance Lambda_

**Module constants:** `GROUPS_TABLE`, `DEFAULT_GROUP_NAME`, `VIEW_KEY`, `CONFIRMATION_CODE`, `MONTHS`, `FINANCE_LEVELS`, `ALLOWED_FIELDS`, `NUMERIC_FIELDS`, `REQUIRED_FIELDS`, `AVG_GAMES_PER_SESSION`, `SESSION_RATE`, `ACTIVE_DAYS_THRESHOLD`

| Function | Args | Line | What it does |
|---|---|---|---|
| `_caller_claims` | event | 82 | Claims API Gateway's Cognito Authorizer attaches to the request. |
| `_is_super_admin` | claims | 90 | — |
| `_finance_role` | claims | 104 | — |
| `_finance_level` | claims | 120 | — |
| `_has_finance_access` | claims | 124 | View or better - the gate for reading finance at all. |
| `_default_group_id` |  | 129 | The group_id of the 'Club (default)' group that the pre-migration |
| `_group_for_request` | params, body | 145 | The group_id this finance op targets. Falls back to the default group |
| `_group_finance_level` | claims, group_id | 152 | A caller's finance level (0-3) FOR A SPECIFIC GROUP. |
| `_has_any_group_finance` | claims | 178 | True if the caller has finance access in ANY group (owner/admin, or a |
| `finance_key_for_caller` | event | 193 | Hands the shared view key to any caller with finance access - global |
| `set_finance_access` | event | 206 | SuperAdmin sets a player's finance role directly. |
| `handler` | event, context | 230 | — |
| `_scan_type` | record_type, group_id | 336 | — |
| `_num` | v, default | 347 | — |
| `_clean` | record_type, data | 370 | — |
| `_resolve_name` | pid_cache, player_id | 382 | — |
| `list_records` | record_type, params, group_id | 393 | — |
| `create_records` | record_type, body, group_id | 434 | — |
| `update_record` | record_type, record_id, body, group_id | 456 | — |
| `delete_record_enforced` | record_type, record_id, event | 499 | Triple-gated: SuperAdmin identity + FINANCE_VIEW_KEY + the existing |
| `delete_record` | record_type, record_id, body, group_id | 522 | — |
| `get_settings` |  | 536 | — |
| `put_settings` | body | 545 | — |
| `public_upi` |  | 560 | The pay card is shown to guests (they pay walk-in fees), so the UPI |
| `public_walkins` |  | 568 | — |
| `_settlement_rows` | group_id | 588 | Per (month, year, slot): the exact math from the Calculations sheet. |
| `summary` | group_id | 664 | — |
| `insights` | group_id | 681 | Per-member monthly economics, ghosts, and walk-in conversion. |
| `_response` | status_code, body_dict | 879 | — |

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
### Frontend (`frontend/js/app.js` — 6841 LOC, flat global script, ~271 functions)

_Loaded by `index.html` after an inline `<script>` defines the globals `API_BASE_URL`, `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, `UPI_ID`, `FINANCE_VIEW_KEY` placeholders. Functions live in global scope (not an IIFE); most are wired to `onclick=` in the HTML._


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
- `ownsAnyGroup()` — L157
- `canReviewRequests()` — L160
- `updateReviewTabScope()` — L167
- `formatPlayerLabel(name, nickname)` — L190
- `toggleDisplayMode()` — L195

**Data-load helpers**  (from L226)
- `populateSelect(selectEl, items, valueKey, labelKey, pla)` — L250
- `loadPlayers()` — L269
- `loadGroups()` — L292
- `loadGroupMembers(groupId)` — L328
- `applyGroupDefaultsToForm(prefix, settings)` — L363
- `setIfPresent(suffix, value)` — L365
- `renderAddPlayersChecklist()` — L376
- `removePlayerFromGroup(groupId, playerId)` — L389
- `populateTeamSelects()` — L418
- `refreshTeamSelectOptions()` — L444
- `applyMatchTypeVisibility()` — L467
- `updateMatchGroupCache()` — L481
- `randomizeTeams(showAlertOnFail)` — L500

**Live point-by-point scoring**  (from L510)
- `isGameOver(a, b, target)` — L537
- `updateLiveScoreDisplay()` — L545

**Split-screen live scoring**  (from L591)
- `getTeamDisplayName(selectId)` — L615
- `getSplitTeamNames()` — L621
- `updateSplitScreenScores(a, b, over)` — L636
- `openSplitScreenGeneric(config)` — L645
- `closeSplitScreen()` — L654
- `openSplitScreen()` — L660
- `openTournamentSplitScreen(matchKey, target, nameA, nameB, finishFn)` — L685

**Delete / edit player**  (from L776)
- `prefillEditForm()` — L839

**Voice match entry**  (from L1016)
- `myGroups()` — L1021
- `defaultMatchGroup()` — L1031
- `applyVoiceVisibility()` — L1047
- `nwPhon(s)` — L1054
- `nwLev(a, b)` — L1065
- `nwScorePlayer(token, p)` — L1072
- `nwMatchPlayerToken(tokenRaw)` — L1091
- `nwWordsToNums(t)` — L1106
- `nwParseMatchTranscript(raw)` — L1114
- `nwApplyParsedToForm(p)` — L1146
- `set(id, entry)` — L1150
- `nwVoicePreviewHtml(p)` — L1160
- `nwVoiceMatchInit()` — L1172
- `stopListening()` — L1201

**Team pairing preview**  (from L1264)
- `nwSeeded(p)` — L1290
- `nwShuffle(a)` — L1291
- `nwPairingRefreshList()` — L1293
- `nwPairingUpdateCount()` — L1304
- `nwPairingRender()` — L1309
- `nwPairingInit()` — L1341
- `nameFor(pid)` — L1413

**Unsaved-match safety net**  (from L1472)
- `showMatchOutcome(ok, message)` — L1482
- `savePendingMatch(payload, meta)` — L1501
- `loadPendingMatch()` — L1505
- `clearPendingMatch()` — L1508
- `handleSessionExpired()` — L1515
- `ensureRestoreHost()` — L1538
- `offerPendingMatchRestore()` — L1547

**Game log & CSV export**  (from L1558)
- `loadGameLog()` — L1582
- `gameLogGoto(p)` — L1632
- `renderGameLog()` — L1634
- `matchPermissions(m)` — L1691
- `matchGroupLabel(m)` — L1712
- `requestMatchChange(matchId, type, label, groupId, extra)` — L1717
- `editMatchScore(matchId, currentScoreA, currentScoreB, e)` — L1733
- `deleteMatch(matchId, encLabel, groupId)` — L1768
- `downloadCSV(filename, rows)` — L1799
- `loadRankings()` — L1833
- `fetchRatingHistory(playerId)` — L1881
- `loadVisiblePlayers(opts = {})` — L1892

**Profile card customization**  (from L1901)
- `resolveBannerId(id)` — L2020
- `bgCss(id, url)` — L2094
- `updatePageBackground()` — L2099
- `applyPageBackground(player)` — L2110
- `renderProfileCardBanner(player)` — L2117
- `toggleHeaderMenu()` — L2161
- `openSettingsModal()` — L2175
- `loadFinanceAccessList()` — L2188
- `opt(v, label)` — L2206
- `setGroupFinanceRole(groupId, playerId, role)` — L2221
- `setFinanceRole(playerId, role)` — L2233
- `closeSettingsModal()` — L2244
- `renderSettingsPickers(player)` — L2248
- `swatch(field, id, css, selected)` — L2257
- `submitClaimRequest()` — L2286
- `checkApprovalStatus()` — L2311
- `recomputeNow()` — L2324

**Quests**  (from L2326)
- `loadAppSettings()` — L2338
- `setXpPublic(value)` — L2354
- `setVoiceEnabled(value)` — L2369
- `setInstantCreate(value)` — L2384
- `loadQuests()` — L2397

**Store & events admin**  (from L2430)
- `claimQuest(questId)` — L2431
- `loadQuestsAdmin()` — L2447
- `saveQuest()` — L2468
- `deleteQuest(questId)` — L2489
- `loadStore()` — L2501
- `buyStoreItem(itemId)` — L2536
- `onStoreImagePick(input)` — L2550
- `loadStoreAdmin()` — L2558
- `onStoreTypeChange()` — L2579
- `onStoreEffectChange()` — L2591
- `uploadStoreImage(file)` — L2598
- `saveStoreItem()` — L2615
- `deleteStoreItem(itemId)` — L2661
- `loadEventsAdmin()` — L2672
- `editEvent(e)` — L2694
- `saveEvent()` — L2702
- `deleteEvent(eventId)` — L2725
- `refreshEventBanner()` — L2737
- `loadUnconfirmedUsers()` — L2752

**Image uploads**  (from L2765)
- `deleteUnconfirmedUser(username, email)` — L2779
- `loadClaimRequests()` — L2792
- `decideClaimRequest(requestId, action, requestType)` — L2832
- `escapeHtml(s)` — L2870
- `resizeImage(file, kind)` — L2887

**Profile bundle / cards / charts**  (from L2918)
- `isAnimatedImage(file)` — L2922
- `uploadCardImage(kind, fileInput)` — L2934
- `imageSrc(key)` — L2989
- `loadStoreCatalogOnce()` — L2995
- `renderStoreCosmeticStrip(kind, player)` — L3005
- `renderUploadStrip(kind, player)` — L3029
- `vsPlayerVisual(pid, snapshot)` — L3061
- `vsAvatarHtml(v, isWinner)` — L3077
- `teamBanner(side)` — L3094
- `gameScore(game, side)` — L3105
- `renderVsCard(idsA, idsB, opts = {})` — L3111
- `won(side)` — L3116
- `vsSideIds(side)` — L3141
- `setMyCardField(field, value)` — L3151
- `loadProfileBundle(playerId)` — L3219
- `renderTieredCard(icon, name, unit, tiers, currentValue)` — L3334
- `renderBinaryCard(icon, name, desc, achieved, detail)` — L3362
- `loadProfileRatingChart(playerId)` — L3427
- `loadProfilePartnershipsAndRadar(playerId)` — L3476
- `loadProfileHeadToHead(playerId)` — L3525
- `loadProfileWithPartner(playerId)` — L3549
- `partnerGamesGoto(p)` — L3581
- `renderPartnerGames()` — L3583
- `skeletonHTML(lines = 3)` — L3616
- `showProfileSkeletons()` — L3623
- `renderXpPanel(player)` — L3636
- `xpForLevel(n)` — L3644
- `updateHeaderCoins()` — L3670
- `loadProfile()` — L3682
- `refreshProfile()` — L3707
- `refreshProfileIfShowing(affectedPlayerIds)` — L3724
- `renderPartnerRadar(data, highlightTournament, svgId = 'rada)` — L3745
- `loadHistory()` — L3801
- `renderHistory(data)` — L3819
- `loadBadges()` — L3880
- `renderBadges(data)` — L3898
- `loadDiversity()` — L3931

**UPI payment card**  (from L3941)
- `renderDiversity(data)` — L3949
- `playerLabelById(playerId, fallbackName)` — L3970
- `playerLabelsById(playerIds, fallbackNames)` — L3974
- `loadHallOfFame()` — L3980

**Finance tab (view-key + role gated)**  (from L3999)
- `renderHallOfFame(data)` — L4002
- `loadAttendance()` — L4086
- `renderAttendance(data)` — L4105
- `refreshUpiCard()` — L4124
- `renderUpiCard()` — L4136
- `imageServiceFallback()` — L4158
- `xpVisible()` — L4186
- `applyFinanceRoleVisibility()` — L4192
- `finQS(extra)` — L4213
- `financeBaseUrl()` — L4224
- `finPost(path, method, bodyObj)` — L4228
- `populateFinanceGroups()` — L4249
- `reloadFinanceForGroup()` — L4276
- `tryAutoFinanceUnlock()` — L4281
- `requestFinanceAccess()` — L4304
- `financeUnlock()` — L4321
- `loadFinanceSummary()` — L4359
- `loadFinanceExpenses()` — L4380
- `resetExpenseEdit()` — L4418
- `addFinanceExpense()` — L4425
- `loadFinanceMembers()` — L4444
- `renderBulkRosterList()` — L4529
- `bulkAddFromRoster()` — L4543
- `copyPreviousMonthMembers()` — L4558
- `addFinanceMember()` — L4594
- `loadFinanceWalkins()` — L4615
- `addFinanceWalkin()` — L4642

**Match review & reorder (SuperAdmin)**  (from L4649)
- `loadFinanceInsights()` — L4680
- `renderInsights()` — L4694
- `saveFinanceSettings()` — L4766

**Auth UI (Cognito login/signup/session)**  (from L4780)
- `loadPublicWalkins()` — L4803
- `loadReviewDay()` — L4879
- `reviewOrderChanged()` — L4923
- `renderReviewList()` — L4929
- `applyReviewOrder()` — L4978
- `updateAuthUI()` — L5005
- `openAuthModal()` — L5082
- `closeAuthModal()` — L5083
- `showAuthView(view)` — L5084
- `setAuthSession(session, user, opts = {})` — L5092
- `openCompleteProfileModal()` — L5109
- `showCompleteProfileMode(mode, preselectPlayerId)` — L5124
- `populateClaimPicker(preselectPlayerId)` — L5132
- `submitClaimProfile()` — L5156
- `closeCompleteProfileModal()` — L5196
- `sanitizeNickname(raw)` — L5202
- `editDistance(a, b)` — L5207
- `checkForExistingPlayer(name, typedNickname, statusEl)` — L5229
- `submitCompleteProfile()` — L5284

**Init & session restore**  (from L5346)
- `finishRequestAndSignOut(message)` — L5360
- `doLogin()` — L5366

**Tournaments**  (from L5371)
- `doNewPassword()` — L5419
- `doSignup()` — L5430
- `doConfirmSignup()` — L5447
- `doResendConfirmCode()` — L5478
- `doForgotPassword()` — L5489
- `doConfirmForgotPassword()` — L5504
- `doLogout()` — L5516
- `restoreSession()` — L5560
- `restoreTabFromHash()` — L5594
- `addManualTeamRow()` — L5660
- `collectManualTeams()` — L5696
- `loadTournamentGroupOptions()` — L5709
- `loadTournamentParticipantsChecklist()` — L5718
- `updateParticipantsCount()` — L5748
- `collectTournamentParticipants()` — L5760
- `loadTournamentsList()` — L5764
- `submitTournamentCreation(payload)` — L5771
- `collectAllEntities(t)` — L5930
- `getAllTeamEntities(t)` — L5946
- `renderTeamCompositionBars(t, containerId)` — L5964
- `populateSubstitutionSection(t)` — L5999
- `updateSubOldPlayerOptions()` — L6010
- `formatGames(games)` — L6099
- `applyTournamentViewMode()` — L6106
- `matchTotals(match)` — L6112
- `truncateBracketName(name, maxChars = 22)` — L6120
- `renderBracketView(t)` — L6125
- `renderTournament(t)` — L6241
- `generateTournamentRecap(t)` — L6415

**Live scoring inside tournaments**  (from L6421)
- `downloadTournamentImage()` — L6447
- `loadImg(src)` — L6474
- `sideVisuals(side)` — L6484
- `drawCard(x, y, w, match, isFinal)` — L6491
- `drawAvatars(ctx, x, y, side, isWinner)` — L6537
- `paintTeam(ctx, x, y, w, h, side, fallback)` — L6556
- `roundRect(ctx, x, y, w, h, r)` — L6584
- `copyTournamentRecap()` — L6594
- `item_has_third_place(t)` — L6605
- `submitGroupScore(tournamentId, subgroup, fixtureId)` — L6609
- `submitGroupScoreDirect(tournamentId, subgroup, fixtureId, score)` — L6615
- `submitKnockoutScore(tournamentId, roundIndex, matchIndex)` — L6634
- `submitKnockoutScoreDirect(tournamentId, roundIndex, matchIndex, sc)` — L6640
- `submitThirdPlaceScore(tournamentId)` — L6659
- `submitThirdPlaceScoreDirect(tournamentId, score_a, score_b, override)` — L6665
- `getTournamentLiveLog(matchKey)` — L6688
- `tournamentLivePoint(matchKey, side, target)` — L6693
- `tournamentUndoPoint(matchKey, target)` — L6702
- `updateTournamentLiveDisplay(matchKey, target)` — L6708
- `finishGroupLiveGame(matchKey, tournamentId, subgroup, fixtur)` — L6726
- `finishKnockoutLiveGame(matchKey, tournamentId, roundIndex, matc)` — L6735
- `finishThirdPlaceLiveGame(matchKey, tournamentId)` — L6744
- `renderLiveScoreControls(matchKey, target, finishCallExpr, nameA,)` — L6753
- `applyTheme(theme)` — L6822
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
