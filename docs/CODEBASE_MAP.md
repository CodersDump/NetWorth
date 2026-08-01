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

#### `players` — 1709 LOC
_NetWorth - players Lambda (list all, update one, delete one)_

**Module constants:** `CLAIM_REQUESTS_TABLE`, `USER_POOL_ID`, `UPLOADS_BUCKET`, `GROUPS_TABLE`, `CONFIRMATION_CODE`, `ALLOWED_AVATARS`, `ALLOWED_BANNERS`, `ALLOWED_BACKGROUNDS`, `OWNER_DECIDABLE_TYPES`, `_APP_SETTINGS_ID`, `_STORE_CATALOG_ID`, `_STORE_ITEM_TYPES`, `FINANCE_LEVELS`, `ALLOWED_UPLOAD_TYPES`, `UPLOAD_KINDS`, `MAX_UPLOADS_PER_KIND`, `FREE_RENAMES`

| Function | Args | Line | What it does |
|---|---|---|---|
| `sanitize_nickname` | raw | 40 | Same rule as register_player's version (duplicated on purpose - |
| `handler` | event, context | 49 | — |
| `lookup_email_for_login` | identifier | 104 | Resolves a player_id, exact name, or exact nickname to the email |
| `list_players` |  | 139 | — |
| `_caller_claims` | event | 176 | — |
| `_can_self_rename` | claims | 180 | Placeholder gate - the achievement/level system this is meant to |
| `claim_player` | event | 205 | Self-service: link my Cognito account to an EXISTING, UNCLAIMED |
| `_is_super_admin` | claims | 273 | — |
| `_linked_player_is_live` | claims | 280 | True only if the caller's custom:player_id resolves to a player that |
| `create_claim_request` | event | 290 | Anyone logged in but not yet linked can ASK to be linked to an |
| `_caller_owned_group_ids` | claims | 346 | The set of group_ids where the caller's linked player is owner or admin. |
| `_player_group_ids` | player_id | 362 | Every group_id whose roles map contains this player. |
| `_owner_may_decide` | req, owned_group_ids | 370 | True if a group owner/admin (owning owned_group_ids) may act on req: |
| `_audit_attr` | user, name | 387 | — |
| `_cognito_users_all` | cognito | 391 | — |
| `audit_claims` | event | 401 | SuperAdmin: audit Cognito account <-> player linkage (the claim_audit.py |
| `claim_audit_action` | event | 473 | SuperAdmin link/unlink. link: point an account at a player AND stamp the |
| `list_unconfirmed_users` | event | 526 | SuperAdmin-only: Cognito accounts stuck in UNCONFIRMED (signed up but |
| `delete_unconfirmed_user` | event | 558 | SuperAdmin-only: delete a single UNCONFIRMED Cognito account by username. |
| `list_claim_requests` | event | 584 | — |
| `create_action_request` | event | 603 | A non-SuperAdmin asking for a destructive action instead of doing |
| `_create_new_profile_request` | claims, body | 670 | Creating a brand-new profile. By default this is a REQUEST an admin |
| `_get_app_setting` | key, default | 753 | App-wide flags live in one reserved row of the players table, keyed |
| `get_app_settings` | event | 762 | — |
| `set_app_setting` | event | 771 | — |
| `_load_catalog` |  | 796 | — |
| `list_store` | event | 801 | Public read - anyone can browse the store. Returns the catalog. |
| `save_store_item` | event | 807 | — |
| `delete_store_item` | event | 842 | — |
| `purchase_store_item` | event | 855 | A player spends coins on an item. Coins are deducted by bumping |
| `_create_edit_name_request` | claims, body | 900 | Renaming is now self-service-only: the target is always the |
| `_approve_edit_name` | req, claims | 946 | — |
| `_approve_new_profile` | req, claims | 962 | Creates the player only at approval time, and links it to the |
| `_create_match_request` | claims, body, action_type | 998 | A match edit or delete, filed as a request rather than executed. The |
| `_create_finance_access_request` | claims, body | 1046 | A member asking for a finance role (view / write / delete) IN A GROUP. |
| `_approve_finance_access` | req | 1099 | — |
| `decide_claim_request` | event | 1125 | Approve or reject. On approval this writes the link on BOTH sides: |
| `create_upload_url` | event | 1275 | Hands back a short-lived presigned PUT. The browser uploads straight |
| `_valid_upload_key` | value, player_id, kind | 1339 | An uploaded image is referenced by key, and the key is checked |
| `_owns_store_cosmetic` | player, key, kind | 1349 | True if `key` is the image of a store cosmetic the player OWNS whose |
| `_rotate_uploads` | player_id, kind, new_key | 1374 | Maintains the player's short list of custom images, newest first, |
| `update_my_card` | event | 1406 | Self-service avatar/banner customization for the CALLER'S OWN |
| `_consume_perk` | player, player_id, effect_kind | 1502 | Spends one token of a perk the player owns (by store item effect |
| `rename_self` | event | 1531 | Self-service nickname change for the CALLER'S OWN linked player. |
| `update_player` | player_id, event | 1571 | — |
| `delete_player` | player_id, event | 1643 | — |
| `_cognito_username_for_email` | cognito, email | 1692 | The username is not always the email, so it has to be looked up. |
| `_response` | status_code, body_dict | 1700 | — |

#### `groups` — 762 LOC
_NetWorth - groups Lambda_

**Module constants:** `CONFIRMATION_CODE`, `VALID_ROLES`, `FINANCE_ROLE_LEVELS`

| Function | Args | Line | What it does |
|---|---|---|---|
| `sanitize_nickname` | raw | 42 | Same rule as register_player's version (duplicated - separate |
| `handler` | event, context | 53 | — |
| `_authorize_group_action` | group_id, claims | 119 | Shared check for Epic 4's group-scoped write actions: SuperAdmin, or |
| `delete_group_enforced` | group_id, event | 135 | Dual-gated (Epic 4 increment 3): a valid Cognito identity that's |
| `remove_player_enforced` | group_id, player_id, event | 147 | Same dual-gate as delete_group_enforced, for member removal. |
| `_requires_linked_member` | claims | 155 | Signing up is not the same as being a member. Cognito self-signup is |
| `register_and_join` | event | 178 | Combined 'register a friend' + 'quick-add during match setup' |
| `add_player_enforced` | group_id, event | 262 | Requires SuperAdmin, or already owner/admin of THIS group - reuses |
| `create_group_enforced` | event | 271 | Requires a valid Cognito login (any authenticated account - no |
| `_consume_extra_group_perk` | player_id | 305 | Spend one extra_group token if the player owns one. Mirrors the |
| `visible_players_for_caller` | event | 329 | For populating the Profile tab's player picker: SuperAdmin gets |
| `create_group` | event | 388 | — |
| `list_groups` |  | 412 | — |
| `get_group` | group_id | 431 | — |
| `update_group_defaults` | group_id, event | 456 | Save a group's default tournament creation settings (format, points, |
| `set_group_slots` | group_id, event | 477 | Owner/admin group settings via the Cognito-authorized PUT |
| `delete_group` | group_id, event | 579 | Deletes only the group record itself. Player records are never |
| `add_player` | group_id, event | 593 | — |
| `remove_player` | group_id, player_id, event | 635 | — |
| `_caller_claims` | event | 657 | Claims API Gateway's Cognito Authorizer attaches to the request. |
| `_is_super_admin` | claims | 665 | — |
| `set_role` | group_id, player_id, event | 670 | Set (or change) a member's role within this group. |
| `set_finance_role` | group_id, player_id, event | 713 | Set a member's per-group FINANCE role (none/view/write/delete) in this |
| `_response` | status_code, body_dict | 753 | — |

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

#### `finance` — 957 LOC
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
| `_scan_type` | record_type, group_id | 343 | — |
| `_num` | v, default | 354 | — |
| `_clean` | record_type, data | 377 | — |
| `_resolve_name` | pid_cache, player_id | 389 | — |
| `list_records` | record_type, params, group_id | 400 | — |
| `create_records` | record_type, body, group_id | 441 | — |
| `update_record` | record_type, record_id, body, group_id | 463 | — |
| `delete_record_enforced` | record_type, record_id, event | 506 | Triple-gated: SuperAdmin identity + FINANCE_VIEW_KEY + the existing |
| `delete_record` | record_type, record_id, body, group_id | 529 | — |
| `get_settings` |  | 543 | — |
| `put_settings` | body | 552 | — |
| `public_upi` |  | 567 | The pay card is shown to guests (they pay walk-in fees), so the UPI |
| `my_settlement` | claims, group_id | 575 | A single member's own dues in a group: for every (month, slot) where |
| `public_walkins` |  | 637 | — |
| `_settlement_rows` | group_id | 657 | Per (month, year, slot): the exact math from the Calculations sheet. |
| `summary` | group_id | 733 | — |
| `insights` | group_id | 750 | Per-member monthly economics, ghosts, and walk-in conversion. |
| `_response` | status_code, body_dict | 948 | — |

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
### Frontend (`frontend/js/app.js` — 7238 LOC, flat global script, ~289 functions)

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
- `opt(v, label)` — L370
- `nameOf(pid)` — L383
- `applyGroupDefaultsToForm(prefix, settings)` — L418
- `setIfPresent(suffix, value)` — L420
- `renderAddPlayersChecklist()` — L431
- `removePlayerFromGroup(groupId, playerId)` — L444
- `populateTeamSelects()` — L473
- `refreshTeamSelectOptions()` — L499

**Live point-by-point scoring**  (from L510)
- `applyMatchTypeVisibility()` — L522
- `updateMatchGroupCache()` — L536
- `randomizeTeams(showAlertOnFail)` — L555

**Split-screen live scoring**  (from L591)
- `isGameOver(a, b, target)` — L592
- `updateLiveScoreDisplay()` — L600
- `getTeamDisplayName(selectId)` — L670
- `getSplitTeamNames()` — L676
- `updateSplitScreenScores(a, b, over)` — L691
- `openSplitScreenGeneric(config)` — L700
- `closeSplitScreen()` — L709

**Player registration**  (from L711)
- `openSplitScreen()` — L715
- `openTournamentSplitScreen(matchKey, target, nameA, nameB, finishFn)` — L740

**Groups**  (from L850)
- `prefillEditForm()` — L894

**Voice match entry**  (from L1016)
- `myGroups()` — L1076
- `defaultMatchGroup()` — L1086
- `applyVoiceVisibility()` — L1102
- `nwPhon(s)` — L1109
- `nwLev(a, b)` — L1120
- `nwScorePlayer(token, p)` — L1127
- `nwMatchPlayerToken(tokenRaw)` — L1146
- `nwWordsToNums(t)` — L1161
- `nwParseMatchTranscript(raw)` — L1169
- `nwApplyParsedToForm(p)` — L1201
- `set(id, entry)` — L1205
- `nwVoicePreviewHtml(p)` — L1215
- `nwVoiceMatchInit()` — L1227
- `stopListening()` — L1256

**Team pairing preview**  (from L1264)
- `nwSeeded(p)` — L1345
- `nwShuffle(a)` — L1346
- `nwPairingRefreshList()` — L1348
- `nwPairingUpdateCount()` — L1359
- `nwPairingRender()` — L1364
- `nwPairingInit()` — L1396
- `nameFor(pid)` — L1468

**Unsaved-match safety net**  (from L1472)
- `showMatchOutcome(ok, message)` — L1537
- `savePendingMatch(payload, meta)` — L1556

**Game log & CSV export**  (from L1558)
- `loadPendingMatch()` — L1560
- `clearPendingMatch()` — L1563
- `handleSessionExpired()` — L1570
- `ensureRestoreHost()` — L1593
- `offerPendingMatchRestore()` — L1602
- `loadGameLog()` — L1637
- `gameLogGoto(p)` — L1687
- `renderGameLog()` — L1689
- `matchPermissions(m)` — L1746
- `matchGroupLabel(m)` — L1767
- `requestMatchChange(matchId, type, label, groupId, extra)` — L1772
- `editMatchScore(matchId, currentScoreA, currentScoreB, e)` — L1788
- `deleteMatch(matchId, encLabel, groupId)` — L1823
- `downloadCSV(filename, rows)` — L1854
- `loadRankings()` — L1888

**Profile card customization**  (from L1901)
- `fetchRatingHistory(playerId)` — L1936
- `loadVisiblePlayers(opts = {})` — L1947
- `resolveBannerId(id)` — L2075
- `bgCss(id, url)` — L2149
- `updatePageBackground()` — L2154
- `applyPageBackground(player)` — L2165
- `renderProfileCardBanner(player)` — L2172
- `toggleHeaderMenu()` — L2216
- `openSettingsModal()` — L2230
- `loadFinanceAccessList()` — L2243
- `opt(v, label)` — L2261
- `setGroupFinanceRole(groupId, playerId, role)` — L2276
- `setFinanceRole(playerId, role)` — L2288
- `closeSettingsModal()` — L2299
- `renderSettingsPickers(player)` — L2303
- `swatch(field, id, css, selected)` — L2312

**Quests**  (from L2326)
- `submitClaimRequest()` — L2341
- `checkApprovalStatus()` — L2366
- `recomputeNow()` — L2379
- `loadAppSettings()` — L2393
- `setXpPublic(value)` — L2409
- `setVoiceEnabled(value)` — L2424

**Store & events admin**  (from L2430)
- `setInstantCreate(value)` — L2439
- `loadQuests()` — L2452
- `claimQuest(questId)` — L2486
- `loadQuestsAdmin()` — L2502
- `saveQuest()` — L2523
- `deleteQuest(questId)` — L2544
- `loadStore()` — L2556
- `buyStoreItem(itemId)` — L2591
- `onStoreImagePick(input)` — L2605
- `loadStoreAdmin()` — L2613
- `onStoreTypeChange()` — L2634
- `onStoreEffectChange()` — L2646
- `uploadStoreImage(file)` — L2653
- `saveStoreItem()` — L2670
- `deleteStoreItem(itemId)` — L2716
- `loadEventsAdmin()` — L2727
- `editEvent(e)` — L2749
- `saveEvent()` — L2757

**Image uploads**  (from L2765)
- `deleteEvent(eventId)` — L2780
- `refreshEventBanner()` — L2792
- `loadClaimAudit()` — L2809
- `relinkAccount(usernameEnc, presetPlayerId)` — L2876
- `unlinkAccount(usernameEnc)` — L2884
- `unlinkAndStrip(usernameEnc, playerId)` — L2889
- `_claimAuditAction(bodyObj)` — L2894
- `loadUnconfirmedUsers()` — L2905

**Profile bundle / cards / charts**  (from L2918)
- `deleteUnconfirmedUser(username, email)` — L2932
- `loadClaimRequests()` — L2945
- `decideClaimRequest(requestId, action, requestType)` — L2985
- `escapeHtml(s)` — L3023
- `resizeImage(file, kind)` — L3040
- `isAnimatedImage(file)` — L3075
- `uploadCardImage(kind, fileInput)` — L3087
- `imageSrc(key)` — L3142
- `loadStoreCatalogOnce()` — L3148
- `renderStoreCosmeticStrip(kind, player)` — L3158
- `renderUploadStrip(kind, player)` — L3182
- `vsPlayerVisual(pid, snapshot)` — L3214
- `vsAvatarHtml(v, isWinner)` — L3230
- `teamBanner(side)` — L3247
- `gameScore(game, side)` — L3258
- `renderVsCard(idsA, idsB, opts = {})` — L3264
- `won(side)` — L3269
- `vsSideIds(side)` — L3294
- `setMyCardField(field, value)` — L3304
- `loadProfileBundle(playerId)` — L3372
- `renderTieredCard(icon, name, unit, tiers, currentValue)` — L3487
- `renderBinaryCard(icon, name, desc, achieved, detail)` — L3515
- `resetRatingZoom()` — L3580
- `loadProfileRatingChart(playerId)` — L3586
- `loadProfilePartnershipsAndRadar(playerId)` — L3672
- `loadProfileHeadToHead(playerId)` — L3721
- `loadProfileWithPartner(playerId)` — L3745
- `partnerGamesGoto(p)` — L3777
- `renderPartnerGames()` — L3779
- `skeletonHTML(lines = 3)` — L3812
- `showProfileSkeletons()` — L3819
- `renderXpPanel(player)` — L3832
- `xpForLevel(n)` — L3840
- `updateHeaderCoins()` — L3866
- `loadProfile()` — L3878
- `refreshProfile()` — L3903
- `refreshProfileIfShowing(affectedPlayerIds)` — L3920

**UPI payment card**  (from L3941)
- `renderPartnerRadar(data, highlightTournament, svgId = 'rada)` — L3941
- `loadHistory()` — L3997

**Finance tab (view-key + role gated)**  (from L3999)
- `renderHistory(data)` — L4015
- `loadBadges()` — L4076
- `renderBadges(data)` — L4094
- `loadDiversity()` — L4127
- `renderDiversity(data)` — L4145
- `playerLabelById(playerId, fallbackName)` — L4166
- `playerLabelsById(playerIds, fallbackNames)` — L4170
- `loadHallOfFame()` — L4176
- `renderHallOfFame(data)` — L4198
- `loadAttendance()` — L4282
- `renderAttendance(data)` — L4301
- `refreshUpiCard()` — L4320
- `renderUpiCard()` — L4332
- `imageServiceFallback()` — L4354
- `xpVisible()` — L4382
- `applyFinanceRoleVisibility()` — L4388
- `finQS(extra)` — L4409
- `financeBaseUrl()` — L4420
- `finPost(path, method, bodyObj)` — L4424
- `populateFinanceSlots(group)` — L4448
- `populateFinanceGroups()` — L4459
- `reloadFinanceForGroup()` — L4487
- `tryAutoFinanceUnlock()` — L4492
- `myFinanceGroups()` — L4517
- `populateMyDuesGroups()` — L4522
- `loadMyDues(groupId)` — L4540
- `manageGroupSlots(groupId)` — L4585
- `assignSlotMembers(groupId, slotEnc)` — L4603
- `transferGroupOwnership(groupId)` — L4631

**Match review & reorder (SuperAdmin)**  (from L4649)
- `setGroupPayee(groupId)` — L4650
- `requestFinanceAccess()` — L4673
- `financeUnlock()` — L4690
- `loadFinanceSummary()` — L4728
- `loadFinanceExpenses()` — L4749

**Auth UI (Cognito login/signup/session)**  (from L4780)
- `resetExpenseEdit()` — L4787
- `addFinanceExpense()` — L4794
- `loadFinanceMembers()` — L4813
- `scheduleMemberReload()` — L4902
- `renderBulkRosterList()` — L4910
- `bulkAddFromRoster()` — L4924
- `copyPreviousMonthMembers()` — L4939
- `addFinanceMember()` — L4975
- `loadFinanceWalkins()` — L4996
- `addFinanceWalkin()` — L5023
- `loadFinanceInsights()` — L5061
- `renderInsights()` — L5075
- `saveFinanceSettings()` — L5147
- `loadPublicWalkins()` — L5184
- `loadReviewDay()` — L5262
- `reviewOrderChanged()` — L5306
- `renderReviewList()` — L5312

**Init & session restore**  (from L5346)
- `applyReviewOrder()` — L5361

**Tournaments**  (from L5371)
- `updateAuthUI()` — L5388
- `hiddenNow(id, btn)` — L5405
- `openAuthModal()` — L5475
- `closeAuthModal()` — L5476
- `showAuthView(view)` — L5477
- `setAuthSession(session, user, opts = {})` — L5485
- `openCompleteProfileModal()` — L5502
- `showCompleteProfileMode(mode, preselectPlayerId)` — L5517
- `populateClaimPicker(preselectPlayerId)` — L5525
- `submitClaimProfile()` — L5549
- `closeCompleteProfileModal()` — L5589
- `sanitizeNickname(raw)` — L5595
- `editDistance(a, b)` — L5600
- `checkForExistingPlayer(name, typedNickname, statusEl)` — L5622
- `submitCompleteProfile()` — L5677
- `finishRequestAndSignOut(message)` — L5753
- `doLogin()` — L5759
- `doNewPassword()` — L5812
- `doSignup()` — L5823
- `doConfirmSignup()` — L5840
- `doResendConfirmCode()` — L5871
- `doForgotPassword()` — L5882
- `doConfirmForgotPassword()` — L5897
- `doLogout()` — L5909
- `restoreSession()` — L5953
- `restoreTabFromHash()` — L5987
- `addManualTeamRow()` — L6053
- `collectManualTeams()` — L6089
- `loadTournamentGroupOptions()` — L6102
- `loadTournamentParticipantsChecklist()` — L6111
- `updateParticipantsCount()` — L6141
- `collectTournamentParticipants()` — L6153
- `loadTournamentsList()` — L6157
- `submitTournamentCreation(payload)` — L6164
- `collectAllEntities(t)` — L6323
- `getAllTeamEntities(t)` — L6339
- `renderTeamCompositionBars(t, containerId)` — L6357
- `populateSubstitutionSection(t)` — L6392
- `updateSubOldPlayerOptions()` — L6403

**Live scoring inside tournaments**  (from L6421)
- `formatGames(games)` — L6492
- `applyTournamentViewMode()` — L6499
- `matchTotals(match)` — L6505
- `truncateBracketName(name, maxChars = 22)` — L6513
- `renderBracketView(t)` — L6518
- `renderTournament(t)` — L6634
- `generateTournamentRecap(t)` — L6808
- `downloadTournamentImage()` — L6840
- `loadImg(src)` — L6867
- `sideVisuals(side)` — L6877
- `drawCard(x, y, w, match, isFinal)` — L6884
- `drawAvatars(ctx, x, y, side, isWinner)` — L6930
- `paintTeam(ctx, x, y, w, h, side, fallback)` — L6949
- `roundRect(ctx, x, y, w, h, r)` — L6977
- `copyTournamentRecap()` — L6987
- `item_has_third_place(t)` — L6998
- `submitGroupScore(tournamentId, subgroup, fixtureId)` — L7002
- `submitGroupScoreDirect(tournamentId, subgroup, fixtureId, score)` — L7008
- `submitKnockoutScore(tournamentId, roundIndex, matchIndex)` — L7027
- `submitKnockoutScoreDirect(tournamentId, roundIndex, matchIndex, sc)` — L7033
- `submitThirdPlaceScore(tournamentId)` — L7052
- `submitThirdPlaceScoreDirect(tournamentId, score_a, score_b, override)` — L7058
- `getTournamentLiveLog(matchKey)` — L7081
- `tournamentLivePoint(matchKey, side, target)` — L7086
- `tournamentUndoPoint(matchKey, target)` — L7095
- `updateTournamentLiveDisplay(matchKey, target)` — L7101
- `finishGroupLiveGame(matchKey, tournamentId, subgroup, fixtur)` — L7119
- `finishKnockoutLiveGame(matchKey, tournamentId, roundIndex, matc)` — L7128
- `finishThirdPlaceLiveGame(matchKey, tournamentId)` — L7137
- `renderLiveScoreControls(matchKey, target, finishCallExpr, nameA,)` — L7146
- `applyTheme(theme)` — L7219
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
