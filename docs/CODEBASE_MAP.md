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
> **Shipping a change?** See `docs/DEPLOY.md` for the local zip→staging→main→tag runbook — hand back a folder-wise delta zip (changed files only, real repo-relative paths), not the whole repo.

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
| POST `/players/merge` | players | COGNITO | (SuperAdmin, `CONFIRMATION_CODE`-gated) `merge_players` — reassigns a duplicate profile's matches/group membership/open sessions onto another profile, re-points its login if only it has one, then deletes it. Refuses the whole merge, no partial writes, if either profile appears anywhere in tournament data (`_contains_id`, recursive) or has any finance record, or if both already have their own separate login — those need manual resolution. Doesn't touch `ratings_after`/xp/coins/level on reassigned matches; run the existing `/recompute` afterwards to rebuild those from the corrected history. Added 2026-08-30 (v1.79.0) after real duplicate-profile reports (root cause: registering with no group selected — see `defaultRegisterGroup` below). |
| PUT `/rename-self` | players | COGNITO | Self-service nickname change |
| PUT `/update-my-card` | players | COGNITO | Self avatar/banner customization |
| POST `/claim-player` | players | COGNITO | Link my account to an existing unclaimed player |
| POST `/claim-request` | players | COGNITO | Ask an admin to link me |
| POST `/action-request` | players | COGNITO | File a destructive-action request for approval |
| GET `/claim-requests` | players | COGNITO | (Admin) list pending requests |
| POST `/claim-request-decide` | players | COGNITO | (Admin, or a group owner/admin for the owner-decidable types — `OWNER_DECIDABLE_TYPES` = claim, edit_own_name, finance_access, match_edit, match_delete, since 2026-08-20) approve/reject a request. A request's own `group_id` (stamped at creation) decides whether an owner can act on it — no `group_id` (ungrouped, one-off match) stays SuperAdmin-only. `_owner_may_decide`'s error text/comment corrected 2026-08-28 (was stale, still said only claim/rename). Frontend `loadClaimRequests` now shows a group-name/"Global / no group" badge per request. |
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
| POST `/register-and-join` | groups | COGNITO | Register a friend + join in one call. Frontend's Bulk register players card now also uses this route (was the group-unaware `/register`), with a group `<select>` + inline "Create & use" (→ `/group-create`) added to it (owner request, 2026-08-28) so bulk-added players can be dropped straight into a chosen or brand-new group. |
| POST `/sessions` | groups | COGNITO | Create a temporary session roster for a group (owner/admin only) — 2026-08-29, see §3 `networth-sessions`. |
| GET `/group-sessions/{group_id}` | groups | NONE | List a group's currently-open sessions (same unauthenticated posture as `GET /groups/{group_id}`). |
| PUT `/sessions/{session_id}/close` | groups | COGNITO | Close a session (creator, or owner/admin of its group, or SuperAdmin). Soft-close (`is_open:false`), never deleted. |
| POST/DELETE `/sessions/{session_id}/members[/{player_id}]` | groups | COGNITO | Add/remove a session member — any member (any role) of the group may edit it, a deliberately lower bar than creating one. Add accepts `{player_id}` (existing) or `{new_player_name}` (register-on-the-fly, reuses the same helper as `/register-and-join`). Either way the player is added ONLY to the session, never to the real group's `member_ids` — a session is always a guest-style overlay. Wired into Quick tap's session bar (`frontend/js/app.js`, "Sessions" block) — selecting one replaces the avatar grid's roster with the session's `member_ids`. |
| POST `/record-match` | matches | COGNITO | Record a match (linked members only) |
| GET/PUT/DELETE `/matches` `/matches/{match_id}` | matches | NONE/COGNITO | List / fix / delete matches. PUT/DELETE authorized by `_caller_may_edit_match`: SuperAdmin, or owner/admin of the match's own group — `update_match` already accepts `team_a`/`team_b` changes (not just score) from anyone this authorizes. Frontend previously only ever let a group owner use the slower request/approval flow (`isSuperAdmin()`-only gate on direct action, and no participant-edit path in that flow at all) even though the backend already trusted them to act directly — fixed 2026-08-28 via `canActOnMatchDirectly()`, wired into `editMatch`/`editMatchScore`/`deleteMatch`/`matchPermissions`, so a group owner now gets the same full players+score edit/delete capability as SuperAdmin on their own group's matches. |
| POST `/reorder-matches` | matches | COGNITO | (Admin) reorder by re-stamping timestamps |
| POST `/recompute` | matches | COGNITO | (Admin) full replay to rebuild ratings/XP |
| GET/POST/DELETE `/events` | matches | NONE/COGNITO | XP-multiplier events (read public) |
| GET/POST/DELETE `/quests` + POST `/quest-claim` | matches | NONE/COGNITO | Weekly quests + claim reward |
| GET/POST/DELETE `/achievements` + POST `/achievement-claim` + POST `/achievement-unclaim` | matches | COGNITO | Lifetime milestones (2026-08-28, admin usability follow-up 2026-08-29) — sibling of quests but never resets; progress read from `compute_achievements`, reward XP/coins reuse `quest_xp`/`quest_coins`, a cosmetic reward lands in `owned_items` (regular store item, `active:false` so it's earn-only, never buyable). Sentinel row `__achievements__` in the matches table, added to every sentinel-exclusion list alongside `__events__`/`__quests__`. `/achievement-unclaim` (SuperAdmin only) reverses one player's claim on one achievement — clears the claim flag, takes back the XP/coins/cosmetic (clamped at 0) — so a claim made before an achievement's reward was properly configured can be redone. Frontend: "My achievements" in the Settings modal (claim UI) + a SuperAdmin "Achievements" admin panel under Reviews & Approvals (mirrors the Quests admin panel) — the reward-cosmetic field is a `<select>` populated live from the real Store catalog (not a raw item-id text box), the list has an **Edit** button (`editAchievement`, updates in place via the same POST carrying `achievement_id`) and a **Revoke a claim** button (resolves a typed nickname, calls `/achievement-unclaim`) alongside Delete — plus `badgeSvg(tier, glyph)` (implements `docs/BADGE_FORMAT.md`'s hexagon recipe as data-URI SVGs) and a one-click "Create starter achievements" seeder (9 achievements + matching badge cosmetics, idempotent). |
| ANY `/profile-secure/{proxy+}` | matches | COGNITO | Gated profile-data catch-all |
| POST `/create-tournament` | tournaments | COGNITO | Create tournament |
| GET/DELETE `/tournaments` `/tournaments/{proxy+}` | tournaments | NONE | List/get/delete + score submission |
| ANY `/tournament-draft` `/tournament-draft/{proxy+}` | tournaments | COGNITO | Manual-draft mode: leaders, pool board (Phase A); `remove-player` — organizer excuses a group member (e.g. themselves, if not playing) from the roster entirely, `pools_open` only, rejects removing a current leader; auction (`start-auction`/`open-lot`/`bid`/`close-lot`/`skip-lot`/`state`, Phase B); tie-based schedule (`generate-schedule`/`pick-tie-player`/`group-tie-score`/`knockout-tie-score`, Phase C) — `generate-schedule` builds either the original flat round-robin (`manual_draft.num_groups<=1`, the default, byte-identical to before) or real separate named groups (A, B, C...) with squads randomly split and a round-robin only within each group, top `manual_draft.advance_per_group` per group advancing to a combined knockout (boundary ties injected as an extra tie via `_inject_group_tiebreakers_if_needed`; qualifiers are paired into the knockout bracket DETERMINISTICALLY via `_advance_squads_to_knockout_from_groups` — sorted by group name and paired consecutively (A-B, C-D, ...), changed 2026-08-23 from a `random.shuffle` after a live event's real bracket didn't match the app's randomly-drawn one, with the pre-existing same-group-rematch-avoidance swap kept as a safety net for `advance_per_group > 1` — both mirror the legacy `groups_then_knockout` format's own `inject_tiebreakers_if_needed`/`advance_to_knockout`); both `generate-schedule` and `regenerate-schedule` share `_build_group_stage`, which now also rejects any `num_groups` that would leave a group with fewer than 2 squads (previously only `num_groups > squad count` was rejected, so e.g. 4 squads split into 4 groups silently produced 4 unplayable 1-squad groups); `regenerate-schedule` — organizer-only repair action, only while still `group_stage` and nothing in it has been played yet: optionally updates `manual_draft.num_groups`/`advance_per_group` from the request body, then reruns `_build_group_stage` to rebuild the whole schedule from the existing (untouched) squads, without redoing leaders/pools/auction; `pick-tie-player` nominates either one `player_id` (singles) or a `player_ids` pair (doubles, per `manual_draft.match_type`); a leader nominates for their own squad, and the organizer can now also nominate for either squad given `squad_id` to disambiguate — the frontend's tie-card picker now actually offers this to an organizer viewing a tie (previously it only ever rendered a picker for the tie's own two leaders, so the backend's organizer path had no UI to trigger it from); `rename-squad` — organizer or that squad's own leader renames it, `squads_locked` onward (cosmetic, never locked); `move-squad-player` — organizer rebalances a picked player to a different squad, `squads_locked` only (before a schedule exists); `substitute-squad-player` — organizer swaps a squad member for a new replacement, `squads_locked` through `knockout`, clears any not-yet-played lineup pick for the outgoing player, leaves played-match history untouched; `organizer-assign` — organizer directly awards a queued player to a chosen leader for a chosen amount, no open lot / leader bid required (for leaders without app access); `GET /tournament-draft/{id}` — privileged pool/auction detail (organizer always, a leader only while their phase is still live), the only route that ever returns real `pools`/`draft` data — the public `GET /tournaments/{id}` always redacts both for manual-draft tournaments; `set-squad-pairs` + `manual_draft.group_mode='cross_squad'` (owner request, 2026-08-21, same-day rush) — an alternate group-stage shape where each squad first fixes exactly `num_groups` pairs (or solo reps, for singles) via `set-squad-pairs` (organizer or that squad's own leader), then `_build_cross_squad_group_stage` sends exactly ONE of those fixed units from EVERY squad into EVERY group (stored as `item['reps']`, id shape `{squad_id}::rep{n}`, each carrying `parent_squad_id`) instead of splitting whole squads across groups; a cross-squad tie's matches are pre-filled with both reps at build time via the shared `_fill_cross_squad_match_players` helper (used for the group stage, the first knockout round, later knockout rounds, and the third-place match) since the rep is already fully fixed — no `pick-tie-player` lineup step; `_tie_side_leader_id` resolves a tie's squad_a/squad_b (a rep_id in this mode) back to the real leader id so scoring auth (`_authorize_tie_scorer`) and the champion banner (`champion_squad_id`) still work per-squad; `compute_squad_standings` now reads `item['squads']` merged with `item['reps']` (a no-op for every tournament without reps); new `compute_squad_standings_by_parent` rolls a squad's several reps back into one overall standings row (used for `get_tournament`'s `squad_standings` when `group_stage.cross_squad` is set — `group_standings`, the per-group breakdown, stays rep-level since that's who actually plays within a group); `regenerate-schedule` also accepts `group_mode` to switch an already-generated tournament between the two modes in place (clears stale `item['reps']` when switching back to `'squads'`); `generate-schedule` accepts the same `group_mode` override for a tournament's first-ever schedule generation, so the frontend's Pairing panel can drive both first-time generation and in-place repair through one "Generate groups" button/function; the frontend's Pairing panel labels pair slots "Pair N:" (not "Group N:") since which named group a pair lands in is decided randomly at generation time, never chosen by the user, and `renderDraftScheduleView` suppresses the per-group squad-name cards entirely (showing a plain "no matches yet" message instead) whenever a `group_stage.groups` exists with zero total ties, to avoid the misleading pre-generation/stale-schedule view; `create_manual_draft_tournament` also accepts `group_best_of`/`knockout_best_of` (each `1` or `3`, default `1` — byte-identical to every tournament created before this existed), stored in `manual_draft`; `_score_tie_match` picks `best_of` per-stage off its existing `stage_label` parameter (`'group'` vs `'knockout'`/`'third_place'`) rather than a flat field manual-draft items never actually had — `_submit_game`'s `needed_wins=(best_of//2)+1` engine needed no changes, already shared/generic with legacy tournaments — and the frontend's `renderDraftBracketView` (a tie-shaped sibling of the legacy `renderBracketView`, reusing `draftSquadName`/`truncateBracketName`/`item_has_third_place`) now backs the shared View:Table/Bracket toggle once a manual-draft tournament reaches `group_stage`/`knockout`/`completed` (toggle hidden + forced to table before that, since nothing bracket-shaped exists yet); `get_tournament` also attaches `projected_knockout` (via `compute_projected_knockout`) whenever `status=='group_stage'` and at least one group tie is still pending — a read-only preview of the knockout pairing seeded from the CURRENT (partial) standings via the same `build_knockout_tie_round` the real knockout uses, absent once every tie decides (the real `knockout` takes over) or when real separate named groups are in play (advance_per_group/tiebreak-injection eligibility isn't replicated for the projection); rendered by the frontend's `renderProjectedKnockout` as a plain, non-scoreable card in `renderDraftScheduleView`; `cancel-group-tie-match`/`cancel-knockout-tie-match` and `forfeit-group-tie-match`/`forfeit-knockout-tie-match` (owner request, 2026-08-23, live event: 2 group matches could never be played, players unavailable) — organizer-only (stricter than normal scoring, which a tie's own leader can also submit), act on any not-yet-played match: cancel resolves it with no winner and no Elo change (excluded entirely from the tie's win/point-diff tally in `_update_tie_progress`; if every match in a tie ends up cancelled the tie itself resolves `decided=True, winner_squad_id=None, cancelled=True` rather than falling into the pre-existing genuine-deadlock branch), forfeit (`{tie_id, match_index, forfeited_by: 'a'|'b'}`) awards the match win to the other side with no Elo change and does NOT require either side to have nominated a lineup first (covers exactly the case where the absent side never got to nominate anyone) — both share `_after_group_tie_resolved`/the same round-advance check `record_knockout_tie_score` already used, so cancelling/forfeiting the last pending tie in a group or knockout round advances the tournament exactly like a real score would; `_update_tie_progress` also now decides a tie EARLY, the moment one side clinches an unbeatable majority of `matches_per_tie` (`needed_wins = len(matches)//2 + 1`) — added 2026-08-23 after a live best-of-3 knockout tie finished 2-0 but still showed a pointless, still-scoreable Match #3; once a tie is `decided` this way, `_score_tie_match`/`_cancel_tie_match`/`_forfeit_tie_match` all reject any further action on its remaining match slot(s) (400, "this tie is already decided"), and the frontend's `renderTieMatchRow` renders that slot as a plain "Not needed" line instead of active controls, and `renderTieCard`'s header now shows a "Best of N (first to K)" label computed from `tie.matches.length`; `compute_player_tournament_scores` excludes cancelled matches so they don't inflate the tournament-scoped leaderboard; for real named groups instead, `get_tournament` attaches `group_stage_projection` (via `compute_group_stage_projection`) — per group, that group's own standings plus `advancing_ids`/`contested_ids` (the latter populated only on an exact tie at the `advance_per_group` cutoff, mirroring `_inject_group_tiebreakers_if_needed`'s own boundary check) and `pending_ties`; deliberately does NOT project a knockout PAIRING for this case (even though the real pairing is now deterministic, not random — see `_advance_squads_to_knockout_from_groups` above) since projecting it correctly would mean replicating `advance_per_group`/tiebreak-injection eligibility here too, which this intentionally doesn't do — rendered by the frontend's `renderDraftBracketGroupsPanel` into the new `#bracket-groups-panel` div (index.html, sits above `#bracket-svg`, toggled together with it by `applyTournamentViewMode`); separately, `renderDraftBracketView` also draws the flat round-robin case's `projected_knockout` directly into the SVG itself (one dashed preview box) instead of the plain "no bracket yet" text, since that case *is* a single deterministic pairing; `create_manual_draft_tournament` also accepts optional `final_matches_per_tie`/`third_place_matches_per_tie` (owner request, 2026-08-23: "next time onwards will it ask me how many sets for each semis or finals or third place matches?" — the real live event ran semis+final at one format (best-of-3 to 11) but third place as a single match to 21, and until this the whole knockout stage shared one global `knockout_matches_per_tie`) — both default to `knockout_matches_per_tie` when omitted (byte-identical to every tournament created before this existed), and both are snapshotted into `item['knockout']` (alongside the existing `matches_per_tie`) the moment the knockout bracket is first built, not re-read live from `manual_draft` later; `_advance_knockout_ties_if_round_complete` picks `final_matches_per_tie` for the round that comes out to exactly one tie (the final) and `third_place_matches_per_tie` for the auto-created third-place match, falling back to the base `matches_per_tie` (semifinal-tier) for every other round; the very first knockout round built straight off group-stage qualifiers also uses `final_matches_per_tie` in the edge case where it's already down to a single tie (e.g. only 2 total qualifiers, no separate semifinal round to speak of); the create form's "Final matches per tie"/"Third-place matches per tie" fields are left blank by default (placeholder "same as knockout") and are only included in the create payload when the organizer actually fills them in, so the backend's own default-to-`knockout_matches_per_tie` fallback is what actually applies for the common case; `computeLeaderboardRows` (frontend) had its champion/runner-up/third-place placement highlighting fixed for `group_mode='cross_squad'` tournaments (owner report, 2026-08-23, live tournament: "nothing is getting higlighted") — `finalTie.winner_squad_id`/`squad_a`/`squad_b` and `third_place_match.winner_squad_id` are REP ids (`"{squad_id}::repN"`) in cross_squad mode, but the placement lookup was keyed by plain squad id (`squadOf[pid]`, built from `t.squads`, which spans a squad's whole roster across every rep) — so the lookup silently matched nothing for any row on any cross_squad tournament; a reverse `pairKey -> repId` map built from `t.reps` is now preferred over the plain-squad-id fallback, which also fixes a second latent collision (two of a squad's own reps finishing in two different places, e.g. one runner-up + a different rep taking third, used to overwrite one shared squad-keyed placement entry) since each rep now gets its own independent entry; for a normal (non-cross_squad) tournament `t.reps` is empty so every row falls through to the plain squad-id lookup exactly as before, unchanged — shared by both the HTML leaderboard and the downloadable leaderboard share image, since both already run through this one helper; same-day follow-up (owner report: "why is the 3 position team not moving up ... this is like doing based on number of played matches or what?") — `computeLeaderboardRows`'s row SORT previously ignored placement entirely and ranked purely by regular performance (wins/point-diff/matches-played), so a non-podium pair with a better raw win tally could (and did) outrank the actual bronze medalist, whose run includes the semifinal loss that put them in the third-place match to begin with; fixed by pinning the exact podium-DECIDING pair (`isDecidingPair`, not every squadmate who merely shares that squad's thin tier-colored edge) to the top 3 rows in gold/silver/bronze order ahead of the existing performance tiebreakers, which now only apply within a tier and among the non-podium rest; `update_elo_and_log` (backend, called for every manual-draft group/knockout/third-place match) now also updates `previous_rating`/`xp`/`level`/`coins`/`coins_earned`/`games_played` on each player, not just `rating` (owner report, 2026-08-24, `networthmatches.csv` attached: players who'd genuinely played 5+ tournament matches were still shown as "3/5 games" — provisional, not yet ranked — on the View Rankings screen, which reads a player's persisted `games_played` directly) — mirrors `matches/index.py`'s `_play_and_log` exactly (XP/level/coin constants and helpers duplicated per KNOWN_ISSUES #6, since `matches/index.py`'s own `_play_and_log` is only ever invoked with `tournament_id=None` in practice and can't be reused cross-lambda); Elo `rating` math itself is unchanged, this only adds the missing fields. A `recompute_all_ratings()` run (the `matches/index.py` copy, via the admin `/recompute` route) is recommended once after deploy to backfill every already-recorded tournament match's `games_played`/xp/level/coins, not just future ones |
| ANY `/finance/{proxy+}` | finance | **NONE (legacy open)** | View-key/confirmation-code gated finance ops. `_settlement_rows`'s group-wide (slot-less) expense bucket now divides by TOTAL slot-enrollments ("portions"), not distinct members (owner request, 2026-08-24: "can we switch it back to members in all the slots ... if a group has 2 slots and 3 members share both the groups then they should have 2 portions each" — reverses the narrower 2026-08-20 decision that deduped the expense side to distinct members while leaving only walk-ins slot-weighted; now both sides use the identical `total_slots` denominator) — `cost_per_head`/`residual_per_head` on that bucket are therefore PER-PORTION prices, not per-member ones; `distinct_member_count` is separately exposed alongside the now-portion-based `player_count` so the UI can show "24 portions (15 members)" instead of a bare number that reads like a headcount; new `expense_shares`/`expense_residual_shares` per-member dicts (mirroring the pre-existing `walkin_shares` exactly, including its precision — computed from the raw bucket totals divided directly by `total_slots` and multiplied by each member's own slot count, not from the already-rounded-to-cents per-portion price, to avoid compounding rounding error) are what `my_settlement` and `insights` actually charge/credit each member now, replacing their old flat `cost_per_head`/`residual_per_head` reads for this bucket. Frontend: the `/finance/summary` table's "Members"/"Per head"/"Residual / head" columns relabel for the group-wide row to make the portion-based pricing legible (was previously silently wrong-looking after the backend change alone); Insights' "Copy for WhatsApp" button (unchanged) is now joined by a new "Copy table as image" button (owner request, same day: mobile portrait squishes the on-screen table) — `copyInsightsTableAsImage` hand-draws the full on-screen 7-column table to a canvas (matching this file's existing `downloadTournamentImage`/`downloadDraftLeaderboardImage` pattern, no third-party DOM-to-canvas library) and copies the PNG straight to the clipboard via `navigator.clipboard.write`/`ClipboardItem`, falling back to a plain download (anchor appended to the DOM before `.click()`, matching `downloadCSV`'s more robust pattern rather than the three existing tournament image functions' bare unattached click) when clipboard image support isn't available. Walk-in records get a new optional numeric field `sessions_covered` (owner report, 2026-08-28: Insights' "Sessions paid" for non-members equaled a raw COUNT of walk-in fee entries, not real sessions — a guest who pays a lump sum in one entry, like paying for the whole month at once, showed a tiny fake number regardless of what the fee covered) — defaults to 1 when absent/blank/zero so every existing entry and every per-session payer is unaffected; `insights()`'s guest-conversion loop now sums `sessions_covered` instead of counting entries. Frontend: new "Sessions covered" field on the walk-in add/edit form, auto-suggested from the guest's own most recent per-session rate on fee blur (`suggestWalkinSessions`) but always overridable and never re-clobbered once hand-edited; walk-ins table gets a "Sessions" column. |
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
| `networth-sessions` | `session_id` (HASH) | `group_id`, `name` (e.g. "7-8 PM"), `member_ids`, `is_open`, `created_by`, `created_at` — temporary, group-scoped rosters that overlay a real group without ever touching its own `member_ids` (2026-08-29); soft-closed, not deleted, so a closed session stays as history |
| `networth-matches` | `match_id` (HASH) | `date`(ISO), `match_type`(singles/doubles), `team_a`/`team_b`(ids), `team_a_names`/`team_b_names`, `score_a`/`score_b`, `points_to_win`, `winner`, `ratings_after`, optional `group_id`, `tournament_id`+`stage`, `point_log`+`momentum`, `approved` |
| `networth-tournaments` | `tournament_id` (HASH) | fixtures/brackets, entities, standings, format, `group_id`. `format: 'manual_draft'` items (new) additionally nest `manual_draft` (config, incl. `group_best_of`/`knockout_best_of` — each `1` or `3`, default `1`), `leaders`, `pools` (`assignments`/`unassigned`/`locked`), `draft` (Phase B - `status`, `queue`, `current_lot`, per-leader `remaining_budget`/`pool_picks`/`squad_member_ids`, `unsold`), `squads` (Phase B - built when the draft auto-completes), `group_stage`/`knockout` (Phase C - squad-vs-squad `ties`, each holding `matches_per_tie` fixture-shaped matches + `wins_a/b`/`point_diff_a/b`/`decided`/`winner_squad_id`; `knockout` reuses the same top-level key the legacy single-match format already uses), `champion_squad_id` (Phase C, set on completion) - no new table, same item shape philosophy as `subgroups`/`knockout` |
| `networth-finance` | `record_id` (HASH) | typed records (expense/member/walkin/settings) — `record_type` prefix scan |
| `networth-claim-requests` | `request_id` (HASH) | pending claim / new-profile / edit-name / match-action / finance-access requests |
| `networth-progress-history` | `history_id` (HASH) | locked-in weekly/monthly/yearly winner snapshots (written by scheduler) |

**Elo is path-dependent:** any edit/delete/reorder of a historical match requires a full
replay — `recompute_all_ratings()` (present in both matches and tournaments lambdas).

---

## 4. Infrastructure (`infrastructure/template.yaml`, ~3620 lines)

- **8 Lambdas** (all `python3.12`, handler `index.handler`): whoami, register-player, players,
  groups, finance, matches, tournaments, progress-scheduler.
- **Cognito**: `UserPool` + `CognitoAuthorizer` (IdentitySource = `Authorization` header).
  `UserPoolTier: ESSENTIALS` (added 2026-08-24, owner report: "i was able to forget password and use
  my old password again" — the default `LITE` tier doesn't support `PasswordHistorySize`, the
  CloudFormation-native property that blocks reuse of a user's last N passwords across every
  password-setting path Cognito has, forgot-password included; set to `3` here) — negligible cost
  for a club-sized user base (Essentials is free for the first 10,000 MAU).
- **EventBridge rule** → `progress-scheduler` (weekly match-approval backfill + winner snapshots).
- **S3 `WebsiteBucket`** doubles as the uploads bucket (`uploads/` prefix) → deploy must NEVER
  `s3 sync --delete` or it wipes user cosmetics (workflow uses explicit `cp`).
- **Env-var wiring** each Lambda gets only the tables it needs (e.g. finance gets FINANCE/PLAYERS/MATCHES
  + `FINANCE_VIEW_KEY` + `CONFIRMATION_CODE`; players gets `USER_POOL_ID`, `UPLOADS_BUCKET`).
- Auth split across the API: 67 method-resources are `NONE`, 56 are `COGNITO_USER_POOLS` (recounted
  2026-08-29 alongside adding the 10 Sessions methods — this line had drifted out of date before that
  too, so treat it as a snapshot to re-verify next time a batch of routes changes, not a hand-tracked
  running total).
- **8 DynamoDB tables**: players, groups, sessions (new 2026-08-29), matches, tournaments, finance,
  claim-requests, progress-history.
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

#### `players` — 2146 LOC
_NetWorth - players Lambda (list all, update one, delete one)_

**Module constants:** `CLAIM_REQUESTS_TABLE`, `USER_POOL_ID`, `UPLOADS_BUCKET`, `GROUPS_TABLE`, `MATCHES_TABLE`, `SESSIONS_TABLE`, `FINANCE_TABLE`, `TOURNAMENTS_TABLE`, `CONFIRMATION_CODE`, `ALLOWED_AVATARS`, `ALLOWED_BANNERS`, `ALLOWED_BACKGROUNDS`, `OWNER_DECIDABLE_TYPES`, `_APP_SETTINGS_ID`, `_STORE_CATALOG_ID`, `_STORE_ITEM_TYPES`, `FINANCE_LEVELS`, `ALLOWED_UPLOAD_TYPES`, `UPLOAD_KINDS`, `FREE_CARD_LAYOUTS`, `MAX_UPLOADS_PER_KIND`, `FREE_RENAMES`

| Function | Args | Line | What it does |
|---|---|---|---|
| `sanitize_nickname` | raw | 56 | Same rule as register_player's version (duplicated on purpose - |
| `handler` | event, context | 65 | — |
| `lookup_email_for_login` | identifier | 122 | Resolves a player_id, exact name, or exact nickname to the email |
| `list_players` |  | 157 | — |
| `_caller_claims` | event | 201 | — |
| `_can_self_rename` | claims | 205 | Placeholder gate - the achievement/level system this is meant to |
| `claim_player` | event | 230 | Self-service: link my Cognito account to an EXISTING, UNCLAIMED |
| `_scan_all` | table | 298 | Full-table scan that follows LastEvaluatedKey - a bare .scan() returns |
| `_is_super_admin` | claims | 312 | — |
| `_linked_player_is_live` | claims | 319 | True only if the caller's custom:player_id resolves to a player that |
| `create_claim_request` | event | 329 | Anyone logged in but not yet linked can ASK to be linked to an |
| `_caller_owned_group_ids` | claims | 399 | The set of group_ids where the caller's linked player is owner or admin. |
| `_player_group_ids` | player_id | 415 | Every group_id whose roles map contains this player. |
| `_owner_may_decide` | req, owned_group_ids | 423 | True if a group owner/admin (owning owned_group_ids) may act on req: |
| `_audit_attr` | user, name | 440 | — |
| `_cognito_users_all` | cognito | 444 | — |
| `audit_claims` | event | 454 | SuperAdmin: audit Cognito account <-> player linkage (the claim_audit.py |
| `claim_audit_action` | event | 526 | SuperAdmin link/unlink. link: point an account at a player AND stamp the |
| `list_unconfirmed_users` | event | 579 | SuperAdmin-only: Cognito accounts stuck in UNCONFIRMED (signed up but |
| `delete_unconfirmed_user` | event | 611 | SuperAdmin-only: delete a single UNCONFIRMED Cognito account by username. |
| `list_claim_requests` | event | 637 | — |
| `create_action_request` | event | 656 | A non-SuperAdmin asking for a destructive action instead of doing |
| `_create_new_profile_request` | claims, body | 723 | Creating a brand-new profile. By default this is a REQUEST an admin |
| `_get_app_setting` | key, default | 806 | App-wide flags live in one reserved row of the players table, keyed |
| `get_app_settings` | event | 815 | — |
| `set_app_setting` | event | 829 | — |
| `_load_catalog` |  | 883 | — |
| `list_store` | event | 888 | Public read - anyone can browse the store. Returns the catalog. |
| `save_store_item` | event | 894 | — |
| `delete_store_item` | event | 929 | — |
| `purchase_store_item` | event | 942 | A player spends coins on an item. Coins are deducted by bumping |
| `_create_edit_name_request` | claims, body | 987 | Renaming is now self-service-only: the target is always the |
| `_approve_edit_name` | req, claims | 1033 | — |
| `_approve_new_profile` | req, claims | 1049 | Creates the player only at approval time, and links it to the |
| `_create_match_request` | claims, body, action_type | 1085 | A match edit or delete, filed as a request rather than executed. The |
| `_create_finance_access_request` | claims, body | 1133 | A member asking for a finance role (view / write / delete) IN A GROUP. |
| `_approve_finance_access` | req | 1186 | — |
| `decide_claim_request` | event | 1212 | Approve or reject. On approval this writes the link on BOTH sides: |
| `create_upload_url` | event | 1371 | Hands back a short-lived presigned PUT. The browser uploads straight |
| `_valid_upload_key` | value, player_id, kind | 1435 | An uploaded image is referenced by key, and the key is checked |
| `_owns_store_cosmetic` | player, key, kind | 1445 | True if `key` is the image of a store cosmetic the player OWNS whose |
| `_owns_card_layout` | player, layout | 1471 | True if the player may use this stats layout - free ones always, |
| `_owns_value_cosmetic` | player, kind, value | 1479 | True if the player owns a store item of this cosmetic `kind` whose |
| `_rotate_uploads` | player_id, kind, new_key | 1500 | Maintains the player's short list of custom images, newest first, |
| `update_my_card` | event | 1532 | Self-service avatar/banner customization for the CALLER'S OWN |
| `_consume_perk` | player, player_id, effect_kind | 1707 | Spends one token of a perk the player owns (by store item effect |
| `rename_self` | event | 1736 | Self-service nickname change for the CALLER'S OWN linked player. |
| `update_player` | player_id, event | 1776 | — |
| `_contains_id` | obj, target_id | 1863 | Recursively searches an arbitrarily nested structure for target_id, |
| `merge_players` | event | 1884 | SuperAdmin tool to fix duplicate player registrations - e.g. someone |
| `delete_player` | player_id, event | 2080 | — |
| `_cognito_username_for_email` | cognito, email | 2129 | The username is not always the email, so it has to be looked up. |
| `_response` | status_code, body_dict | 2137 | — |

#### `groups` — 1021 LOC
_NetWorth - groups Lambda_

**Module constants:** `CONFIRMATION_CODE`, `VALID_ROLES`, `FINANCE_ROLE_LEVELS`

| Function | Args | Line | What it does |
|---|---|---|---|
| `sanitize_nickname` | raw | 65 | Same rule as register_player's version (duplicated - separate |
| `_scan_all` | table | 77 | Full-table scan that follows LastEvaluatedKey - a bare .scan() returns |
| `handler` | event, context | 95 | — |
| `_authorize_group_action` | group_id, claims | 172 | Shared check for Epic 4's group-scoped write actions: SuperAdmin, or |
| `delete_group_enforced` | group_id, event | 188 | Dual-gated (Epic 4 increment 3): a valid Cognito identity that's |
| `remove_player_enforced` | group_id, player_id, event | 200 | Same dual-gate as delete_group_enforced, for member removal. |
| `_requires_linked_member` | claims | 208 | Signing up is not the same as being a member. Cognito self-signup is |
| `_create_player_record` | name, skill_level, nickname, created_by_email | 231 | Shared 'register a brand-new player row' core, factored out of |
| `register_and_join` | event | 275 | Combined 'register a friend' + 'quick-add during match setup' |
| `_caller_is_group_member` | group, claims | 330 | 'Any member' bar shared by session editing - a real member (any |
| `_session_view` | item | 341 | Shape a session row for the frontend. member_ids only, not resolved |
| `create_session` | event | 356 | POST /sessions - owner/admin of group_id only (or SuperAdmin), same |
| `list_group_sessions` | group_id | 387 | GET /group-sessions/{group_id} - deliberately unauthenticated, same |
| `_session_and_group` | session_id | 399 | Shared lookup for the three session-mutation routes below. Returns |
| `close_session` | session_id, event | 411 | PUT /sessions/{session_id}/close - the creator, or an owner/admin of |
| `add_session_member` | session_id, event | 437 | POST /sessions/{session_id}/members - any member (any role) of the |
| `remove_session_member` | session_id, player_id, event | 489 | DELETE /sessions/{session_id}/members/{player_id} - same 'any |
| `add_player_enforced` | group_id, event | 512 | Requires SuperAdmin, or already owner/admin of THIS group - reuses |
| `create_group_enforced` | event | 521 | Requires a valid Cognito login (any authenticated account - no |
| `_consume_extra_group_perk` | player_id | 555 | Spend one extra_group token if the player owns one. Mirrors the |
| `visible_players_for_caller` | event | 579 | For populating the Profile tab's player picker: SuperAdmin gets |
| `create_group` | event | 638 | — |
| `list_groups` |  | 662 | — |
| `get_group` | group_id | 681 | — |
| `update_group_defaults` | group_id, event | 715 | Save a group's default tournament creation settings (format, points, |
| `set_group_slots` | group_id, event | 736 | Owner/admin group settings via the Cognito-authorized PUT |
| `delete_group` | group_id, event | 838 | Deletes only the group record itself. Player records are never |
| `add_player` | group_id, event | 852 | — |
| `remove_player` | group_id, player_id, event | 894 | — |
| `_caller_claims` | event | 916 | Claims API Gateway's Cognito Authorizer attaches to the request. |
| `_is_super_admin` | claims | 924 | — |
| `set_role` | group_id, player_id, event | 929 | Set (or change) a member's role within this group. |
| `set_finance_role` | group_id, player_id, event | 972 | Set a member's per-group FINANCE role (none/view/write/delete) in this |
| `_response` | status_code, body_dict | 1012 | — |

#### `matches` — 2885 LOC
_NetWorth - matches Lambda (singles + doubles)_

**Module constants:** `K_FACTOR`, `XP_PLAYED`, `XP_WIN_BONUS`, `XP_TOURNAMENT_WIN`, `XP_MARGIN_PER_POINTS`, `XP_MARGIN_CAP`, `XP_LEVEL_COEFF`, `COINS_PER_LEVEL`, `_EVENTS_ROW_ID`, `_QUESTS_ROW_ID`, `_ACHIEVEMENTS_ROW_ID`, `_APP_SETTINGS_ID`, `_PRIVATE_ID_KEYS`, `QUEST_TYPES`, `ACHIEVEMENT_METRICS`, `_SEASON_ROW_PREFIX`, `COMEBACK_BONUS_THRESHOLD`, `COMEBACK_BONUS_PER_POINT`, `COMEBACK_BONUS_CAP`

| Function | Args | Line | What it does |
|---|---|---|---|
| `level_from_xp` | xp | 77 | Inverse of xp = 5*N^2, floored: the highest level fully paid for by |
| `xp_for_level` | level | 86 | Total XP needed to reach a given level - used for progress bars. |
| `xp_for_match` | stage, won, margin | 91 | Base XP a single player earns for one match (before any event |
| `_scan_all` | table | 115 | Full-table scan that follows LastEvaluatedKey - a bare .scan() returns |
| `_load_private_ids` |  | 130 | player_ids to exclude from rankings/leaderboards/top-N lists right now |
| `_entry_is_private` | x, private_ids | 165 | — |
| `_scrub_private` | obj, private_ids | 168 | Recursively drop leaderboard/distribution entries belonging to a private |
| `_rerank_visible` | leaders, hidden_ids | 190 | For a rank-ordered leaders list (already sorted best-first, each row |
| `_load_quests` |  | 232 | — |
| `_week_bounds_utc` | now | 237 | Monday 00:00 (inclusive) to next Monday (exclusive), as ISO date |
| `_evaluate_quest` | quest, player_id, week_matches, player_rating | 247 | Returns how many times the player has satisfied this quest's condition |
| `_season_config` |  | 295 | Season definitions + soft-reset k live in the shared app-settings row |
| `_resolve_season` | resolved, which | 314 | — |
| `_ensure_season_baseline` | season, k, items | 328 | Freeze, once, each player's lifetime rating as of the season start |
| `compute_season_leaderboard` | season, items, k, min_games | 359 | Derived climb board: everyone starts the season at a soft-reset baseline |
| `_season_board_leaders` | season, items, k | 403 | Leaders for a season: sealed (frozen) if it has ended, else live. |
| `_season_badges_for` | player_id, leaders | 421 | A player's standing + earned badges on one season board. |
| `compute_player_season_summary` | player_id, items | 444 | Per-season standing + badges for one player, across started seasons. |
| `_quest_period` | quest | 465 | (bounds, claim_prefix, label) for a quest by scope. Season-scoped quests |
| `list_quests` | event | 480 | Returns this week's quests with the caller's progress and claim state. |
| `save_quest` | event | 523 | — |
| `delete_quest` | event | 553 | — |
| `claim_quest` | event | 566 | Player claims a completed quest's reward. Verified server-side against |
| `_load_achievements` |  | 642 | — |
| `list_achievements` | event | 647 | Every achievement, with the caller's live progress/claim state. |
| `save_achievement` | event | 695 | — |
| `delete_achievement` | event | 729 | — |
| `claim_achievement` | event | 742 | Player claims a completed achievement's reward. Progress is |
| `unclaim_achievement` | event | 797 | SuperAdmin-only corrective action (added 2026-08-29): reverses one |
| `_load_events` |  | 846 | — |
| `event_multiplier_for_date` | date_str, events | 850 | The XP multiplier active on a given match date (default 1.0). Pass a |
| `display_name` | player_item, fallback | 871 | Single source of truth for name formatting: 'Nickname (Real Name)' |
| `compute_comeback_bonus` | momentum | 888 | Extra rating-point bonus for the winning side, on top of the |
| `_is_valid_completed_game` | score_a, score_b, target | 902 | BWF-style badminton scoring: first to `target` points wins, but must lead |
| `_caller_claims` | event | 919 | Claims API Gateway's Cognito Authorizer attaches to the request. |
| `_is_super_admin` | claims | 925 | — |
| `_caller_may_edit_match` | claims, match | 930 | Who may directly edit/delete a match (PUT/DELETE /matches/{id}): |
| `_can_view_profile` | claims, target_player_id | 962 | SuperAdmin sees everyone. Anyone can view their own profile. A |
| `_requires_linked_member` | claims | 982 | Signing up is not the same as being a member. Cognito self-signup is |
| `record_match_enforced` | event | 1005 | — |
| `profile_view_enforced` | event | 1015 | Entry point for the isolated /profile-secure/{proxy+} catch-all. |
| `handler` | event, context | 1042 | — |
| `list_events` | event | 1115 | Public read - the frontend shows an active-event banner to everyone. |
| `save_event` | event | 1123 | SuperAdmin creates or updates an event (upsert by event_id). |
| `delete_event` | event | 1153 | — |
| `recompute_now` | event | 1166 | SuperAdmin-only: replay every match to rebuild ratings, XP, levels |
| `reorder_matches` | event | 1176 | Reorders a set of matches by reassigning their timestamps. |
| `record_match` | event | 1244 | — |
| `update_match` | match_id, event | 1285 | Fix a mis-entered score on an already-recorded standalone match. |
| `delete_match` | match_id, event | 1345 | Permanently delete a mis-recorded match - e.g. the wrong player was |
| `recompute_all_ratings` |  | 1365 | Elo is path-dependent - each match's rating change depends on the |
| `compute_momentum_stats` | point_log, winner | 1489 | Longest scoring streak per team, and how big a deficit the winner overcame. |
| `compute_adaptive_k` | pairing_count | 1536 | Higher K for a fresh/novel doubles pairing (each match together is |
| `get_pairing_count` | team_ids, exclude_match_id | 1552 | How many prior doubles matches has this exact 2-player team played |
| `_play_and_log` | match_type, team_a_ids, team_b_ids, score_a,  | 1572 | — |
| `list_matches` | event | 1669 | — |
| `compute_partnerships` | player_id, items | 1829 | For a given player, tally win/loss record with each doubles partner |
| `get_group_member_ids` | group_id | 1871 | The set of player_ids belonging to a group, used to filter WHO shows |
| `compute_attendance` | items, group_id_filter | 1882 | Per-player attendance/consistency: total matches, distinct calendar |
| `compute_hall_of_fame` | items, group_id_filter, hidden_ids | 1950 | Highlight stats computed from full chronological match history: |
| `compute_achievements` | player_id, matches, tournaments | 2279 | Milestone/tiered achievement progress for one player: total matches |
| `compute_top_opponents` | player_id, matches, top_n | 2420 | This player's win/loss record against every opponent they've ever |
| `compute_overall_record` | player_id, matches | 2462 | This player's total win/loss record, split by singles and doubles. |
| `compute_head_to_head` | player_id, opponent_id, matches | 2491 | One player's win/loss record specifically as an OPPONENT of another |
| `compute_with_partner` | player_id, partner_id, matches | 2523 | One player's win/loss record when partnered WITH another player on |
| `compute_recent_form` | player_id, matches, limit | 2574 | A player's last N matches, in chronological order (oldest to |
| `compute_diversity` | items, group_id_filter | 2631 | For every player: how concentrated their doubles partnerships are. |
| `compute_progress_history_summary` | scope_label, period_name | 2676 | Reads the permanent, locked-in weekly/monthly/yearly winner history |
| `compute_progress_badges` | items, group_id_filter | 2753 | For each of the last week/month/year: who improved their rating the |
| `compute_partner_distribution` | player_id, items, top_n | 2829 | For the radar/spider chart: one player's doubles partners, sorted by |
| `_response` | status_code, body_dict | 2876 | — |

#### `tournaments` — 3735 LOC
_NetWorth - tournaments Lambda (singles or doubles)_

**Module constants:** `K_FACTOR`, `COMEBACK_BONUS_THRESHOLD`, `COMEBACK_BONUS_PER_POINT`, `COMEBACK_BONUS_CAP`, `XP_PLAYED`, `XP_WIN_BONUS`, `XP_MARGIN_PER_POINTS`, `XP_MARGIN_CAP`, `XP_LEVEL_COEFF`, `COINS_PER_LEVEL`, `_EVENTS_ROW_ID`, `CONFIRMATION_CODE`, `MANUAL_DRAFT_ACCEPTED_TARGETS`

| Function | Args | Line | What it does |
|---|---|---|---|
| `_scan_all` | table | 42 | Full-table scan that follows LastEvaluatedKey - a bare .scan() returns |
| `level_from_xp` | xp | 84 | Inverse of xp = 5*N^2, floored: the highest level fully paid for by |
| `xp_for_match` | stage, won, margin | 93 | Base XP a single player earns for one match (before any event |
| `_load_events` |  | 105 | — |
| `event_multiplier_for_date` | date_str, events | 110 | The XP multiplier active on a given match date (default 1.0). |
| `compute_comeback_bonus` | momentum | 129 | Extra rating-point bonus for the winning side, on top of the |
| `compute_momentum_stats` | point_log, winner | 141 | Longest scoring streak per team, and how big a deficit the winner overcame. |
| `_is_valid_completed_game` | score_a, score_b, target | 189 | Same BWF-style rule as the standalone matches Lambda: win by 2 at |
| `_is_valid_manual_draft_game_score` | score_a, score_b | 218 | — |
| `_caller_claims` | event | 222 | Same pattern as matches lambda - see that file's comment for |
| `_is_super_admin` | claims | 231 | Ported from groups/index.py - identical logic, kept in sync by hand |
| `_authorize_tournament_organizer` | item, claims | 238 | Shared check for every manual-draft organizer-only write (set |
| `_authorize_pool_auction_viewer` | item, claims | 259 | Who may see pool assignments / auction budgets & bids for a |
| `create_tournament_enforced` | event | 280 | — |
| `handler` | event, context | 286 | — |
| `seeded_order` | players | 334 | Sort by current rating, descending. New players just use their |
| `pair_for_balance` | ordered_players | 344 | Given a skill-ordered list, pair strongest with weakest (snake |
| `create_tournament` | event | 359 | — |
| `build_round_robin` | entities | 501 | — |
| `build_knockout_round` | entities | 518 | — |
| `_bye_match` | entity | 553 | — |
| `handle_draft_route` | event | 580 | — |
| `_draft_get_tournament` | tournament_id | 651 | Shared load+validate for every route below: must exist and must be |
| `_draft_everyone` | item | 662 | Every player currently accounted for in this tournament's pool |
| `create_manual_draft_tournament` | event, claims | 671 | Creates the shell for a manual-mode tournament: leaders, pools, the |
| `set_leaders` | tournament_id, event, claims | 792 | — |
| `add_draft_player` | tournament_id, event, claims | 818 | Lets the organizer drop a player into the unassigned tray while |
| `remove_draft_player` | tournament_id, event, claims | 848 | The inverse of add_draft_player: drops someone out of this |
| `set_pool_assignment` | tournament_id, event, claims | 886 | Full replace of one pool's member list - the simplest, idempotent |
| `lock_pools` | tournament_id, event, claims | 946 | — |
| `_draft_decided_ids` | draft | 1007 | Every player_id that's no longer available to auction: already won |
| `_authorize_leader` | item, claims | 1018 | Caller must be one of THIS tournament's registered leaders (matched |
| `start_auction` | tournament_id, event, claims | 1029 | — |
| `open_lot` | tournament_id, event, claims | 1081 | — |
| `submit_bid` | tournament_id, event, claims | 1118 | — |
| `_maybe_freeze_squads` | item, draft | 1175 | Shared by close_lot and organizer_assign: once every leader's every |
| `close_lot` | tournament_id, event, claims | 1206 | — |
| `organizer_assign` | tournament_id, event, claims | 1238 | Lets the organizer record a winning bid and award a player entirely |
| `skip_lot` | tournament_id, event, claims | 1298 | — |
| `get_draft_state` | tournament_id, event, claims | 1320 | The polling endpoint - a small payload (no bid_history/full item) |
| `get_draft_sensitive_detail` | tournament_id, event, claims | 1346 | The privileged counterpart to the public GET /tournaments/{id}, |
| `build_tie` | squad_a_id, squad_b_id, matches_per_tie | 1384 | — |
| `build_tie_round_robin` | squad_ids, matches_per_tie | 1407 | — |
| `_bye_tie` | squad_id | 1415 | Mirrors _bye_match: auto-decided the instant it's created, no |
| `build_knockout_tie_round` | squad_ids, matches_per_tie | 1425 | Generalizes build_knockout_round: same power-of-2/byes-needed |
| `_update_tie_progress` | tie | 1450 | Recomputes wins_a/wins_b/point_diff_a/point_diff_b from the tie's |
| `_score_tie_match` | item, tie, match_index, score_a, score_b, ove | 1532 | Submits one individual match's score within a tie. Raises ValueError |
| `_cancel_tie_match` | tie, match_index | 1584 | Marks one match as administratively cancelled - can't be played |
| `_forfeit_tie_match` | tie, match_index, forfeited_by | 1607 | Marks one match as forfeited by one side (owner report, 2026-08-23: |
| `_find_tie` | item, tie_id | 1635 | A tie_id is a UUID unique across the whole tournament, so it can be |
| `_tie_side_leader_id` | item, side_id | 1652 | Resolves a tie's squad_a/squad_b value to the leader id who's |
| `_authorize_tie_scorer` | item, tie, claims | 1663 | Organizer, or one of THIS tie's own two squad leaders - matches the |
| `compute_squad_standings` | item, squad_ids | 1678 | Squad-level standings: sorted by (ties_won desc, aggregate point |
| `compute_projected_knockout` | item | 1719 | Read-time-only preview of the knockout matchup, computed from the |
| `compute_group_stage_projection` | item | 1771 | Real-separate-groups sibling of compute_projected_knockout (owner |
| `compute_squad_standings_by_parent` | item | 1825 | Cross-squad group mode sibling of compute_squad_standings: rolls |
| `compute_player_tournament_scores` | item | 1862 | A tournament-scoped, non-Elo per-player score/leaderboard - a |
| `rename_squad` | tournament_id, event, claims | 1942 | Squads get an auto-generated name ("Team <leader>") the instant the |
| `set_squad_pairs` | tournament_id, event, claims | 1976 | Cross-squad group mode only (owner request, 2026-08-21): before the |
| `move_squad_player` | tournament_id, event, claims | 2037 | Organizer-only roster rebalancing between two squads, before the |
| `_rebuild_entity_after_substitution` | entity, old_player_id, new_player_id, new_pla | 2083 | Swaps old_player_id for new_player_id inside a squad-pair/rep/ |
| `substitute_squad_player` | tournament_id, event, claims | 2108 | Organizer-only real substitution for a manual-draft squad: swaps a |
| `_build_group_stage` | item | 2228 | Shared schedule-building logic, used both by generate_schedule (the |
| `_fill_cross_squad_match_players` | item, ties | 2283 | Cross-squad group mode (owner request, 2026-08-21): a tie's two |
| `_build_cross_squad_group_stage` | item | 2312 | Cross-squad group mode (owner request, 2026-08-21): instead of |
| `generate_schedule` | tournament_id, event, claims | 2390 | — |
| `regenerate_schedule` | tournament_id, event, claims | 2425 | Organizer repair action: re-run schedule generation for a tournament |
| `pick_tie_player` | tournament_id, event, claims | 2486 | A leader nominates which of their own squad's members plays a given |
| `_generate_knockout_from_group_stage` | item | 2588 | — |
| `_inject_group_tiebreakers_if_needed` | item | 2607 | Real-separate-groups sibling of the legacy groups_then_knockout |
| `_advance_squads_to_knockout_from_groups` | item | 2651 | Real-separate-groups sibling of the legacy groups_then_knockout |
| `record_group_tie_score` | tournament_id, event, claims | 2703 | — |
| `_after_group_tie_resolved` | item | 2738 | Shared by every route that can make a group tie `decided` (score, |
| `cancel_group_tie_match` | tournament_id, event, claims | 2752 | Organizer-only: administratively cancels one group match that can |
| `forfeit_group_tie_match` | tournament_id, event, claims | 2788 | Organizer-only sibling of cancel_group_tie_match: one side didn't |
| `_advance_knockout_ties_if_round_complete` | item | 2823 | Mirrors record_knockout_score's round-advancement + third-place- |
| `record_knockout_tie_score` | tournament_id, event, claims | 2869 | — |
| `cancel_knockout_tie_match` | tournament_id, event, claims | 2908 | Organizer-only knockout/third-place sibling of cancel_group_tie_match |
| `forfeit_knockout_tie_match` | tournament_id, event, claims | 2944 | Organizer-only knockout/third-place sibling of forfeit_group_tie_match |
| `list_tournaments` | event | 2983 | — |
| `_redact_pool_auction_detail` | item | 3007 | GET /tournaments/{id} is unauthenticated - literally anyone browsing |
| `_hide_pool_auction_from_non_organizer` | item, claims | 3030 | pick_tie_player/record_group_tie_score/record_knockout_tie_score are |
| `get_tournament` | tournament_id | 3044 | — |
| `recompute_all_ratings` |  | 3085 | Elo is path-dependent - each match's rating change depends on the |
| `delete_tournament` | tournament_id, event | 3163 | Deletes this tournament AND every match record tagged with its |
| `compute_standings` | fixtures, entities | 3197 | — |
| `compute_all_standings` | item | 3229 | — |
| `_submit_game` | fixture, score_a, score_b, best_of, target, o | 3235 | Append one game's score to a fixture/match. Returns True if the match is now decided. |
| `record_group_score` | tournament_id, event | 3264 | — |
| `inject_tiebreakers_if_needed` | item | 3318 | Checks each subgroup for a genuine tie (same wins AND point_diff) at |
| `advance_to_knockout` | item | 3369 | — |
| `record_knockout_score` | tournament_id, event | 3394 | — |
| `compute_adaptive_k` | pairing_count | 3505 | Higher K for a fresh/novel doubles pairing (each match together is |
| `get_pairing_count` | team_ids | 3519 | How many prior doubles matches has this exact 2-player team played |
| `update_elo_and_log` | match_type, entity_a, entity_b, score_a, scor | 3537 | — |
| `substitute_player` | tournament_id, event | 3633 | Swap a player out of a team for all of that team's FUTURE (unplayed) |
| `_response` | status_code, body_dict | 3726 | — |

#### `finance` — 1660 LOC
_NetWorth - finance Lambda_

**Module constants:** `GROUPS_TABLE`, `DEFAULT_GROUP_NAME`, `GROUP_SLOT`, `VIEW_KEY`, `CONFIRMATION_CODE`, `MONTHS`, `FINANCE_LEVELS`, `ALLOWED_FIELDS`, `NUMERIC_FIELDS`, `REQUIRED_FIELDS`, `DEFAULT_CLUB_UTC_OFFSET_MINUTES`, `AVG_GAMES_PER_SESSION`, `SESSION_RATE`, `ACTIVE_DAYS_THRESHOLD`, `SLOT_GRACE_MINUTES`, `ASSUMED_MINUTES_PER_GAME`, `DEFAULT_ASSUMED_MINUTES_PER_GAME`, `DENSITY_FLAG_RATIO`, `_SLOT_LABEL_RE`

| Function | Args | Line | What it does |
|---|---|---|---|
| `_scan_all` | table | 87 | Full-table scan that follows LastEvaluatedKey - a bare .scan() returns |
| `_caller_claims` | event | 115 | Claims API Gateway's Cognito Authorizer attaches to the request. |
| `_is_super_admin` | claims | 123 | — |
| `_finance_role` | claims | 137 | — |
| `_finance_level` | claims | 153 | — |
| `_has_finance_access` | claims | 157 | View or better - the gate for reading finance at all. |
| `_default_group_id` |  | 162 | The group_id of the 'Club (default)' group that the pre-migration |
| `_group_for_request` | params, body | 178 | The group_id this finance op targets. Falls back to the default group |
| `_group_finance_level` | claims, group_id | 185 | A caller's finance level (0-3) FOR A SPECIFIC GROUP. |
| `_slot_key` | slot | 211 | Normalize a record's slot for bucketing/comparison: a missing/blank |
| `_member_assigned_slots` | pid, group | 218 | The set of slots (raw, already-normalized strings) a player is |
| `_view_scope_slots` | claims, group_id, level | 227 | Stage 4c: a plain 'view'-level grant only sees their own assigned |
| `_has_any_group_finance` | claims | 253 | True if the caller has finance access in ANY group (owner/admin, or a |
| `_effective_finance_role` | claims, group_id | 268 | The role name to REPORT to the frontend for button visibility: the |
| `finance_key_for_caller` | event | 293 | Hands the shared view key to any caller with finance access - global |
| `set_finance_access` | event | 310 | SuperAdmin sets a player's finance role directly. |
| `handler` | event, context | 334 | — |
| `_scan_type` | record_type, group_id | 452 | — |
| `_num` | v, default | 463 | — |
| `_clean` | record_type, data | 487 | — |
| `_resolve_name` | pid_cache, player_id | 499 | — |
| `_prev_period` | month, year | 510 | — |
| `_next_period` | month, year | 515 | — |
| `_member_relief` | settlement, memberships, ident, month, year,  | 520 | Relief a member gets in (month, year): the previous month's residual. |
| `list_records` | record_type, params, group_id, scope_slots | 543 | — |
| `create_records` | record_type, body, group_id | 605 | — |
| `update_record` | record_type, record_id, body, group_id | 627 | — |
| `delete_record_enforced` | record_type, record_id, event | 690 | Triple-gated: SuperAdmin identity + FINANCE_VIEW_KEY + the existing |
| `delete_record` | record_type, record_id, body, group_id | 713 | — |
| `get_settings` |  | 730 | — |
| `put_settings` | body | 758 | — |
| `public_upi` |  | 787 | The pay card is shown to guests (they pay walk-in fees), so the UPI |
| `my_settlement` | claims, group_id | 795 | A single member's own dues in a group: for every (month, slot) where |
| `public_walkins` |  | 936 | — |
| `_settlement_rows` | group_id | 956 | Per (month, year, slot): the exact math from the Calculations sheet. |
| `summary` | group_id, scope_slots | 1169 | — |
| `_parse_slot_window` | label | 1210 | Best-effort parse of a free-form slot label ('7AM-8AM', '19:00-20:00', |
| `_local_minutes_of_day` | iso_ts, offset_minutes | 1246 | Convert a stored ISO-8601 UTC match timestamp to local minute-of-day |
| `_minute_in_window` | minute, window, grace_minutes | 1266 | Whether `minute` (local minute-of-day) falls inside `window` (start, |
| `_timing_checks` | matches, group, offset_minutes, target_ym | 1279 | Best-effort, non-authoritative diagnostics only (see module note |
| `insights` | group_id | 1359 | Per-member monthly economics, ghosts, and walk-in conversion. |
| `_response` | status_code, body_dict | 1651 | — |

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
### Frontend (`frontend/js/app.js` — 12645 LOC, flat global script, ~499 functions)

_Loaded by `index.html` after an inline `<script>` defines the globals `API_BASE_URL`, `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, `UPI_ID`, `FINANCE_VIEW_KEY` placeholders. Functions live in global scope (not an IIFE); most are wired to `onclick=` in the HTML._


**Auth/token core (top of file)**  (from L0)
- `getAuthHeaders()` — L9
- `isLoggedIn()` — L12

**Token freshness & authedFetch**  (from L14)
- `tokenSecondsRemaining()` — L28
- `ensureFreshToken(force = false)` — L33
- `authedFetch(url, options = {})` — L67
- `send()` — L68
- `describeApiError(res, data)` — L136
- `isSuperAdmin()` — L143
- `myPlayerId()` — L148
- `hasLinkedPlayer()` — L162
- `myRoleInGroup(group)` — L172
- `canManageGroup(group)` — L178
- `ownsAnyGroup()` — L186
- `canReviewRequests()` — L189
- `updateReviewTabScope()` — L196

**Nickname/name display toggle**  (from L205)
- `formatPlayerLabel(name, nickname)` — L219
- `toggleDisplayMode()` — L224
- `_nwModal({ message, input, defaultValue, okText, )` — L281
- `cleanup(val)` — L300
- `onKey(e)` — L322
- `nwConfirm(message, opts = {})` — L332
- `nwAlert(message, opts = {})` — L335
- `nwPrompt(message, defaultValue = '', opts = {})` — L338

**Data-load helpers**  (from L347)
- `populateSelect(selectEl, items, valueKey, labelKey, pla)` — L349
- `bumpMatchesRev()` — L377
- `isTabActive(tab)` — L378
- `ensureFresh(key, loader)` — L383
- `ensureOnce(key, loader)` — L389
- `loadStatsBundle()` — L395
- `makeCardsCollapsible(containerId)` — L425
- `setOpen(open)` — L440
- `makeStatsCollapsible()` — L445
- `makeFinanceCollapsible()` — L452
- `ensureProfileFresh()` — L457
- `loadActiveTabData()` — L466
- `myPlayerRecord()` — L477
- `iAmPrivate()` — L478
- `privateHiddenIds()` — L486
- `rankingHiddenIds()` — L499
- `renderPrivacyControl()` — L512
- `toggleMyPrivacy()` — L546
- `setPrivacyMode(value)` — L569
- `setPrivacyCooldown(value)` — L583
- `statsFetch(query)` — L597
- `populateAdminPrivacySelect()` — L603
- `adminSetPrivacy(makePrivate)` — L613
- `populateAdminRenameSelect()` — L636
- `adminRenamePlayer()` — L646
- `populateMergePlayerSelects()` — L676
- `mergePlayersAdmin()` — L691
- `seasonMedallion(rank, size)` — L737
- `seasonBadgeSvg(kind, rank, size)` — L751
- `loadPlayerSeasons(playerId)` — L767
- `loadSeasonsMeta()` — L794
- `loadSeasonBoard(seasonId)` — L816
- `renderSeasonAdmin()` — L847
- `saveSeasons(list, statusElId, okMsg)` — L856
- `addSeason()` — L867
- `deleteSeason(id)` — L881
- `setSeasonsEnabled(value)` — L885
- `setSeasonK(value)` — L896
- `loadPlayers()` — L904
- `loadGroups()` — L928
- `loadGroupMembers(groupId)` — L993
- `opt(v, label)` — L1045
- `opt(v, label)` — L1069
- `nameOf(pid)` — L1082
- `applyGroupDefaultsToForm(prefix, settings)` — L1117
- `setIfPresent(suffix, value)` — L1119
- `renderAddPlayersChecklist()` — L1130
- `removePlayerFromGroup(groupId, playerId)` — L1143
- `populateTeamSelects()` — L1178
- `refreshTeamSelectOptions()` — L1217
- `syncTeamSelectValues()` — L1235
- `handleTeamSelectChange(changedId)` — L1244
- `applyMatchTypeVisibility()` — L1258

**Segmented controls (match type / points-to-win)**  (from L1269)
- `nwSyncSegFromSelect(selectId, segId)` — L1275
- `updateMatchGroupCache()` — L1295
- `randomizeTeams(showAlertOnFail)` — L1316

**Live point-by-point scoring**  (from L1349)
- `isGameOver(a, b, target)` — L1354
- `updateLiveScoreDisplay()` — L1362

**Split-screen live scoring**  (from L1430)
- `getTeamDisplayName(selectId)` — L1432
- `getSplitTeamNames()` — L1438
- `updateSplitScreenScores(a, b, over)` — L1453
- `openSplitScreenGeneric(config)` — L1462
- `closeSplitScreen()` — L1471
- `openSplitScreen()` — L1477
- `openTournamentSplitScreen(matchKey, target, nameA, nameB, finishFn)` — L1502

**Delete / edit player**  (from L1661)
- `prefillEditForm()` — L1702

**Matches (record/list/game-log)**  (from L1864)
- `myGroups()` — L1894
- `visibleGroupsForFilter()` — L1905
- `defaultMatchGroup()` — L1913
- `defaultRegisterGroup()` — L1944

**Voice match entry**  (from L1953)
- `applyVoiceVisibility()` — L1962
- `nwPhon(s)` — L1969
- `nwLev(a, b)` — L1980
- `nwScorePlayer(token, p)` — L1987
- `nwMatchPlayerToken(tokenRaw)` — L2006
- `nwWordsToNums(t)` — L2021
- `nwParseMatchTranscript(raw)` — L2029
- `nwApplyParsedToForm(p)` — L2061
- `set(id, entry)` — L2065
- `nwVoicePreviewHtml(p)` — L2075
- `nwVoiceMatchInit()` — L2087
- `stopListening()` — L2116

**Team pairing preview**  (from L2201)
- `nwSeeded(p)` — L2205
- `nwShuffle(a)` — L2206
- `nwPairingRefreshList()` — L2208
- `nwPairingUpdateCount()` — L2219
- `nwPairingRender()` — L2224
- `nwPairingInit()` — L2256

**Quick record: tap mode + Sessions + voice stack + shared queue**  (from L2297)
- `postMatchPayload(payload)` — L2313
- `nwSetRecordMode(mode)` — L2323
- `nwTapRosterPool()` — L2367
- `nwLoadGroupSessions()` — L2375
- `nwRenderSessionBar()` — L2395
- `nwRenderOnCourtActions()` — L2451
- `nwOpenSessionAddPanel()` — L2537
- `nwUpdateSessionAddCount()` — L2553
- `nwSessionAddMember(body)` — L2562
- `nwSessionRemoveMember(playerId)` — L2611
- `nwTapSlotsPerTeam()` — L2633
- `nwTapRefreshAvatarGrid()` — L2637
- `nwTapToggleAvatar(playerId)` — L2731
- `nwTapPlayerName(id)` — L2740
- `nwTapRenderTeams()` — L2745
- `nwTapPointsToWin()` — L2781
- `nwTapFinalScore()` — L2782
- `nwTapRenderQuickScore()` — L2786
- `nwTapRenderManual()` — L2818
- `nwStackVoiceNote(said)` — L2948
- `nwSaveQueue()` — L3000
- `nwRenderQueue()` — L3004
- `groupName(gid)` — L3010
- `nwQueueEditItem(id)` — L3038
- `nwQueueRemoveItem(id)` — L3077
- `nameFor(pid)` — L3181
- `showMatchOutcome(ok, message)` — L3251

**Unsaved-match safety net**  (from L3263)
- `savePendingMatch(payload, meta)` — L3270
- `loadPendingMatch()` — L3274
- `clearPendingMatch()` — L3277
- `handleSessionExpired()` — L3284
- `ensureRestoreHost()` — L3307
- `offerPendingMatchRestore()` — L3316

**Game log & CSV export**  (from L3349)
- `loadGameLog()` — L3351
- `gameLogGoto(p)` — L3401
- `renderGameLog()` — L3403
- `matchPermissions(m)` — L3459
- `matchGroupLabel(m)` — L3481
- `canActOnMatchDirectly(m)` — L3499
- `requestMatchChange(matchId, type, label, groupId, extra)` — L3506
- `editMatch(matchId, groupId)` — L3530
- `opts(sel)` — L3539
- `pickers(team, prefix)` — L3541
- `close()` — L3562
- `editMatchScore(matchId, currentScoreA, currentScoreB, e)` — L3587
- `deleteMatch(matchId, encLabel, groupId)` — L3623
- `downloadCSV(filename, rows)` — L3655
- `loadRankings()` — L3689
- `gp(p)` — L3715
- `fetchRatingHistory(playerId)` — L3760
- `loadVisiblePlayers(opts = {})` — L3771

**Profile card customization**  (from L3846)
- `resolveBannerId(id)` — L3909
- `bgCss(id, url)` — L3983
- `updatePageBackground()` — L3988
- `applyPageBackground(player)` — L3999
- `renderProfileCardBanner(player)` — L4006
- `toggleHeaderMenu()` — L4050
- `openSettingsModal()` — L4064
- `loadFinanceAccessList()` — L4078
- `opt(v, label)` — L4096
- `setGroupFinanceRole(groupId, playerId, role)` — L4111
- `setGroupMemberRole(groupId, playerId, role, wasRole, isSelf)` — L4127
- `setFinanceRole(playerId, role)` — L4143
- `closeSettingsModal()` — L4154
- `renderSettingsPickers(player)` — L4158
- `swatch(field, id, css, selected)` — L4167
- `submitClaimRequest()` — L4196
- `checkApprovalStatus()` — L4221
- `recomputeNow()` — L4234
- `loadAppSettings()` — L4249
- `setXpPublic(value)` — L4281
- `setVoiceEnabled(value)` — L4296
- `setInstantCreate(value)` — L4311

**Quests**  (from L4323)
- `loadQuests()` — L4324
- `_renderQuestRow(q)` — L4333
- `_hdr(t)` — L4357
- `claimQuest(questId)` — L4365
- `loadQuestsAdmin()` — L4381
- `saveQuest()` — L4402
- `deleteQuest(questId)` — L4424

**Achievements**  (from L4435)
- `badgeSvg(tier, glyph)` — L4451
- `loadMyAchievements()` — L4475
- `claimAchievement(achievementId)` — L4510
- `loadAchievementsAdmin()` — L4537
- `editAchievement(achievementId)` — L4595
- `cancelAchievementEdit()` — L4612
- `saveAchievement()` — L4627
- `deleteAchievement(achievementId)` — L4654
- `revokeAchievementClaim(achievementId)` — L4669
- `seedStarterAchievements()` — L4713

**Store & events admin**  (from L4754)
- `loadStore()` — L4755
- `catOf(i)` — L4780
- `cardHtml(i)` — L4785
- `buyStoreItem(itemId)` — L4817
- `onStoreImagePick(input)` — L4831
- `loadStoreAdmin()` — L4839
- `onStoreTypeChange()` — L4867
- `onStoreEffectChange()` — L4879
- `uploadStoreImage(file)` — L4895
- `saveStoreItem()` — L4912
- `deleteStoreItem(itemId)` — L4962
- `loadEventsAdmin()` — L4973
- `editEvent(e)` — L4995
- `saveEvent()` — L5003
- `deleteEvent(eventId)` — L5026
- `refreshEventBanner()` — L5038
- `loadClaimAudit()` — L5055
- `relinkAccount(usernameEnc, presetPlayerId)` — L5122
- `unlinkAccount(usernameEnc)` — L5130
- `unlinkAndStrip(usernameEnc, playerId)` — L5135
- `_claimAuditAction(bodyObj)` — L5140
- `loadUnconfirmedUsers()` — L5151
- `deleteUnconfirmedUser(username, email)` — L5178
- `loadClaimRequests()` — L5191
- `decideClaimRequest(requestId, action, requestType)` — L5246
- `escapeHtml(s)` — L5284

**Image uploads**  (from L5289)
- `resizeImage(file, kind)` — L5301
- `isAnimatedImage(file)` — L5336
- `uploadCardImage(kind, fileInput)` — L5348
- `imageSrc(key)` — L5403
- `loadStoreCatalogOnce()` — L5409
- `renderStoreCosmeticStrip(kind, player)` — L5419
- `renderUploadStrip(kind, player)` — L5443
- `vsPlayerVisual(pid, snapshot)` — L5475
- `vsAvatarHtml(v, isWinner)` — L5491
- `teamBanner(side)` — L5508
- `gameScore(game, side)` — L5519
- `renderVsCard(idsA, idsB, opts = {})` — L5525
- `won(side)` — L5530
- `vsSideIds(side)` — L5555
- `setMyCardField(field, value)` — L5565

**Profile bundle / cards / charts**  (from L5633)
- `loadProfileBundle(playerId)` — L5633
- `renderTieredCard(icon, name, unit, tiers, currentValue)` — L5748
- `renderBinaryCard(icon, name, desc, achieved, detail)` — L5776
- `resetRatingZoom()` — L5848
- `loadProfileRatingChart(playerId)` — L5854
- `loadProfilePartnershipsAndRadar(playerId)` — L5940
- `loadProfileHeadToHead(playerId)` — L5989
- `loadProfileWithPartner(playerId)` — L6013
- `partnerGamesGoto(p)` — L6045
- `renderPartnerGames()` — L6047
- `skeletonHTML(lines = 3)` — L6080
- `showProfileSkeletons()` — L6087
- `renderXpPanel(player)` — L6100
- `xpForLevel(n)` — L6108
- `updateHeaderCoins()` — L6134
- `loadProfile()` — L6146
- `refreshProfile()` — L6176
- `refreshProfileIfShowing(affectedPlayerIds)` — L6193
- `renderPartnerRadar(data, highlightTournament, svgId = 'rada)` — L6214
- `loadHistory()` — L6270
- `renderHistory(data)` — L6288
- `loadBadges()` — L6349
- `renderBadges(data)` — L6367
- `loadDiversity()` — L6400
- `renderDiversity(data)` — L6418
- `playerLabelById(playerId, fallbackName)` — L6439
- `playerLabelsById(playerIds, fallbackNames)` — L6443
- `loadHallOfFame()` — L6449
- `renderHallOfFame(data)` — L6471
- `loadAttendance()` — L6555
- `renderAttendance(data)` — L6574

**UPI payment card**  (from L6589)
- `refreshUpiCard()` — L6593
- `renderUpiCard()` — L6605
- `imageServiceFallback()` — L6627

**Finance tab (view-key + role gated)**  (from L6647)
- `xpVisible()` — L6655
- `applyFinanceRoleVisibility()` — L6661
- `refreshFinanceRoleForGroup()` — L6692
- `finQS(extra)` — L6705
- `financeBaseUrl()` — L6716
- `finPost(path, method, bodyObj)` — L6720
- `populateFinanceSlots(group)` — L6744
- `_rememberedFinance(key)` — L6767
- `_rememberFinance(key, val)` — L6771
- `restoreFinanceMonth()` — L6777
- `populateFinanceGroups()` — L6786
- `reloadFinanceForGroup()` — L6814
- `tryAutoFinanceUnlock()` — L6819
- `myFinanceGroups()` — L6849
- `populateMyDuesGroups()` — L6854
- `loadMyDues(groupId)` — L6872
- `manageGroupSlots(groupId)` — L6922
- `assignSlotMembers(groupId, slotEnc)` — L6940
- `transferGroupOwnership(groupId)` — L6968
- `setGroupPayee(groupId)` — L6987
- `requestFinanceAccess()` — L7010
- `financeUnlock()` — L7027
- `updateFinanceScopeNote(scopedTo)` — L7082
- `loadFinanceSummary()` — L7092
- `loadFinanceExpenses()` — L7132
- `resetExpenseEdit()` — L7170
- `addFinanceExpense()` — L7177
- `loadFinanceMembers()` — L7196
- `markMembersDirty()` — L7300
- `recalcMembers()` — L7307
- `renderBulkRosterList()` — L7319
- `bulkAddFromRoster()` — L7333
- `copyPreviousMonthMembers()` — L7348
- `addFinanceMember()` — L7384
- `resetWalkinEdit()` — L7410
- `suggestWalkinSessions()` — L7429
- `loadFinanceWalkins()` — L7461
- `addFinanceWalkin()` — L7513
- `loadFinanceInsights()` — L7546
- `copyDuesForWhatsApp()` — L7560
- `pad(s, w)` — L7577
- `padL(s, w)` — L7578
- `line(n, o, r, p)` — L7579
- `done()` — L7589
- `fallbackCopy(text, cb)` — L7595
- `copyInsightsTableAsImage()` — L7624
- `renderInsights()` — L7730
- `saveFinanceSettings()` — L7865
- `loadPublicWalkins()` — L7911

**Match review & reorder (SuperAdmin)**  (from L7999)
- `loadReviewDay()` — L8004
- `reviewOrderChanged()` — L8048
- `renderReviewList()` — L8054
- `applyReviewOrder()` — L8115
- `updateAuthUI()` — L8143
- `hiddenNow(id, btn)` — L8171
- `refreshMySession(statusElId)` — L8256
- `setStatus(msg)` — L8257
- `openAchievementsModal()` — L8285
- `closeAchievementsModal()` — L8294
- `openAuthModal()` — L8295
- `closeAuthModal()` — L8296
- `showAuthView(view)` — L8297
- `setAuthSession(session, user, opts = {})` — L8305
- `closeCompleteProfileModal()` — L8325
- `openCompleteProfileModal()` — L8326
- `showCompleteProfileMode(mode, preselectPlayerId)` — L8341
- `populateClaimPicker(preselectPlayerId)` — L8349
- `submitClaimProfile()` — L8373
- `closeCompleteProfileModal()` — L8413
- `sanitizeNickname(raw)` — L8419
- `editDistance(a, b)` — L8424
- `checkForExistingPlayer(name, typedNickname, statusEl)` — L8446
- `submitCompleteProfile()` — L8501
- `finishRequestAndSignOut(message)` — L8577
- `doLogin()` — L8583
- `doNewPassword()` — L8636
- `doSignup()` — L8647
- `doConfirmSignup()` — L8664
- `doResendConfirmCode()` — L8695
- `doForgotPassword()` — L8706
- `doConfirmForgotPassword()` — L8721
- `doLogout()` — L8733
- `restoreSession()` — L8777
- `restoreTabFromHash()` — L8821

**Tournaments**  (from L8855)
- `addManualTeamRow()` — L8896
- `collectManualTeams()` — L8932
- `loadTournamentGroupOptions()` — L8945
- `loadTournamentParticipantsChecklist()` — L8954
- `updateParticipantsCount()` — L8984
- `collectTournamentParticipants()` — L8996
- `loadTournamentsList()` — L9000
- `submitTournamentCreation(payload)` — L9007
- `submitManualDraftCreation(group_id, name)` — L9033
- `draftPlayerName(pid)` — L9081
- `draftEveryone(t)` — L9086
- `renderManualDraftTournament(t)` — L9092
- `fetchTournamentDetail(tournamentId)` — L9191
- `fetchAndRenderTournamentDetail(tournamentId)` — L9207
- `stopSchedulePolling()` — L9238
- `startSchedulePolling(tournamentId)` — L9243
- `isSchedulePollingActiveFor(tournamentId)` — L9253
- `schedulePollTick(tournamentId)` — L9255
- `renderDraftLeaderPicker(t)` — L9286
- `saveDraftLeaders(tournamentId)` — L9306
- `renderDraftPoolBoard(t)` — L9316
- `chip(pid)` — L9321
- `draftChipTapped(pid, ev)` — L9368
- `draftPoolColumnTapped(tournamentId, poolName)` — L9375
- `draftChipDragStart(ev, pid)` — L9382
- `draftPoolDragOver(ev)` — L9387
- `draftPoolDrop(ev, tournamentId, poolName)` — L9392
- `moveDraftPlayerToPool(tournamentId, poolName, playerId)` — L9401
- `putDraftPool(tournamentId, poolName, playerIds)` — L9421
- `addNewDraftPlayer(tournamentId, groupId)` — L9431
- `removeDraftPlayer(tournamentId, playerId)` — L9457
- `lockDraftPools(tournamentId)` — L9470
- `stopDraftPolling()` — L9500
- `startDraftPolling(tournamentId)` — L9505
- `isDraftPollingActiveFor(tournamentId)` — L9518
- `pollDraftStateTick(tournamentId)` — L9520
- `draftDecidedIds(draft)` — L9531
- `renderDraftStartAuctionPanel(t)` — L9540
- `startDraftAuction(tournamentId)` — L9549
- `renderDraftAuctionRoom(t)` — L9559
- `draftAssignEligibleLeaders(t, pool)` — L9574
- `draftAssignLeaderOptionsHtml(t, pool)` — L9584
- `updateDraftAssignLeaderOptions()` — L9590
- `renderDraftOrganizerAssignPanel(t)` — L9599
- `organizerAssignPlayer(tournamentId)` — L9624
- `renderDraftLiveStatusHtml(tournamentId, draftLike)` — L9645
- `updateDraftLiveStatus(tournamentId, draftLike)` — L9680
- `renderDraftQueuePicker(t)` — L9693
- `openDraftLot(tournamentId, playerId)` — L9720
- `closeDraftLot(tournamentId)` — L9728
- `skipDraftLot(tournamentId)` — L9738
- `renderDraftBidBox()` — L9748
- `draftBidBump(delta)` — L9761
- `submitDraftBid(tournamentId)` — L9768
- `renderDraftSquadsReview(t)` — L9797
- `renderSetSquadPairsPanel(t)` — L9817
- `generateCrossSquadGroups(tournamentId, status)` — L9880
- `saveSquadPairs(tournamentId, squadId, numGroups, slotsP)` — L9897
- `generateDraftSchedule(tournamentId)` — L9918
- `renderSquadRosterEditPanel(t, allowMove)` — L9939
- `renameSquadPrompt(tournamentId, squadId)` — L10005
- `moveSquadPlayer(tournamentId)` — L10022
- `toggleSquadSubNewPlayerFields(useNew)` — L10036
- `substituteSquadPlayer(tournamentId)` — L10053
- `draftSquadName(t, squadId)` — L10105
- `toggleDraftGroupOpen(name, detailsEl)` — L10127
- `toggleDraftSquadSection(key, detailsEl)` — L10140
- `renderDraftScheduleView(t)` — L10144
- `renderProjectedKnockout(t)` — L10241
- `renderRegenerateScheduleGroupPanel(t)` — L10263
- `regenerateDraftSchedule(tournamentId)` — L10287
- `renderSquadStandingsTable(standings, projection)` — L10314
- `computeLeaderboardRows(stats, t)` — L10353
- `tallyPair(side)` — L10390
- `squadSideField(tie, sid)` — L10452
- `decidingPairKey(tie, sid)` — L10453
- `podiumRank(row)` — L10511
- `renderPlayerTournamentStatsTable(stats, t)` — L10527
- `rowHtml(row)` — L10535
- `renderTieSection(title, ties, t, stageKind)` — L10580
- `renderTieCard(tie, t, stageKind)` — L10587
- `renderTieMatchRow(tie, m, idx, t, stageKind, iLeadA, iLead)` — L10619
- `draftTieMatchAdminControlsHtml(tournamentId, tieId, idx, stageKind, sid)` — L10805
- `cancelDraftTieMatch(tournamentId, tieId, matchIndex, stageKi)` — L10813
- `forfeitDraftTieMatch(tournamentId, tieId, matchIndex, stageKi)` — L10824
- `draftPlayerPickerHtml(tournamentId, tieId, matchIndex, members)` — L10835
- `opts(selected)` — L10841
- `pickTiePlayer(tournamentId, tieId, matchIndex, playerI)` — L10861
- `pickTiePlayerPair(tournamentId, tieId, matchIndex, squadId)` — L10873
- `submitDraftTieScore(tournamentId, tieId, matchIndex, stageKi)` — L10889
- `submitDraftTieScoreDirect(tournamentId, tieId, matchIndex, stageKi)` — L10906
- `collectAllEntities(t)` — L11072
- `getAllTeamEntities(t)` — L11088
- `renderTeamCompositionBars(t, containerId)` — L11106
- `populateSubstitutionSection(t)` — L11141
- `updateSubOldPlayerOptions()` — L11152
- `formatGames(games)` — L11241
- `applyTournamentViewMode()` — L11248
- `matchTotals(match)` — L11264
- `truncateBracketName(name, maxChars = 22)` — L11272
- `renderBracketView(t)` — L11277
- `renderDraftBracketGroupsPanel(t)` — L11405
- `renderDraftBracketView(t)` — L11441
- `renderTournament(t)` — L11572
- `generateTournamentRecap(t)` — L11747
- `downloadTournamentImage()` — L11779
- `loadImg(src)` — L11806
- `sideVisuals(side)` — L11816
- `drawCard(x, y, w, match, isFinal)` — L11823
- `drawAvatars(ctx, x, y, side, isWinner)` — L11869
- `paintTeam(ctx, x, y, w, h, side, fallback)` — L11888
- `roundRect(ctx, x, y, w, h, r)` — L11916
- `downloadDraftShareImage()` — L11932
- `loadImg(src)` — L11939
- `sideAvatars(squadId)` — L11988
- `drawSide(side, sx, sy, isWinner)` — L12069
- `downloadDraftLeaderboardImage()` — L12150
- `loadImg(src)` — L12159
- `presetKeyFor(bannerCss)` — L12168
- `copyTournamentRecap()` — L12278
- `item_has_third_place(t)` — L12289
- `submitGroupScore(tournamentId, subgroup, fixtureId)` — L12293
- `submitGroupScoreDirect(tournamentId, subgroup, fixtureId, score)` — L12299
- `submitKnockoutScore(tournamentId, roundIndex, matchIndex)` — L12336
- `submitKnockoutScoreDirect(tournamentId, roundIndex, matchIndex, sc)` — L12342
- `submitThirdPlaceScore(tournamentId)` — L12369
- `submitThirdPlaceScoreDirect(tournamentId, score_a, score_b, override)` — L12375

**Live scoring inside tournaments**  (from L12402)
- `getTournamentLiveLog(matchKey)` — L12406
- `tournamentLivePoint(matchKey, side, target)` — L12411
- `tournamentUndoPoint(matchKey, target)` — L12420
- `updateTournamentLiveDisplay(matchKey, target)` — L12426
- `finishGroupLiveGame(matchKey, tournamentId, subgroup, fixtur)` — L12444
- `finishKnockoutLiveGame(matchKey, tournamentId, roundIndex, matc)` — L12458
- `finishThirdPlaceLiveGame(matchKey, tournamentId)` — L12467
- `finishDraftTieLiveGame(matchKey, tournamentId, tieId, matchInde)` — L12485
- `renderLiveScoreControls(matchKey, target, finishCallExpr, nameA,)` — L12494
- `activateTab(tabName)` — L12518
- `jumpToRecordMatch()` — L12615
- `applyTheme(theme)` — L12621
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
