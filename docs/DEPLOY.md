# NetWorth — Deploy Runbook

> Not previously written down anywhere in this repo — codified 2026-08-29 after
> being asked "is this not written in some doc or something?" It wasn't. The
> workflow YAML files (`deploy.yml`, `deploy-staging.yml`) document what CI
> does once code reaches a branch/tag; this file documents the local habit of
> getting a change (usually a zip of updated files from a Claude session) from
> a download into that branch/tag in the first place.

## 1. Getting a change from a Claude session into `staging`

Claude should hand back a **folder-wise delta zip** — only the files that
actually changed, at their real repo-relative paths (e.g. `frontend/js/app.js`,
not the whole repo) — named `networth_vX.Y.Z.zip` for the version it's meant
to become. If a session hands back something else, ask for it in this shape.

```powershell
cd C:\path\to\NetWorth
git checkout staging
git pull origin staging
Expand-Archive -Path "$env:USERPROFILE\Downloads\networth_vX.Y.Z.zip" -DestinationPath "$env:TEMP\networth_vX.Y.Z_extract" -Force
Copy-Item "$env:TEMP\networth_vX.Y.Z_extract\*" -Destination . -Recurse -Force
git add <only the files that actually changed>
git commit -m "<what changed and why>"
git push origin staging
```

Pushing to `staging` fires `.github/workflows/deploy-staging.yml` automatically
— it deploys the frontend to the staging bucket/URL, injecting the SAME
API/Cognito config prod uses. It changes no backend and creates nothing new;
staging currently shares prod's actual backend/DB (see `BACKLOG.md` →
"Isolated staging (data clone)" — not built yet, so don't rely on staging as a
sandbox for anything destructive).

Go verify the change on the staging URL before moving on.

## 2. Promoting staging → prod

1. Open a PR from `staging` into `main`. Branch protection blocks the merge
   until the staging deploy above is green.
2. After merging:

```powershell
git checkout main
git pull origin main
git tag vX.Y.Z
git push origin vX.Y.Z
```

Pushing a `v*` tag fires `.github/workflows/deploy.yml`. It first re-verifies
the tagged commit actually reached `main` via that PR gate (`on-main-gate`),
then always redeploys the **whole stack** — `infrastructure/template.yaml`
via `aws cloudformation deploy` (backend Lambdas + infra, whether or not they
changed this release), a forced `aws apigateway create-deployment` (needed
any time a route changed — see `KNOWN_ISSUES.md` → "Ops / deploy fragility"),
then the frontend (`index.html`/`css/`/`js/`, each `aws s3 cp` with
`--cache-control "no-cache"`, never a destructive `sync --delete` — that
bucket also holds user-uploaded cosmetics). A manual "Run workflow" dispatch
or publishing a GitHub Release trigger the identical pipeline; the tag push
is just the habitual path.

## Version numbering

Minor bump (`v1.X.0`) per feature/session; patch bump (`v1.X.Y`) for a
same-day fix to something not yet released. **Last deployed: v1.75.0.**

## Frontend-only changes (the common case)

If a change touches only `frontend/`, steps 1–2 above are the entire runbook
— the prod pipeline redeploys the backend stack unconditionally regardless,
so there's nothing extra to remember to do for a frontend-only release.
