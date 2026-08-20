# NetWorth — Known Issues & Loopholes (LLM Reference)

> Things any assistant should know are *already broken, risky, or fragile* before touching them.
> Each item: what it is, why it matters, and the safe move. Severity is a rough triage, not gospel.
> Nothing here is an emergency as of the snapshot; several are deliberate trade-offs. Verify against
> live AWS before acting — this reflects the 2026-07-29 code snapshot only.

---

## Security / auth

### 1. Dual finance surface: `/finance/{proxy+}` is a legacy **open** (no-authorizer) route  · sev: medium
The finance Lambda is reachable two ways: `/finance-secure/{proxy+}` (Cognito-gated, checks
`_has_finance_access`) and the older `/finance/{proxy+}` with `AuthorizationType: NONE`. On the
open route there is **no Cognito identity** — write/delete gating there leans on the
`FINANCE_VIEW_KEY` + `CONFIRMATION_CODE` only. Deletes are triple-gated, but the open route widens
the attack surface: anyone who learns the shared view key can reach more than "view".
**Safe move:** treat the view key as a shared secret, not an auth boundary. Plan to retire the open
route (frontend already has `/finance-secure`); confirm nothing external still calls `/finance/...`
before removing. Don't add *new* capabilities to the open route.

### 2. Many routes are `AuthorizationType: NONE` (~54 vs ~37 Cognito)  · sev: low–medium
Public reads (players, groups, tournaments, store, events, public walk-ins/UPI) are intentionally
open. The risk is drift: it's easy to add a *write* under a `NONE` parent by accident. **Safe move:**
when adding a route, decide auth explicitly and mirror the closest sibling; writes should be COGNITO
unless there's a real reason (and then gated in-code).

### 3. Committed AWS account ID in policy JSONs  · sev: low
`current-policy.json` and `networth-deploy-policy.json` contain the 12-digit account id
`593579469110`. Account IDs aren't secret-secret, but they don't belong in a public repo.
**Safe move:** parameterize with a placeholder or move these out of the repo; scrub history if the
repo is/will be public.

### 4. Self-signup ≠ membership, enforced only by `_requires_linked_member`  · sev: low
A Cognito account can exist with no linked player. Every write path relies on
`_requires_linked_member` / `_linked_player_is_live` to block "signed up but not a member" callers.
**Safe move:** any new write endpoint must call that gate; forgetting it silently lets stranger
accounts act.

### 4b. Legacy global `finance_role` still acts as a floor on the default group  · sev: low (design), RESOLVED 2026-08-19 (superseded)
Finance used to be club-GLOBAL (one `finance_role` per player), which meant a group owner granting
finance access would hand it out across every group - so `OWNER_DECIDABLE_TYPES` excluded
`finance_access`. Finance is now group-scoped (BACKLOG "Group-scoped finance", Stages 2-5): each
group has its own `finance_roles` map, `finance_access` requests always carry a `group_id`, and
`_owner_may_decide` requires the caller to own that specific group. `finance_access` is back in
`OWNER_DECIDABLE_TYPES` and the owner-facing per-member role selector is live in the group detail
panel (`setGroupFinanceRole`, gated on `canManageGroup`). **What's left:** the legacy global
`finance_role` on a player record still counts as a floor, but *only on the "Club (default)" group*
(`_group_finance_level`'s transition floor) - it never leaks into other groups. **Safe move:** when
granting broad legacy access via `set_finance_access` (SuperAdmin, `/finance-access` POST), remember
it only ever applies to the default group; use a group's own `finance_roles` map (or the
request→approve flow) for every other group.

### 17. `PUT`/`DELETE /matches/{match_id}` had no Cognito auth at all  · sev: high, RESOLVED 2026-08-20
Both routes were `AuthorizationType: NONE` with a shared `CONFIRMATION_CODE` as the ONLY gate inside
the Lambda - no caller identity was ever checked. Anyone who knew or guessed the code (a plain string
compare, not a Cognito credential) could edit/delete any match and trigger a full rating recompute
from a totally unauthenticated request. The frontend only ever exposed the direct-edit/delete UI to
`isSuperAdmin()`, but that's a UI nicety, not enforcement - the API itself never checked. Fixed
(Owner-asked "why are we still needing the code ... for the match it should be fine right", which
surfaced the actual gap): both routes are now `COGNITO_USER_POOLS`; the Lambda checks
`_caller_may_edit_match` (SuperAdmin, or owner/admin of the match's own `group_id` - same bar
`OWNER_DECIDABLE_TYPES` already applies when a non-admin requests the same change). The confirmation
code is gone from both the frontend prompts and the two handlers. **Safe move:** the request→approve
path's internal Lambda-to-Lambda invoke (`decide_claim_request` → `MATCHES_FUNCTION`) bypasses API
Gateway entirely, so it's unaffected by the auth-type change - it forges `requestContext.authorizer.
claims` with the already-vetted deciding user's own claims, which `_caller_may_edit_match` accepts on
the same terms as a direct call.

---

## Correctness / data integrity

### 5. Elo path-dependence — the recompute trap  · sev: high if mishandled
Editing, deleting, or reordering a historical match invalidates every later `ratings_after`.
`recompute_all_ratings()` exists (duplicated in matches **and** tournaments lambdas) to replay from
scratch. **Safe move:** never mutate a single match's rating fields directly; always trigger a full
replay. The `/recompute` route and the SuperAdmin "recompute now" button are the sanctioned path.

### 6. Duplicated logic across lambdas — silent divergence risk  · sev: medium
Copy-pasted (not shared) across files:
`sanitize_nickname` (register_player, groups, players **+** `sanitizeNickname` in app.js),
`recompute_all_ratings`, `compute_momentum_stats`, `compute_comeback_bonus`, `compute_adaptive_k`,
`get_pairing_count`, `_is_valid_completed_game` (matches + tournaments), and the per-lambda
`_caller_claims` / `_is_super_admin` / `_response`. Fix a bug in one copy and the others keep the
bug. The nickname one is the most dangerous — divergence breaks login-by-nickname and claim linkage.
**Safe move:** when editing any of these, edit **every** copy in the same change; longer term,
factor into a shared Lambda layer (backlog item).

### 7. Momentum / point-log can be bogus  · sev: low
Live split-screen scoring can produce malformed point logs (there's literally a repair script,
`clear_bogus_momentum.py`). Comeback-bonus math trusts `point_log`. **Safe move:** validate point
logs on write if you touch that path; don't assume a stored `momentum` block is well-formed.

### 15. Unpaginated `table.scan()` truncates silently at 1 MB  · sev: medium (latent)
`list_players` (players lambda) does `table.scan().get('Items', [])` with **no pagination**. DynamoDB
caps a scan page at 1 MB and returns a `LastEvaluatedKey` you must loop on; this code doesn't. Today
the players table fits one page (~55 rows) so it works, but as rows grow (more players, plus per-row
cosmetics/uploads/`owned_items`) whoever lands past the boundary silently vanishes from `/players`, and
therefore from the frontend `allPlayers` roster. That breaks everything reading `allPlayers`: dropdowns,
team pickers, stats, finance settlement, and each dropped player's own `hasLinkedPlayer()` (they read as
"no linked profile" despite a healthy Cognito link — a candidate cause in the 2026-08-07 Suren case
before it turned out to be a stale token). **Safe move:** paginate every `.scan()` (loop on
`LastEvaluatedKey`); grep all lambdas for bare `.scan(` — a copy-paste-prone shape. (Found 2026-08-07.)
**RESOLVED 2026-08-09:** `_scan_all()` helper added to both lambdas; all 9 players-table scans (incl.
`list_players`) + the matches/history/tournaments scans paginated. Only the small `claim_requests`/
`groups` scans remain bare (low risk).

---

## Ops / deploy fragility

### 8. Stale API Gateway stage → 500s with no CORS headers  · sev: medium (recurring)
When CloudFormation fails, the stage isn't refreshed and integrations 500 at the gateway level with
**no CORS headers**, so the browser reports a CORS error that's really a deploy failure.
**Safe move / fix:** `aws apigateway create-deployment --rest-api-id zywd1pvlm6 --stage-name prod`.
The deploy workflow already runs this as a dedicated step — keep that step.

### 16. Account Lambda concurrency = 10 → burst throttling → 500s with no CORS  · sev: medium
Same *symptom* as #8 (browser says "blocked by CORS / no Access-Control-Allow-Origin", really a raw
gateway 500), different *cause*. The account's `ConcurrentExecutions` limit is **10** (new-account
default; normal 1000), shared across all 8 functions. First paint fans out ~15 simultaneous
`/matches?...`/tournaments/finance calls, exceeds 10, and the overflow is **throttled by the Lambda
service before the function runs** — so it 500s with no CORS header **and leaves no CloudWatch log**
(nothing executed). Which requests lose the race is random, so the failing endpoints differ per reload.
**Distinguisher from #8:** a code exception leaves a traceback; a throttle leaves *no* invocation log
but shows on the `Throttles` CloudWatch metric. **Safe move:** (a) request a Service Quotas increase for
Lambda "Concurrent executions"; (b) cut the load-time fan-out (lazy-load per tab + one bundle endpoint
+ cached preflights) so first paint needs 2–3 invocations, not ~15. (BACKLOG Now/high; found 2026-08-07.)

**MITIGATED 2026-08-11 (partial):** Stats tab now uses a single `stats_bundle` call (4 concurrent
scans -> 1). The account quota increase is still the real fix.

### 9. Website bucket == uploads bucket  · sev: high if `--delete` used
User cosmetics live under `uploads/` in the same S3 bucket that serves the site. A
`aws s3 sync --delete` would wipe them. **Safe move:** deploy uses explicit `aws s3 cp` per path;
never introduce a `sync --delete`.

### 10. Frontend config lives in `index.html`, not `app.js`  · sev: low
`API_BASE_URL`, `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, UPI/finance placeholders are declared
in an inline `<script>` in `index.html`; `app.js` reads them as globals. The deploy `sed`-injects
values into `index.html` **only**. **Safe move:** if you move config into `app.js`, update the
injection step in `.github/workflows/deploy.yml` or the app boots with `REPLACE_ME` values.

### 11. `app.js` ↔ `index.html` name coupling  · sev: low
`app.js` is one IIFE; ~257 functions are invoked from inline `onclick=`/`onchange=` in `index.html`.
Renaming a JS function without updating the HTML produces a silent no-op on click. **Safe move:**
grep `index.html` for the old name on every rename.

### 12. `git push` rejections mid-session  · sev: low (annoyance)
Recurring: push rejected because remote moved. Recovery loop:
`git fetch` → `git pull --no-rebase --no-edit` → resolve any `<<<<<<<` markers → `git push`.
Nuclear: `git reset --hard origin/main` + re-extract the zip.

---

## AWS cost gotchas (billing, not bugs)

### 13. "Free tier" assumptions that don't hold  · sev: low (cost)
- API Gateway free tier is **12-month only**, not always-free.
- DynamoDB **on-demand** bills per request from the first call; the always-free allowance only
  applies to **provisioned** mode.
- Secrets Manager has a recurring per-secret charge with no permanent free tier.
**Safe move:** factor these in before adding new tables/secrets/endpoints; prefer provisioned DynamoDB
only if traffic is predictable and you actually want the free allowance.

---

## Housekeeping

### 14. Committed build artifacts  · sev: cosmetic
`__pycache__/*.pyc` (incl. a `matches/index.cpython-312.pyc`) shipped inside the snapshot zip even
though `.gitignore` covers them. **Safe move:** ensure they're not tracked in git (`git rm --cached`
if they are); they're just noise for an LLM reading the tree.
