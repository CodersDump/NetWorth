# AGENTS.md — how any LLM/agent should work on NetWorth

> This file is the **entry point** for any AI assistant working on this repo — Claude,
> qwen3-coder via aider, Cursor, Continue, whatever. Read this first, then read
> `docs/CODEBASE_MAP.md`. You should almost never need to open every source file:
> the map lists every function with a one-line description and line number.
>
> `AGENTS.md` is a filename many agentic tools auto-discover. For **aider**, either add it
> with `/read AGENTS.md docs/CODEBASE_MAP.md` or put those paths under `read:` in
> `.aider.conf.yml` so they load as read-only context on every session.

---

## 0. What NetWorth is

AWS-serverless badminton-club manager: static SPA (S3+CloudFront) → API Gateway →
8 Python 3.12 Lambdas → 7 DynamoDB tables, Cognito auth. Went live 2026-07-19.
Owner runs it solo; **do not break production.**

Key coordinates: repo `CodersDump/NetWorth` · region `us-east-1` · stack `networth-app` ·
API GW `zywd1pvlm6` · CloudFront `d1mdot1vsm6xu6.cloudfront.net` · Cognito pool `us-east-1_svy5Sv8Av`.

---

## 1. Read order (do this before writing code)

1. **This file** (`AGENTS.md`) — conventions + guardrails.
2. **`docs/CODEBASE_MAP.md`** — the function-level map. Find the function you need here;
   jump straight to its file+line instead of scanning.
3. **`docs/KNOWN_ISSUES.md`** — the traps/loopholes. Check whether your task touches one.
4. **`docs/BACKLOG.md`** — the roadmap. Log new work here; pick from here if asked "what next".

Only open actual source (`backend/lambdas/*/index.py`, `frontend/js/app.js`) once you know
the exact function you need to see or change.

---

## 2. Golden rules (violating these has bitten this project before)

1. **Elo is path-dependent.** Any change to a *historical* match (edit/delete/reorder) means
   a full replay via `recompute_all_ratings()`. Never patch one match's `ratings_after` in place.
2. **The S3 website bucket also holds user uploads** under `uploads/`. Deploy uses explicit
   `aws s3 cp` — **never `s3 sync --delete`**, which would wipe every uploaded cosmetic.
3. **CORS / stale API Gateway stage:** when CloudFormation fails, the stage isn't refreshed and
   integrations return gateway-level **500s with no CORS headers**. Fix:
   `aws apigateway create-deployment --rest-api-id zywd1pvlm6 --stage-name prod`.
4. **Nickname sanitizer is duplicated** in 3 lambdas + `app.js`. Change one → change all four.
5. **`authedFetch()` is the only correct way to call secure routes** from the frontend — it
   refreshes near-expired Cognito tokens. Don't hand-roll `fetch` with a stale token.
6. **`app.js` is one IIFE wired to inline `onclick=` in `index.html`.** Rename a JS function →
   grep and update `index.html` too.
7. **Never commit secrets/PII.** `.env`, `player_emails.csv`, `.aider*`, `__pycache__/` are
   gitignored. Account IDs currently sit in committed policy JSONs — see KNOWN_ISSUES.

---

## 3. Change protocol (how to deliver a fix)

The owner deploys from **Windows PowerShell**. Package and instruct accordingly.

**Packaging**
- Deliver edits as a `NetWorth.zip` with `.git` stripped, OR as precise per-file diffs if the
  tool applies them in place (aider does — prefer diffs when using aider locally).
- Deploy flow: unzip → copy contents of the `NetWorth/` folder over the repo → `git add/commit/push`
  → GitHub Actions runs `.github/workflows/deploy.yml`.
- **Never use `Copy-Item`** to move files into the repo (it previously spilled a venv into the
  repo root). Extract/overwrite explicitly.

**PowerShell command style**
- Group commands into **separate blocks by execution stage** — (a) local checks, (b) commit+push,
  (c) post-deploy verification. Never combine stages into one block. Anything that waits on a prior
  step goes in its own block with a note on when to run it.
- Use `-Encoding ascii` (not `utf8` — avoids a BOM issue). Single-quote AWS filter args.

**Git push-rejection recovery loop** (recurring pain point):
```
git fetch
git pull --no-rebase --no-edit
# inspect for <<<<<<< conflict markers, resolve if any
git push
```
Nuclear option (last resort, discards local): `git reset --hard origin/main` then re-extract the zip.

---

## 3a. Change-delivery SOP (do this on EVERY change — owner's standing request)

For any code change, the assistant delivers, in this order:

1. **Update the docs in the same change.** If functions were added/renamed/removed →
   regenerate the map: `python tools/generate_codebase_map.py`. If routes, data model, infra,
   or a trap changed → hand-edit `docs/CODEBASE_MAP.md` (§2–4, §8) and/or `docs/KNOWN_ISSUES.md`.
   Log the work in `docs/BACKLOG.md` (move to **Done** with today's date when shipped).
2. **Zip only the files that changed** (plus any regenerated docs), `.git` stripped, in repo-relative
   paths so it unzips cleanly over the tree.
3. **Give the git staging block** (stage the changed files → commit → push to the working/`staging`
   branch). See the flow below for exact commands.
4. **Give the post-merge release block** — to run *after* the `staging → main` PR is merged —
   that cuts a version tag and pushes it (the tag push is what deploys prod).

Blocks 3 and 4 are always **separate** command blocks (block 4 waits on the human merging the PR).

## 3b. Release flow (matches the workflows — don't shortcut it)

The pipeline is deliberately gated. Merging to `main` does **not** deploy prod; a `v*` tag does.

```
feature work ──push──▶ staging branch ──▶ deploy-staging.yml ──▶ staging URL (prod untouched)
      │                                                              │ verify here
      └────────────── open PR: staging ─▶ main ◀── branch protection needs staging green
                                   │ merge (only STAGES code as ship-ready)
                                   ▼
                        push tag  v1.2.3  ──▶ deploy.yml ──▶ on-main-gate ──▶ PROD
                        (also: publish a Release, or "Run workflow" dispatch)
```

- `deploy-staging.yml` fires on **push to `staging`** — frontend-only preview, reads prod's
  API/Cognito config, uploads to the staging bucket. Creates/changes no backend.
- `deploy.yml` fires on **`workflow_dispatch` / Release published / push of a `v*` tag**, and its
  `on-main-gate` job **rejects any tag whose commit isn't an ancestor of `main`** — so you can only
  ship staging-verified commits. It also force-refreshes the API GW stage (the CORS-500 fix) and
  `cp`s (never `sync --delete`) the frontend.

**Block 3 — stage + commit + push to staging** (PowerShell, ASCII):
```powershell
# Run from the repo root, on your working branch (or: git checkout staging)
git add backend/lambdas/<changed>/index.py frontend/js/app.js `
        docs/CODEBASE_MAP.md docs/KNOWN_ISSUES.md docs/BACKLOG.md AGENTS.md
git status                              # eyeball what's staged
git commit -m "feat: <what changed> (+docs)"
git push origin staging                 # -> deploy-staging.yml -> verify on the staging URL
```
Then open the `staging → main` PR on GitHub and merge it once staging is green.

**Block 4 — cut + push the release tag (RUN ONLY AFTER the PR is merged to main):**
```powershell
git checkout main
git pull origin main                    # get the just-merged commit locally
git tag -a v1.2.3 -m "NetWorth v1.2.3: <summary>"
git push origin v1.2.3                  # -> deploy.yml -> on-main-gate -> PROD deploy
```
Use semver: patch for fixes, minor for features, major for breaking changes. If a tag deploy is
rejected by `on-main-gate`, the tagged commit isn't on `main` — merge the PR first, then re-tag.

If `git push origin staging` (or the tag push) is rejected because the remote moved:
`git fetch` → `git pull --no-rebase --no-edit` → resolve any `<<<<<<<` markers → push again.

## 4. Guardrails for autonomous/local agents (aider, qwen3-coder)

- **Ask before destructive AWS calls.** Deletes, `recompute`, CloudFormation stack ops, and
  anything touching Cognito users or DynamoDB in prod must be confirmed with the owner first.
- **Don't invent AWS resource IDs.** Use the ones in §0 / CODEBASE_MAP; if unsure, read stack outputs.
- **Prefer reading the map over grepping the whole tree** — it's cheaper on a local model's context
  window, which is the whole reason this doc set exists.
- **Keep diffs small and staged.** One concern per change; match the existing code style
  (the lambdas favor small helper functions + a `_response()` CORS wrapper).
- **When you finish, update the docs:** append completed work to `docs/BACKLOG.md`'s Done section,
  and if you added/renamed functions, note that `docs/CODEBASE_MAP.md` needs regenerating.

---

## 5. Regenerating the map

`CODEBASE_MAP.md` is generated: Python functions via `ast`, `app.js` via regex on
`function name(...)` / `const name = (...) =>` plus `// ==== section ====` banners.
When code changes substantially, re-run that extraction rather than hand-editing the tables.
(The extraction logic lives in the owner's local tooling — see `docs/BACKLOG.md` → "Tooling".)
