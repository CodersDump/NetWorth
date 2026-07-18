# NetWorth

A small serverless app to organize badminton tournaments for the group -
registration, score-based Elo ranking, and (later) matchup trend analysis.

## Architecture

- **S3** - static site hosting for the frontend (shareable link, no login yet)
- **API Gateway** - REST endpoints
- **Lambda (Python)** - registration, score entry, ranking, matchup stats
- **DynamoDB** - `Players` and `Matches` tables (matches are append-only, so
  rankings and trend analysis can be computed from history later)

## Repo structure

```
networth-app/
├── .github/
│   └── workflows/
│       └── deploy.yml      # GitHub Actions: deploy stack + sync frontend on push to main
├── infrastructure/
│   └── template.yaml       # CloudFormation: DynamoDB, Lambda, API Gateway, S3
├── backend/
│   └── lambdas/
│       └── register_player.py   # source of truth for the Lambda code
├── frontend/
│   └── index.html          # registration form, calls the API
└── README.md
```

## CI/CD (GitHub Actions)

`.github/workflows/deploy.yml` runs on every push to `main`: deploys the
CloudFormation stack, then syncs `frontend/index.html` to S3 with the live
API URL auto-injected.

To use it, add these repo secrets (Settings -> Secrets and variables ->
Actions) using an IAM user scoped to your personal AWS account (not a work
one):

- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION` (e.g. `us-east-1`)

Once set, pushing to `main` deploys automatically - no manual CLI steps
needed after that.

> Note: the Lambda code is currently inlined directly in
> `template.yaml` (via `ZipFile`) so the whole stack deploys in one command.
> `backend/lambdas/register_player.py` is the same code kept separately for
> easier editing/version history - when you change it, copy the updated code
> back into the `ZipFile` block in `template.yaml` before redeploying.
> Once you outgrow inline code (multiple Lambdas, extra dependencies), switch
> to packaging a `.zip` and referencing an S3 bucket in `Code:` instead.

## Deploy

Prereqs: AWS CLI configured with your personal AWS account/profile.

```bash
aws cloudformation deploy \
  --template-file infrastructure/template.yaml \
  --stack-name networth-app \
  --capabilities CAPABILITY_NAMED_IAM \
  --profile YOUR_PERSONAL_PROFILE
```

After it finishes, get the outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name networth-app \
  --query "Stacks[0].Outputs" \
  --profile YOUR_PERSONAL_PROFILE
```

This gives you:
- `WebsiteUrl` - the shareable link for your teammates
- `ApiUrl` - the API base URL (paste this into `frontend/index.html`,
  replacing `API_BASE_URL`)

## Upload the frontend

```bash
aws s3 cp frontend/index.html s3://networth-site-<your-account-id>/index.html \
  --profile YOUR_PERSONAL_PROFILE
```

(Bucket name comes from the CloudFormation output / `WebsiteBucket` resource.)

## Roadmap

1. **v1 (this)** - player registration
2. **v2** - match score entry + Elo rating update
3. **v3** - matchup trend analysis (head-to-head win rates, performance vs
   skill tier) once a few weeks of match history exist
4. **v4 (later)** - Cognito login layer, added on top of existing API Gateway
   routes without rewriting Lambda logic
