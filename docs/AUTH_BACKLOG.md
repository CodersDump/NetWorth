# NetWorth — Authentication & Authorization Backlog

Guiding rule for every item below: **additive first, cutover second.** Nothing
here disables the current live site, the shared-secret gates, or your daily
match recording until a specific "cutover" ticket is deliberately actioned.

---

## Epic 1 — Cognito foundation (isolated, no live impact) ✅ DONE, verified live

- [x] Create a Cognito **User Pool** — username = email or phone, standard
      password policy, self-service password reset enabled.
- [x] Create an **App Client** (no client secret — this is a browser SPA,
      a secret can't be kept safe in JS anyway).
- [x] Enable **guest / unauthenticated access** — decide here whether
      "guest" means a Cognito Identity Pool unauthenticated identity, or
      simply "no login required, read-only" at the app layer. (Recommend
      the latter to start — simpler, and matches what the public Stats/
      walk-ins views already do today with zero auth.)
- [x] Add a **custom attribute** `custom:player_id` on the User Pool — this
      is the link between a Cognito identity and your existing
      `PlayersTable` row. Established at user-creation time for existing
      roster members (Epic 2), or via a claim step for brand-new signups.
- [x] Stand up a throwaway test page (not part of the real frontend yet) to
      confirm sign-up, login, forced password change, and password reset
      all work end-to-end before touching anything live.

**Exit criteria:** ✅ MET — verified via `auth-test/index.html` against the real deployed pool: sign-up, email confirmation, login, and a real decoded token all worked. (One deploy hiccup along the way: the GitHub Actions deploy user's IAM policy needed `cognito-idp:*` permissions added - fixed in `current-policy.json`, documented there for anyone who re-reads this.)

---

## Epic 2 — Bulk-provision existing players *(what you asked for directly)* ✅ DONE

- [x] Write `scripts/provision_cognito_users.py`:
  - Reads every player from `PlayersTable`.
  - For each, calls Cognito `AdminCreateUser` with:
    - a **default temporary password** (same string for everyone is fine —
      see note below on why that's an acceptable trade-off here)
    - `custom:player_id` set to their existing `player_id` — this is what
      makes login "just work" against their real match history/ratings
      from the first login, no separate claim flow needed for people
      already on the roster
    - `MessageAction=SUPPRESS` so Cognito doesn't try to auto-email/SMS
      anyone (you're distributing the password yourself, e.g. WhatsApp)
  - Prints a clean **username + temp password list** for you to copy out
    and distribute. This list is never committed to the repo.
  - Idempotent: re-running skips players who already have a Cognito user.
- [x] Decide the username scheme: email if everyone has one on file, phone
      number otherwise, or a simple `firstname.lastname` if neither — this
      affects what you actually send people to log in with.

**On the shared default password:** Cognito forces a password change on
first login when a user is created via `AdminCreateUser` without the
"permanent" flag (`FORCE_CHANGE_PASSWORD` status) — so the shared string is
single-use per account by construction. Reasonable for a trusted club
group; flagging it so it's a conscious choice, not an assumption.

**Exit criteria:** script built and tested (dry-run, apply, idempotent re-run, super-admin grant) - ready to run for real against the actual roster whenever emails are collected.

---

## Epic 3 — Authorization model (three tiers, confirmed) - data model DONE, enforcement pending Epic 4

Two different *kinds* of role, deliberately modeled with two different AWS
mechanisms — using the right tool for each rather than one system trying
to do both jobs:

| Tier | Scope | Where it lives |
|---|---|---|
| **Guest** | read-only, everywhere | no login at all — limited to routes already left open today (Stats, public walk-ins) |
| **Group owner/admin** | only *their* group(s) | `role` field per (group, player) on `GroupsTable` — resource-scoped, grows as groups grow |
| **Super-admin (you)** | everything, every group | a **Cognito pool-wide Group** (e.g. `SuperAdmin`) — small, rarely-changing, checked via the ID token's `cognito:groups` claim |

Every write-permission check becomes: *is the caller in the `SuperAdmin`
Cognito group, OR are they `owner`/`admin` of this specific club-group in
DynamoDB?* — super-admin always short-circuits to yes; everyone else gets
the per-group check. This avoids conflating "AWS-level pool role" with
"which of my 20+ groups does this person run," which don't scale the same
way.

- [x] Create the `SuperAdmin` Cognito Group (built in Epic 1). Adding your
      own account to it: use `provision_cognito_users.py --grant-super-admin`.
- [x] Extend `GroupsTable` with a `role` per (group, player): `owner` /
      `admin` / `member`. Groups Lambda updated: `create_group` accepts an
      optional `creator_player_id` -> owner; `add_player` accepts an
      optional `role` (default `member`) and never clobbers an existing
      role on re-add; new `PUT /groups/{id}/roles/{player_id}` endpoint;
      `remove_player` cleans up the departing member's role entry;
      `get_group` returns each member's role. Fully unit-tested.
- [x] One-time backfill script: `scripts/backfill_group_roles.py`. Guesses
      the first member in `member_ids` as owner, prints every proposal for
      review, supports a `group_owner_overrides.csv` for wrong guesses,
      idempotent (skips groups that already have roles). Tested: guess,
      override-beats-guess, empty-group skip, and idempotent re-run.
- [x] New-group creation flow: creator is automatically `owner` of that
      group (not super-admin — that stays manually granted, just you).
- [ ] Owner/admin-only actions defined explicitly, per group (invite/remove
      members, edit group tournament defaults, promote another member to
      admin of that group). *(data model supports this now; which specific
      actions require which role is a decision for Epic 4's wiring, not
      blocking - the role data already exists to check against.)*
- [ ] Guest-visible routes enumerated explicitly (currently: Stats tab
      reads, public walk-in list) — everything else requires a login,
      including anything that writes.

**Exit criteria:** ✅ data model MET - roles CRUD built and tested, backfill
script ready to run against real groups. Guest-visible routes still need
explicit enumeration (small remaining item above). Enforcement is Epic 4,
as planned - nothing here changes what's currently allowed to call these
routes, it only makes the role data available to check against once Epic 4
adds the check.

---

## Epic 4 — Wire authorization into the API (careful, one route at a time)

- [ ] Add a **Cognito Authorizer** to API Gateway, attached to **zero
      existing routes** at first.
- [ ] Attach it to one low-risk **new** route first, confirm end-to-end
      that a valid JWT is required and a missing/bad one is rejected.
- [ ] Only then, one existing route at a time: add the **super-admin OR
      group-owner check** *alongside* the current `CONFIRMATION_CODE` /
      `FINANCE_VIEW_KEY` check (both must pass), never replacing until
      proven.
- [ ] Once every gated route has been running dual-checked without issue
      for a while → **cutover ticket**: drop the old shared-secret check,
      keep only the role check.

**Exit criteria:** every previously-secret-gated action is now gated by
"are you an authenticated admin/owner of this specific group," and the two
old shared codes are no longer required anywhere.

---

## Epic 5 — Frontend auth UI

- [ ] Login screen (username + password), forced-change-password screen,
      "forgot password" flow — all standard Cognito hosted flows or a thin
      custom UI over the Cognito API, your call on look-and-feel.
- [ ] Session token storage: works today via browser storage; upgrades
      cleanly to an httpOnly cookie once CloudFront unification (separate,
      optional epic) removes the cross-origin split.
- [ ] Role-aware UI: hide admin-only buttons (delete, edit expenses, group
      settings) from non-admins per group, rather than relying solely on
      the backend rejecting the call (better UX, backend stays the real
      guard either way).
- [ ] Mismatch handling: if someone's Cognito account name doesn't match
      what's shown, point them at the **existing player-rename endpoint**
      (`PUT /players/{player_id}`) — already propagates through frozen
      match/tournament name snapshots via `rename_player_history.py`
      logic, so no new rename mechanism is needed, just surfaced in the
      logged-in UI.

**Exit criteria:** a real person can log in on their phone, see only what
their role allows, and rename themselves if their account name is wrong.

---

## Epic 6 — Claim flow for brand-new (not-yet-rostered) people

- [ ] For anyone who isn't already a `PlayersTable` row (a genuinely new
      recruit, not a re-provisioned existing player): admin creates a
      placeholder player record with a short claim code; new signup enters
      the code once at first login to link their fresh Cognito identity to
      that placeholder `player_id`.
- [ ] This is separate from Epic 2 because existing players don't need a
      claim step — they're pre-linked at creation time via
      `custom:player_id`. This epic is only for people who join *after*
      the migration.

---

## Epic 7 — Guest visibility scoping (parked, exact spec captured 2026-07-23)

Right now "guest" just means "not logged in," with no tab-level
restriction at all — a guest sees the same tabs a logged-in member does,
just without access to the write actions we've locked down so far. The
actual intended guest experience, per-tab:

| Tab | Guest access |
|---|---|
| Matches | Match log / results only — no recording new matches |
| Tournaments | View existing tournaments only — no creating new ones |
| Stats | Fully accessible, no restriction |
| Profile | Fully accessible, no restriction |
| Finance | **Only the public UPI QR payment card** — no settlement, expenses, memberships, walk-ins, or insights, even read-only |
| Players / Groups | Not specified yet — likely read-only list, same open question as write actions generally |

Implementation shape (when picked up): mostly a frontend tab-rendering
change (`isLoggedIn()` already exists from Epic 5 - gate tab *content*,
not just buttons, based on it), plus possibly new read-restrictions on the
finance Lambda's non-delete routes so a guest hitting the API directly
(bypassing the UI) is equally restricted, not just visually hidden.

**Not started. No code changes yet for this epic - noted here so the
exact spec survives until it's picked up.**

---

- **CloudFront unification** (single domain for frontend + API, drops
  CORS, enables httpOnly cookies) — can happen any time, independent of
  auth work.
- **Route 53 custom domain** — explicitly parked for later per your call.
- **Mobile app (Capacitor)** — authenticates against the same Cognito User
  Pool once it exists, so doing auth now avoids solving it twice.

---

## What never changes as a side effect of any of the above

Elo/rating math, Hall of Fame, achievements, progress-history scheduler,
finance settlement math, all six existing Lambdas' business logic, all
DynamoDB tables and their current data, the GitHub Actions deploy pipeline,
and your ability to record matches/scores on the live site — throughout
every epic above, until a specific cutover ticket says otherwise.
