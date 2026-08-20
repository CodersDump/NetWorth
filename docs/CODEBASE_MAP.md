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

#### `players` — 1901 LOC
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
| `create_upload_url` | event | 1343 | Hands back a short-lived presigned PUT. The browser uploads straight |
| `_valid_upload_key` | value, player_id, kind | 1407 | An uploaded image is referenced by key, and the key is checked |
| `_owns_store_cosmetic` | player, key, kind | 1417 | True if `key` is the image of a store cosmetic the player OWNS whose |
| `_owns_card_layout` | player, layout | 1443 | True if the player may use this stats layout - free ones always, |
| `_owns_value_cosmetic` | player, kind, value | 1451 | True if the player owns a store item of this cosmetic `kind` whose |
| `_rotate_uploads` | player_id, kind, new_key | 1472 | Maintains the player's short list of custom images, newest first, |
| `update_my_card` | event | 1504 | Self-service avatar/banner customization for the CALLER'S OWN |
| `_consume_perk` | player, player_id, effect_kind | 1679 | Spends one token of a perk the player owns (by store item effect |
| `rename_self` | event | 1708 | Self-service nickname change for the CALLER'S OWN linked player. |
| `update_player` | player_id, event | 1748 | — |
| `delete_player` | player_id, event | 1835 | — |
| `_cognito_username_for_email` | cognito, email | 1884 | The username is not always the email, so it has to be looked up. |
| `_response` | status_code, body_dict | 1892 | — |

#### `groups` — 763 LOC
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
| `update_group_defaults` | group_id, event | 457 | Save a group's default tournament creation settings (format, points, |
| `set_group_slots` | group_id, event | 478 | Owner/admin group settings via the Cognito-authorized PUT |
| `delete_group` | group_id, event | 580 | Deletes only the group record itself. Player records are never |
| `add_player` | group_id, event | 594 | — |
| `remove_player` | group_id, player_id, event | 636 | — |
| `_caller_claims` | event | 658 | Claims API Gateway's Cognito Authorizer attaches to the request. |
| `_is_super_admin` | claims | 666 | — |
| `set_role` | group_id, player_id, event | 671 | Set (or change) a member's role within this group. |
| `set_finance_role` | group_id, player_id, event | 714 | Set a member's per-group FINANCE role (none/view/write/delete) in this |
| `_response` | status_code, body_dict | 754 | — |

#### `matches` — 2545 LOC
_NetWorth - matches Lambda (singles + doubles)_

**Module constants:** `K_FACTOR`, `XP_PLAYED`, `XP_WIN_BONUS`, `XP_TOURNAMENT_WIN`, `XP_MARGIN_PER_POINTS`, `XP_MARGIN_CAP`, `XP_LEVEL_COEFF`, `COINS_PER_LEVEL`, `_EVENTS_ROW_ID`, `_QUESTS_ROW_ID`, `_APP_SETTINGS_ID`, `_PRIVATE_ID_KEYS`, `QUEST_TYPES`, `_SEASON_ROW_PREFIX`, `COMEBACK_BONUS_THRESHOLD`, `COMEBACK_BONUS_PER_POINT`, `COMEBACK_BONUS_CAP`, `CONFIRMATION_CODE`

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
| `_is_valid_completed_game` | score_a, score_b, target | 628 | BWF-style badminton scoring: first to `target` points wins, but must lead |
| `_caller_claims` | event | 645 | Claims API Gateway's Cognito Authorizer attaches to the request. |
| `_is_super_admin` | claims | 651 | — |
| `_can_view_profile` | claims, target_player_id | 656 | SuperAdmin sees everyone. Anyone can view their own profile. A |
| `_requires_linked_member` | claims | 676 | Signing up is not the same as being a member. Cognito self-signup is |
| `record_match_enforced` | event | 699 | — |
| `profile_view_enforced` | event | 709 | Entry point for the isolated /profile-secure/{proxy+} catch-all. |
| `handler` | event, context | 736 | — |
| `list_events` | event | 799 | Public read - the frontend shows an active-event banner to everyone. |
| `save_event` | event | 807 | SuperAdmin creates or updates an event (upsert by event_id). |
| `delete_event` | event | 837 | — |
| `recompute_now` | event | 850 | SuperAdmin-only: replay every match to rebuild ratings, XP, levels |
| `reorder_matches` | event | 860 | Reorders a set of matches by reassigning their timestamps. |
| `record_match` | event | 928 | — |
| `update_match` | match_id, event | 969 | Fix a mis-entered score on an already-recorded standalone match. |
| `delete_match` | match_id, event | 1028 | Permanently delete a mis-recorded match - e.g. the wrong player was |
| `recompute_all_ratings` |  | 1049 | Elo is path-dependent - each match's rating change depends on the |
| `compute_momentum_stats` | point_log, winner | 1173 | Longest scoring streak per team, and how big a deficit the winner overcame. |
| `compute_adaptive_k` | pairing_count | 1220 | Higher K for a fresh/novel doubles pairing (each match together is |
| `get_pairing_count` | team_ids, exclude_match_id | 1236 | How many prior doubles matches has this exact 2-player team played |
| `_play_and_log` | match_type, team_a_ids, team_b_ids, score_a,  | 1256 | — |
| `list_matches` | event | 1353 | — |
| `compute_partnerships` | player_id, items | 1497 | For a given player, tally win/loss record with each doubles partner |
| `get_group_member_ids` | group_id | 1539 | The set of player_ids belonging to a group, used to filter WHO shows |
| `compute_attendance` | items, group_id_filter | 1550 | Per-player attendance/consistency: total matches, distinct calendar |
| `compute_hall_of_fame` | items, group_id_filter | 1618 | Highlight stats computed from full chronological match history: |
| `compute_achievements` | player_id, matches, tournaments | 1939 | Milestone/tiered achievement progress for one player: total matches |
| `compute_top_opponents` | player_id, matches, top_n | 2080 | This player's win/loss record against every opponent they've ever |
| `compute_overall_record` | player_id, matches | 2122 | This player's total win/loss record, split by singles and doubles. |
| `compute_head_to_head` | player_id, opponent_id, matches | 2151 | One player's win/loss record specifically as an OPPONENT of another |
| `compute_with_partner` | player_id, partner_id, matches | 2183 | One player's win/loss record when partnered WITH another player on |
| `compute_recent_form` | player_id, matches, limit | 2234 | A player's last N matches, in chronological order (oldest to |
| `compute_diversity` | items, group_id_filter | 2291 | For every player: how concentrated their doubles partnerships are. |
| `compute_progress_history_summary` | scope_label, period_name | 2336 | Reads the permanent, locked-in weekly/monthly/yearly winner history |
| `compute_progress_badges` | items, group_id_filter | 2413 | For each of the last week/month/year: who improved their rating the |
| `compute_partner_distribution` | player_id, items, top_n | 2489 | For the radar/spider chart: one player's doubles partners, sorted by |
| `_response` | status_code, body_dict | 2536 | — |

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

#### `finance` — 1146 LOC
_NetWorth - finance Lambda_

**Module constants:** `GROUPS_TABLE`, `DEFAULT_GROUP_NAME`, `GROUP_SLOT`, `VIEW_KEY`, `CONFIRMATION_CODE`, `MONTHS`, `FINANCE_LEVELS`, `ALLOWED_FIELDS`, `NUMERIC_FIELDS`, `REQUIRED_FIELDS`, `AVG_GAMES_PER_SESSION`, `SESSION_RATE`, `ACTIVE_DAYS_THRESHOLD`

| Function | Args | Line | What it does |
|---|---|---|---|
| `_caller_claims` | event | 83 | Claims API Gateway's Cognito Authorizer attaches to the request. |
| `_is_super_admin` | claims | 91 | — |
| `_finance_role` | claims | 105 | — |
| `_finance_level` | claims | 121 | — |
| `_has_finance_access` | claims | 125 | View or better - the gate for reading finance at all. |
| `_default_group_id` |  | 130 | The group_id of the 'Club (default)' group that the pre-migration |
| `_group_for_request` | params, body | 146 | The group_id this finance op targets. Falls back to the default group |
| `_group_finance_level` | claims, group_id | 153 | A caller's finance level (0-3) FOR A SPECIFIC GROUP. |
| `_slot_key` | slot | 179 | Normalize a record's slot for bucketing/comparison: a missing/blank |
| `_member_assigned_slots` | pid, group | 186 | The set of slots (raw, already-normalized strings) a player is |
| `_view_scope_slots` | claims, group_id, level | 195 | Stage 4c: a plain 'view'-level grant only sees their own assigned |
| `_has_any_group_finance` | claims | 221 | True if the caller has finance access in ANY group (owner/admin, or a |
| `finance_key_for_caller` | event | 236 | Hands the shared view key to any caller with finance access - global |
| `set_finance_access` | event | 249 | SuperAdmin sets a player's finance role directly. |
| `handler` | event, context | 273 | — |
| `_scan_type` | record_type, group_id | 391 | — |
| `_num` | v, default | 402 | — |
| `_clean` | record_type, data | 425 | — |
| `_resolve_name` | pid_cache, player_id | 437 | — |
| `_prev_period` | month, year | 448 | — |
| `_member_relief` | settlement, memberships, ident, month, year,  | 453 | Relief a member gets in (month, year): the previous month's residual. |
| `list_records` | record_type, params, group_id, scope_slots | 476 | — |
| `create_records` | record_type, body, group_id | 538 | — |
| `update_record` | record_type, record_id, body, group_id | 560 | — |
| `delete_record_enforced` | record_type, record_id, event | 623 | Triple-gated: SuperAdmin identity + FINANCE_VIEW_KEY + the existing |
| `delete_record` | record_type, record_id, body, group_id | 646 | — |
| `get_settings` |  | 660 | — |
| `put_settings` | body | 669 | — |
| `public_upi` |  | 684 | The pay card is shown to guests (they pay walk-in fees), so the UPI |
| `my_settlement` | claims, group_id | 692 | A single member's own dues in a group: for every (month, slot) where |
| `public_walkins` |  | 777 | — |
| `_settlement_rows` | group_id | 797 | Per (month, year, slot): the exact math from the Calculations sheet. |
| `summary` | group_id, scope_slots | 911 | — |
| `insights` | group_id | 933 | Per-member monthly economics, ghosts, and walk-in conversion. |
| `_response` | status_code, body_dict | 1137 | — |

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
### Frontend (`frontend/js/app.js` — 8181 LOC, flat global script, ~354 functions)

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
- `_nwModal({ message, input, defaultValue, okText, )` — L247
- `cleanup(val)` — L266
- `onKey(e)` — L288
- `nwConfirm(message, opts = {})` — L298
- `nwAlert(message, opts = {})` — L301
- `nwPrompt(message, defaultValue = '', opts = {})` — L304
- `populateSelect(selectEl, items, valueKey, labelKey, pla)` — L315
- `bumpMatchesRev()` — L343
- `isTabActive(tab)` — L344
- `ensureFresh(key, loader)` — L349
- `ensureOnce(key, loader)` — L355
- `loadStatsBundle()` — L361
- `makeStatsCollapsible()` — L388
- `setOpen(open)` — L403
- `ensureProfileFresh()` — L412
- `loadActiveTabData()` — L421
- `myPlayerRecord()` — L431
- `iAmPrivate()` — L432
- `privateHiddenIds()` — L435
- `renderPrivacyControl()` — L439
- `toggleMyPrivacy()` — L459
- `setPrivacyMode(value)` — L482
- `setPrivacyCooldown(value)` — L496

**Live point-by-point scoring**  (from L510)
- `statsFetch(query)` — L510
- `populateAdminPrivacySelect()` — L516
- `adminSetPrivacy(makePrivate)` — L526
- `seasonMedallion(rank, size)` — L547
- `seasonBadgeSvg(kind, rank, size)` — L561
- `loadPlayerSeasons(playerId)` — L577

**Split-screen live scoring**  (from L591)
- `loadSeasonsMeta()` — L604
- `loadSeasonBoard(seasonId)` — L626
- `renderSeasonAdmin()` — L657
- `saveSeasons(list, statusElId, okMsg)` — L666
- `addSeason()` — L677
- `deleteSeason(id)` — L691
- `setSeasonsEnabled(value)` — L695
- `setSeasonK(value)` — L706

**Player registration**  (from L711)
- `loadPlayers()` — L714
- `loadGroups()` — L737

**Delete / edit player**  (from L776)
- `loadGroupMembers(groupId)` — L785
- `opt(v, label)` — L822
- `opt(v, label)` — L846

**Groups**  (from L850)
- `nameOf(pid)` — L859
- `applyGroupDefaultsToForm(prefix, settings)` — L894
- `setIfPresent(suffix, value)` — L896
- `renderAddPlayersChecklist()` — L907
- `removePlayerFromGroup(groupId, playerId)` — L920
- `populateTeamSelects()` — L955

**Matches (record/list/game-log)**  (from L979)
- `refreshTeamSelectOptions()` — L989
- `syncTeamSelectValues()` — L1003
- `handleTeamSelectChange(changedId)` — L1012

**Voice match entry**  (from L1016)
- `applyMatchTypeVisibility()` — L1026
- `updateMatchGroupCache()` — L1039
- `randomizeTeams(showAlertOnFail)` — L1058
- `isGameOver(a, b, target)` — L1096
- `updateLiveScoreDisplay()` — L1104
- `getTeamDisplayName(selectId)` — L1174
- `getSplitTeamNames()` — L1180
- `updateSplitScreenScores(a, b, over)` — L1195
- `openSplitScreenGeneric(config)` — L1204
- `closeSplitScreen()` — L1213
- `openSplitScreen()` — L1219
- `openTournamentSplitScreen(matchKey, target, nameA, nameB, finishFn)` — L1244

**Team pairing preview**  (from L1264)
- `prefillEditForm()` — L1398

**Game log & CSV export**  (from L1558)
- `myGroups()` — L1590
- `defaultMatchGroup()` — L1600
- `applyVoiceVisibility()` — L1616
- `nwPhon(s)` — L1623
- `nwLev(a, b)` — L1634
- `nwScorePlayer(token, p)` — L1641
- `nwMatchPlayerToken(tokenRaw)` — L1660
- `nwWordsToNums(t)` — L1675
- `nwParseMatchTranscript(raw)` — L1683
- `nwApplyParsedToForm(p)` — L1715
- `set(id, entry)` — L1719
- `nwVoicePreviewHtml(p)` — L1729
- `nwVoiceMatchInit()` — L1741
- `stopListening()` — L1770
- `nwSeeded(p)` — L1859
- `nwShuffle(a)` — L1860
- `nwPairingRefreshList()` — L1862
- `nwPairingUpdateCount()` — L1873
- `nwPairingRender()` — L1878

**Profile card customization**  (from L1901)
- `nwPairingInit()` — L1910
- `nameFor(pid)` — L1982
- `showMatchOutcome(ok, message)` — L2052
- `savePendingMatch(payload, meta)` — L2071
- `loadPendingMatch()` — L2075
- `clearPendingMatch()` — L2078
- `handleSessionExpired()` — L2085
- `ensureRestoreHost()` — L2108
- `offerPendingMatchRestore()` — L2117
- `loadGameLog()` — L2152
- `gameLogGoto(p)` — L2202
- `renderGameLog()` — L2204
- `matchPermissions(m)` — L2261
- `matchGroupLabel(m)` — L2282
- `requestMatchChange(matchId, type, label, groupId, extra)` — L2287
- `editMatch(matchId, groupId)` — L2308
- `opts(sel)` — L2317
- `pickers(team, prefix)` — L2319

**Quests**  (from L2326)
- `close()` — L2341
- `editMatchScore(matchId, currentScoreA, currentScoreB, e)` — L2367
- `deleteMatch(matchId, encLabel, groupId)` — L2403

**Store & events admin**  (from L2430)
- `downloadCSV(filename, rows)` — L2435
- `loadRankings()` — L2469
- `gp(p)` — L2495
- `fetchRatingHistory(playerId)` — L2540
- `loadVisiblePlayers(opts = {})` — L2551
- `resolveBannerId(id)` — L2689
- `bgCss(id, url)` — L2763

**Image uploads**  (from L2765)
- `updatePageBackground()` — L2768
- `applyPageBackground(player)` — L2779
- `renderProfileCardBanner(player)` — L2786
- `toggleHeaderMenu()` — L2830
- `openSettingsModal()` — L2844
- `loadFinanceAccessList()` — L2857
- `opt(v, label)` — L2875
- `setGroupFinanceRole(groupId, playerId, role)` — L2890
- `setGroupMemberRole(groupId, playerId, role, wasRole, isSelf)` — L2906

**Profile bundle / cards / charts**  (from L2918)
- `setFinanceRole(playerId, role)` — L2922
- `closeSettingsModal()` — L2933
- `renderSettingsPickers(player)` — L2937
- `swatch(field, id, css, selected)` — L2946
- `submitClaimRequest()` — L2975
- `checkApprovalStatus()` — L3000
- `recomputeNow()` — L3013
- `loadAppSettings()` — L3028
- `setXpPublic(value)` — L3057
- `setVoiceEnabled(value)` — L3072
- `setInstantCreate(value)` — L3087
- `loadQuests()` — L3100
- `_renderQuestRow(q)` — L3109
- `_hdr(t)` — L3133
- `claimQuest(questId)` — L3141
- `loadQuestsAdmin()` — L3157
- `saveQuest()` — L3178
- `deleteQuest(questId)` — L3200
- `loadStore()` — L3212
- `catOf(i)` — L3237
- `cardHtml(i)` — L3242
- `buyStoreItem(itemId)` — L3274
- `onStoreImagePick(input)` — L3288
- `loadStoreAdmin()` — L3296
- `onStoreTypeChange()` — L3324
- `onStoreEffectChange()` — L3336
- `uploadStoreImage(file)` — L3352
- `saveStoreItem()` — L3369
- `deleteStoreItem(itemId)` — L3419
- `loadEventsAdmin()` — L3430
- `editEvent(e)` — L3452
- `saveEvent()` — L3460
- `deleteEvent(eventId)` — L3483
- `refreshEventBanner()` — L3495
- `loadClaimAudit()` — L3512
- `relinkAccount(usernameEnc, presetPlayerId)` — L3579
- `unlinkAccount(usernameEnc)` — L3587
- `unlinkAndStrip(usernameEnc, playerId)` — L3592
- `_claimAuditAction(bodyObj)` — L3597
- `loadUnconfirmedUsers()` — L3608
- `deleteUnconfirmedUser(username, email)` — L3635
- `loadClaimRequests()` — L3648
- `decideClaimRequest(requestId, action, requestType)` — L3688
- `escapeHtml(s)` — L3726
- `resizeImage(file, kind)` — L3743
- `isAnimatedImage(file)` — L3778
- `uploadCardImage(kind, fileInput)` — L3790
- `imageSrc(key)` — L3845
- `loadStoreCatalogOnce()` — L3851
- `renderStoreCosmeticStrip(kind, player)` — L3861
- `renderUploadStrip(kind, player)` — L3885
- `vsPlayerVisual(pid, snapshot)` — L3917
- `vsAvatarHtml(v, isWinner)` — L3933

**UPI payment card**  (from L3941)
- `teamBanner(side)` — L3950
- `gameScore(game, side)` — L3961
- `renderVsCard(idsA, idsB, opts = {})` — L3967
- `won(side)` — L3972
- `vsSideIds(side)` — L3997

**Finance tab (view-key + role gated)**  (from L3999)
- `setMyCardField(field, value)` — L4007
- `loadProfileBundle(playerId)` — L4075
- `renderTieredCard(icon, name, unit, tiers, currentValue)` — L4190
- `renderBinaryCard(icon, name, desc, achieved, detail)` — L4218
- `resetRatingZoom()` — L4290
- `loadProfileRatingChart(playerId)` — L4296
- `loadProfilePartnershipsAndRadar(playerId)` — L4382
- `loadProfileHeadToHead(playerId)` — L4431
- `loadProfileWithPartner(playerId)` — L4455
- `partnerGamesGoto(p)` — L4487
- `renderPartnerGames()` — L4489
- `skeletonHTML(lines = 3)` — L4522
- `showProfileSkeletons()` — L4529
- `renderXpPanel(player)` — L4542
- `xpForLevel(n)` — L4550
- `updateHeaderCoins()` — L4576
- `loadProfile()` — L4588
- `refreshProfile()` — L4618
- `refreshProfileIfShowing(affectedPlayerIds)` — L4635

**Match review & reorder (SuperAdmin)**  (from L4649)
- `renderPartnerRadar(data, highlightTournament, svgId = 'rada)` — L4656
- `loadHistory()` — L4712
- `renderHistory(data)` — L4730

**Auth UI (Cognito login/signup/session)**  (from L4780)
- `loadBadges()` — L4791
- `renderBadges(data)` — L4809
- `loadDiversity()` — L4842
- `renderDiversity(data)` — L4860
- `playerLabelById(playerId, fallbackName)` — L4881
- `playerLabelsById(playerIds, fallbackNames)` — L4885
- `loadHallOfFame()` — L4891
- `renderHallOfFame(data)` — L4913
- `loadAttendance()` — L4997
- `renderAttendance(data)` — L5016
- `refreshUpiCard()` — L5035
- `renderUpiCard()` — L5047
- `imageServiceFallback()` — L5069
- `xpVisible()` — L5097
- `applyFinanceRoleVisibility()` — L5103
- `finQS(extra)` — L5124
- `financeBaseUrl()` — L5135
- `finPost(path, method, bodyObj)` — L5139
- `populateFinanceSlots(group)` — L5163
- `_rememberedFinance(key)` — L5186
- `_rememberFinance(key, val)` — L5190
- `restoreFinanceMonth()` — L5196
- `populateFinanceGroups()` — L5205
- `reloadFinanceForGroup()` — L5233
- `tryAutoFinanceUnlock()` — L5238
- `myFinanceGroups()` — L5263
- `populateMyDuesGroups()` — L5268
- `loadMyDues(groupId)` — L5286
- `manageGroupSlots(groupId)` — L5331

**Init & session restore**  (from L5346)
- `assignSlotMembers(groupId, slotEnc)` — L5349

**Tournaments**  (from L5371)
- `transferGroupOwnership(groupId)` — L5377
- `setGroupPayee(groupId)` — L5396
- `requestFinanceAccess()` — L5419
- `financeUnlock()` — L5436
- `updateFinanceScopeNote(scopedTo)` — L5478
- `loadFinanceSummary()` — L5488
- `loadFinanceExpenses()` — L5510
- `resetExpenseEdit()` — L5548
- `addFinanceExpense()` — L5555
- `loadFinanceMembers()` — L5574
- `markMembersDirty()` — L5678
- `recalcMembers()` — L5685
- `renderBulkRosterList()` — L5697
- `bulkAddFromRoster()` — L5711
- `copyPreviousMonthMembers()` — L5726
- `addFinanceMember()` — L5762
- `loadFinanceWalkins()` — L5783
- `addFinanceWalkin()` — L5810
- `loadFinanceInsights()` — L5848
- `copyDuesForWhatsApp()` — L5862
- `pad(s, w)` — L5879
- `padL(s, w)` — L5880
- `line(n, o, r, p)` — L5881
- `done()` — L5891
- `fallbackCopy(text, cb)` — L5897
- `renderInsights()` — L5906
- `saveFinanceSettings()` — L5979
- `loadPublicWalkins()` — L6016
- `loadReviewDay()` — L6099
- `reviewOrderChanged()` — L6143
- `renderReviewList()` — L6149
- `applyReviewOrder()` — L6210
- `updateAuthUI()` — L6238
- `hiddenNow(id, btn)` — L6266
- `refreshMySession(statusElId)` — L6351
- `setStatus(msg)` — L6352
- `openAchievementsModal()` — L6380
- `closeAchievementsModal()` — L6389
- `openAuthModal()` — L6390
- `closeAuthModal()` — L6391
- `showAuthView(view)` — L6392
- `setAuthSession(session, user, opts = {})` — L6400
- `closeCompleteProfileModal()` — L6420

**Live scoring inside tournaments**  (from L6421)
- `openCompleteProfileModal()` — L6421
- `showCompleteProfileMode(mode, preselectPlayerId)` — L6436
- `populateClaimPicker(preselectPlayerId)` — L6444
- `submitClaimProfile()` — L6468
- `closeCompleteProfileModal()` — L6508
- `sanitizeNickname(raw)` — L6514
- `editDistance(a, b)` — L6519
- `checkForExistingPlayer(name, typedNickname, statusEl)` — L6541
- `submitCompleteProfile()` — L6596
- `finishRequestAndSignOut(message)` — L6672
- `doLogin()` — L6678
- `doNewPassword()` — L6731
- `doSignup()` — L6742
- `doConfirmSignup()` — L6759
- `doResendConfirmCode()` — L6790
- `doForgotPassword()` — L6801
- `doConfirmForgotPassword()` — L6816
- `doLogout()` — L6828
- `restoreSession()` — L6872
- `restoreTabFromHash()` — L6916
- `addManualTeamRow()` — L6976
- `collectManualTeams()` — L7012
- `loadTournamentGroupOptions()` — L7025
- `loadTournamentParticipantsChecklist()` — L7034
- `updateParticipantsCount()` — L7064
- `collectTournamentParticipants()` — L7076
- `loadTournamentsList()` — L7080
- `submitTournamentCreation(payload)` — L7087
- `collectAllEntities(t)` — L7246
- `getAllTeamEntities(t)` — L7262
- `renderTeamCompositionBars(t, containerId)` — L7280
- `populateSubstitutionSection(t)` — L7315
- `updateSubOldPlayerOptions()` — L7326
- `formatGames(games)` — L7415
- `applyTournamentViewMode()` — L7422
- `matchTotals(match)` — L7428
- `truncateBracketName(name, maxChars = 22)` — L7436
- `renderBracketView(t)` — L7441
- `renderTournament(t)` — L7557
- `generateTournamentRecap(t)` — L7731
- `downloadTournamentImage()` — L7763
- `loadImg(src)` — L7790
- `sideVisuals(side)` — L7800
- `drawCard(x, y, w, match, isFinal)` — L7807
- `drawAvatars(ctx, x, y, side, isWinner)` — L7853
- `paintTeam(ctx, x, y, w, h, side, fallback)` — L7872
- `roundRect(ctx, x, y, w, h, r)` — L7900
- `copyTournamentRecap()` — L7910
- `item_has_third_place(t)` — L7921
- `submitGroupScore(tournamentId, subgroup, fixtureId)` — L7925
- `submitGroupScoreDirect(tournamentId, subgroup, fixtureId, score)` — L7931
- `submitKnockoutScore(tournamentId, roundIndex, matchIndex)` — L7950
- `submitKnockoutScoreDirect(tournamentId, roundIndex, matchIndex, sc)` — L7956
- `submitThirdPlaceScore(tournamentId)` — L7975
- `submitThirdPlaceScoreDirect(tournamentId, score_a, score_b, override)` — L7981
- `getTournamentLiveLog(matchKey)` — L8004
- `tournamentLivePoint(matchKey, side, target)` — L8009
- `tournamentUndoPoint(matchKey, target)` — L8018
- `updateTournamentLiveDisplay(matchKey, target)` — L8024
- `finishGroupLiveGame(matchKey, tournamentId, subgroup, fixtur)` — L8042
- `finishKnockoutLiveGame(matchKey, tournamentId, roundIndex, matc)` — L8051
- `finishThirdPlaceLiveGame(matchKey, tournamentId)` — L8060
- `renderLiveScoreControls(matchKey, target, finishCallExpr, nameA,)` — L8069
- `applyTheme(theme)` — L8162
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
