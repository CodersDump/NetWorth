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

### 4b. Finance roles are club-GLOBAL, which constrains group-owner delegation  · sev: low (design)
`finance_role` is a single global level per player, and the finance tab shows one shared club
finance. So finance approval can't yet be safely delegated to group owners: granting a member finance
access grants it across every group. This is why `OWNER_DECIDABLE_TYPES` deliberately excludes
`finance_access` (group owners approve claim/rename only). **Safe move:** don't add `finance_access`
to owner-decidable types until finance is group-scoped (BACKLOG "Now / high priority").

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
