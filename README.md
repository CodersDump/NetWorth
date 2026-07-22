# NetWorth

A serverless badminton club management app for the Matchpoint group — player registration, team-average Elo rankings, tournaments with brackets, permanent progress history with streaks and badges, a Hall of Fame, per-player achievements, and a private finance ledger that replaced the club's Excel sheet. Live since the July 19, 2026 tournament.

## Architecture

```
                         GitHub Actions (deploy.yml)
                 secrets ──► CloudFormation NoEcho params ──► Lambda env vars
                                        │
   Browser ──► S3 / CloudFront ──► API Gateway (REST, /prod)
   (static index.html)                  │
                        ┌───────────────┼──────────────────────┐
                        ▼               ▼                      ▼
                  Lambda (Python 3.12, one per domain)   EventBridge (daily)
                  register · players · groups ·                │
                  matches · tournaments · finance        progress_scheduler
                        │                                      │
                        ▼                                      ▼
                  DynamoDB (PAY_PER_REQUEST)
                  players · groups · matches · tournaments ·
                  progress-history · finance
```

- **Frontend** — a single static `index.html` (no framework, no build step) on S3. Every feature is a card in one of seven tabs: Players, Groups, Matches, Tournaments, Stats, Finance, Profile.
- **Backend** — six Lambdas behind API Gateway `{proxy+}` routes, one per domain. Functions are packaged from `backend/lambdas/*` by `aws cloudformation package` (inline `ZipFile` has a 4 KB limit).
- **Scheduler** — an EventBridge rule triggers `progress_scheduler` daily; on period boundaries it permanently locks in the previous week/month/year's badge winners.
- **Secrets** — the destructive-operation confirmation code and the finance view key are **never in the repo**. They live in GitHub repository secrets, flow into CloudFormation as `NoEcho` parameters at deploy time, and reach the Lambdas as environment variables. Rotating a key = edit the GitHub secret, re-run the deploy.

## The rating system

Every player starts at **1000**. Matches are replayed strictly chronologically — Elo is path-dependent, so any correction (edited score, deleted match, cleared data) triggers a full replay from scratch rather than a local patch.

**Team-average Elo.** For doubles, each side's rating is the average of its two players. Expected score for side A:

```
E_A = 1 / (1 + 10^((R_B − R_A) / 400))
delta = K × (actual − expected)        actual ∈ {1, 0}
```

Both partners on a side receive the **same delta** — which means a fixed pair moves in lockstep and ties in "most improved" are structural, not rare. The app records co-winners rather than picking one by accident.

**Adaptive K (doubles).** K depends on how many times that exact pairing has played together: **40** for a brand-new pairing (high information), **32** while the pairing has fewer than 5 matches, **20** once established (dampens lockstep swings for fixed partners). Singles always uses flat K = 32.

**Comeback bonus.** Matches recorded with the live point-by-point counter store a `point_log`. If the eventual winner overcame a mid-game deficit of **5+ points**, they earn a bonus of `deficit × 0.3`, capped at **+8**, on top of the normal delta. Manually entered scores can never earn this — a final score alone can't prove a comeback.

**Batch-entry guard.** A real ~40-point game changes scorer dozens of times; someone misusing the live counter to punch in a final score (e.g. 20 points to one side, then 22 to the other) produces 1–3 unbroken scoring runs. Any point log with **20+ points in ≤3 runs** is flagged `suspected_batch_entry`, its "deficit overcome" is zeroed, and it can neither earn a comeback bonus nor appear in the Hall of Fame comebacks.

## Progress history, streaks, and badges

Daily, the scheduler checks whether a week (Monday UTC), month, or year just ended. For each completed period it computes, per scope (**global** plus each group):

- **Most improved** — each player's rating delta across the period: last rating inside the period minus last rating before it (1000 if they'd never played). A group scope only restricts *who can win the badge*; every player's delta still comes from their full match history.
- **Most active** — most matches played in the period.

All **co-winners of a tie are recorded together** (sorted deterministically), and streaks / "times held" counts credit each co-winner individually: if A & B tie this week and only A also won last week, A is on a 2-streak and B on a 1-streak. Winners are written to `progress-history` under a deterministic id (`scope#period#period_start`), so recomputation after a data repair overwrites cleanly instead of duplicating. `scripts/backfill_progress_history.py` rebuilds the whole table with identical logic.

## Hall of Fame (Stats tab)

All computed from full chronological match history; a group filter limits who *appears*, not which matches count.

| Section | How it's calculated |
|---|---|
| Longest win streak | Best consecutive-wins run by any player, ever |
| Biggest blowout | Largest score margin on record |
| Peak ratings | Each player's highest rating ever reached |
| Giant-killer upsets | Wins where the losing side's pre-match team rating was higher; ranked by the gap |
| Best comebacks | Largest deficits overcome, live-scored matches only (see guard above) |
| Most consistent / volatile | Population std-dev of per-match rating deltas, min 3 matches |
| Format specialists | Biggest gap between singles and doubles win rate, min 2 matches in each |
| Deep-run rate | Share of tournament entries that reached a knockout-stage match |
| Best partnerships | Doubles pairs by win %, min 3 matches together |
| Session MVPs | Best total rating gain on each play date (ties → co-MVPs) |
| Biggest single-match swings | Largest positive rating change in one match |
| Deuce specialists | Wins by exactly 2 points past 21 (the game reached deuce) |
| Undefeated sessions | Days with 3+ matches and zero losses |

## Achievements (Profile tab)

Tiered cards show progress as `current/next-target` with a matching progress bar; badge cards are held by the current record-holder and can change hands.

| Tiered card | Measures | Tiers |
|---|---|---|
| 🎮 Court Regular | matches played | 1 · 10 · 50 · 100 · 250 · 500 · 1000 |
| 🏆 Conqueror | tournament championships | 1 · 5 · 10 · 25 |
| 🔥 On Fire | best personal win streak | 3 · 5 · 10 · 15 |
| 🏅 Podium | champion + runner-up + 3rd-place finishes | 1 · 3 · 5 · 10 |
| 🎲 Deuce Demon | wins by 2 after deuce | 1 · 5 · 15 · 30 |
| 🛡️ Iron Day | undefeated sessions (3+ matches, 0 losses) | 1 · 3 · 5 · 10 |
| 📅 Ever-Present | best streak of consecutive club sessions attended | 3 · 5 · 10 · 20 |
| ⛰️ Summit | peak rating reached | 1050 · 1100 · 1150 · 1200 |

Record-holder badges (one holder each, min-thresholds as above): 🥇 Longest win streak · 📈 Peak performer · 🎯 Most consistent · 💥 Giant killer · 🔄 Comeback king · 💪 Blowout winner · 🎾 Format specialist · 🚀 Deep run master · 🌟 Rising star (most improved this week/month/year) · ⚡ Most active.

The profile also shows an auto-computed rivalry callout: your **😤 nemesis** (worst record against, min 3 meetings) and **😎 favourite opponent** (best record, min 3 meetings).

## Finance (private, view-key gated)

The club's expense Excel, rebuilt as three record types in one DynamoDB table — **expenses** (per month + slot line items with estimated vs actual cost and quantity), **memberships** (player × month × slot enrollment with Yes/No status), and **walk-ins** (per-session guests with fee, skill, and recruit verdict; negative fee = refund/adjustment).

**Settlement math**, per month and slot (the two timeslots are separate financial pools):

```
estimated_total   = Σ estimated_cost × estimated_qty     (what members paid against)
actual_total      = Σ actual_cost × actual_qty
extra_collected   = Σ walk-in fees that month + slot
player_count      = COUNT(memberships with status Yes)
cost_per_head     = estimated_total / player_count
residual_per_head = (estimated_total − actual_total + extra_collected) / player_count
```

**Privacy model.** Every finance route requires the view key, verified **server-side** (the frontend is a static page on an open API, so hiding a tab protects nothing). Deletes additionally require the club-wide confirmation code. The single exception is `GET /finance/walkins/public`: names, dates, and slots only — never fees, skill ratings, or recruit verdicts — and it 404s entirely unless the toggle in Finance settings is on.

**Insights** cross-reference finance with the match log: 👻 ghosts (enrolled members with zero recorded matches that month — the renewal chase-list), 💸 effective cost per match played, and 🎯 walk-in → member conversion.

## Scripts

One-time repairs and data tools (all use boto3 directly against DynamoDB; all idempotent):

| Script | Purpose |
|---|---|
| `backfill_progress_history.py` | Rebuild the entire progress-history table from current match data |
| `repair_ratings_after.py` | Full chronological rating replay (adaptive K + comeback bonuses), fixing `ratings_after` trails and current ratings |
| `clear_bogus_momentum.py` | List/inspect/clear a bad live-scoring point log, with a simulated before/after ranking diff before anything is written |
| `seed_finance_from_excel.py` | One-time import of the historical expense workbook, with an explicit player-name mapping and a dry-run link report |
| `backfill_member_ratings.py` | Reconstruct rating snapshots for tournaments created before the snapshot feature |
| `tag_july19_matches.py` | Tag the July 19 tournament's matches with tournament/stage/group ids |
| `rename_player_history.py` | Propagate a player rename into frozen match/tournament name snapshots |

## Deploying

Push to `main`. GitHub Actions packages the Lambdas, deploys the CloudFormation stack (passing `ConfirmationCode` and `FinanceViewKey` from repository secrets — both required, the deploy fails without them), forces a fresh API Gateway deployment, and syncs the frontend to S3.

## API routes

| Method | Path | Purpose |
|---|---|---|
| POST | `/register` | Register a new player |
| GET | `/players` | List all players |
| PUT / DELETE | `/players/{player_id}` | Rename / delete a player (delete needs confirmation code) |
| POST / GET | `/groups` | Create / list groups |
| GET / PUT / DELETE | `/groups/{group_id}` | Group detail / tournament defaults / delete |
| POST / DELETE | `/groups/{group_id}/players[/{player_id}]` | Add / remove members |
| POST | `/matches` | Record a match (manual score or live point log), updates Elo |
| GET | `/matches?...` | Game log, hall of fame, progress badges, progress history, head-to-head, profile bundles — selected by query params |
| PUT / DELETE | `/matches/{match_id}` | Correct / delete a match (triggers full rating recompute) |
| POST / GET | `/tournaments` | Create / list tournaments |
| GET | `/tournaments/{id}` | Detail + standings |
| POST | `/tournaments/{id}/group-score`, `/knockout-score` | Record fixture scores |
| GET | `/finance/summary`, `/expenses`, `/memberships`, `/walkins`, `/insights`, `/settings` | Finance reads (view key) |
| POST / PUT / DELETE | `/finance/{expenses,memberships,walkins}[/{id}]` | Finance writes (view key; deletes also need confirmation code) |
| GET | `/finance/walkins/public` | Names/dates/slots only, no key, only while enabled |

## Repo structure

```
NetWorth/
├── .github/workflows/deploy.yml     # CI: package + deploy + frontend sync
├── infrastructure/template.yaml     # CloudFormation: everything above
├── backend/lambdas/
│   ├── register_player/  players/  groups/
│   ├── matches/          # Elo, hall of fame, achievements, history
│   ├── tournaments/      # brackets, live scoring, momentum
│   ├── finance/          # ledger, settlement, insights
│   └── progress_scheduler/
├── frontend/index.html              # the entire UI
├── scripts/                         # repairs & data tools (see table)
└── README.md
```
