# NetWorth

A small serverless app to organize badminton tournaments for the group -
registration, score-based Elo ranking, and (later) matchup trend analysis.

## Architecture

- **S3** - static site hosting for the frontend (shareable link, no login yet)
- **API Gateway** - REST endpoints
- **Lambda (Python)** - registration, players, groups, matches (Elo), tournaments
- **DynamoDB** - `Players`, `Groups`, `Matches`, `Tournaments` tables
- **S3 (artifacts bucket)** - stores packaged Lambda `.zip` files during deploy
  (created automatically by the workflow if it doesn't exist)

> **Why packaged, not inline?** CloudFormation's inline Lambda code (`ZipFile`)
> has a hard 4096-byte limit. Once functions grew past a few lines (groups,
> matches, tournaments), we switched to referencing local folders in
> `backend/lambdas/*` from the template, and let `aws cloudformation package`
> zip + upload them to S3 automatically before every deploy.

## API routes

| Method | Path | Purpose |
|---|---|---|
| POST | `/register` | Register a new player |
| GET | `/players` | List all players |
| DELETE | `/players/{player_id}` | Delete a player |
| POST | `/groups` | Create a group |
| GET | `/groups` | List all groups |
| GET | `/groups/{group_id}` | Get a group + its members |
| POST | `/groups/{group_id}/players` | Add a player to a group (body: `player_id`) |
| DELETE | `/groups/{group_id}/players/{player_id}` | Remove a player from a group |
| POST | `/matches` | Record a standalone match score, updates Elo ratings |
| GET | `/matches?group_id=X&player_id=Y` | Game log, optionally filtered |
| POST | `/tournaments` | Create a tournament (`knockout` or `groups_then_knockout`) |
| GET | `/tournaments?group_id=X` | List tournaments |
| GET | `/tournaments/{tournament_id}` | Get tournament detail + standings |
| POST | `/tournaments/{tournament_id}/group-score` | Record a group-stage fixture score |
| POST | `/tournaments/{tournament_id}/knockout-score` | Record a knockout match score |

## Repo structure

```
networth-app/
├── .github/
│   └── workflows/
│       └── deploy.yml      # GitHub Actions: package + deploy stack + sync frontend
├── infrastructure/
│   └── template.yaml       # CloudFormation: DynamoDB, Lambda, API Gateway, S3
├── backend/
│   └── lambdas/
│       ├── register_player/index.py
│       ├── players/index.py
│       ├── groups/index.py
│       ├── matches/index.py
│       └── tournaments/index.py
├── frontend/
│   └── index.html          # registration, groups, game log, tournaments UI
└── README.md
```

## Deploy

Handled automatically by `.github/workflows/deploy.yml` on every push to
`main`. It:

1. Ensures an S3 artifacts bucket exists (`networth-artifacts-<account-id>`)
2. Runs `aws cloudformation package` - zips each folder under
   `backend/lambdas/` and uploads to the artifacts bucket
3. Runs `aws cloudformation deploy` using the packaged template
4. Injects the live API URL into `frontend/index.html` and syncs it to the
   website S3 bucket

### Manual deploy (if you ever need it)

```powershell
aws cloudformation package `
  --template-file infrastructure/template.yaml `
  --s3-bucket networth-artifacts-<your-account-id> `
  --output-template-file packaged-template.yaml

aws cloudformation deploy `
  --template-file packaged-template.yaml `
  --stack-name networth-app `
  --capabilities CAPABILITY_NAMED_IAM
```

### Deploy user IAM policy

The IAM user used by GitHub Actions needs S3 permissions for the artifacts
bucket, plus CloudFront permissions (for the HTTPS distribution), in addition
to the CloudFormation/DynamoDB/Lambda/API Gateway/IAM permissions from before:

```json
{
  "Sid": "ArtifactsBucketAccess",
  "Effect": "Allow",
  "Action": [
    "s3:CreateBucket",
    "s3:HeadBucket",
    "s3:PutObject",
    "s3:GetObject",
    "s3:ListBucket"
  ],
  "Resource": [
    "arn:aws:s3:::networth-artifacts-*",
    "arn:aws:s3:::networth-artifacts-*/*"
  ]
},
{
  "Sid": "CloudFrontAccess",
  "Effect": "Allow",
  "Action": [
    "cloudfront:CreateDistribution",
    "cloudfront:GetDistribution",
    "cloudfront:GetDistributionConfig",
    "cloudfront:UpdateDistribution",
    "cloudfront:DeleteDistribution",
    "cloudfront:TagResource",
    "cloudfront:ListTagsForResource",
    "cloudfront:CreateInvalidation",
    "cloudfront:ListDistributions"
  ],
  "Resource": "*"
}
```

> CloudFront resources are global (not tied to a region) and don't support
> name-prefix scoping like the other services here, so this uses `"*"` for
> the Resource - acceptable since it's your own personal account.

## Repo secrets required

Add these under Settings -> Secrets and variables -> Actions, using an IAM
user scoped to your personal AWS account:

- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION` (e.g. `us-east-1`)

## Roadmap

1. **v1** - player registration, list, delete
2. **v2** - groups (create, add/remove members), match score entry with
   automatic Elo rating updates, filterable game log (by group or player)
3. **v3 (this)** - tournaments: random knockout brackets, or FIFA-style
   groups-then-knockout, each saved as its own entry with live standings and
   bracket progression
4. **v4** - matchup trend analysis (head-to-head win rates, performance vs
   skill tier) once a few weeks of match history exist
5. **v5 (later)** - Cognito login layer, added on top of existing API Gateway
   routes without rewriting Lambda logic
