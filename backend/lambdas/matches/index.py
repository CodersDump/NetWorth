"""
NetWorth - matches Lambda (singles + doubles)

Routes:
    POST /matches  -> record a match, updates Elo ratings
    GET  /matches?group_id=X&player_id=Y  -> game log, optionally filtered

Body for POST /matches:
    {
      "match_type": "singles" | "doubles",
      "team_a": ["player_id1"] or ["player_id1", "player_id2"],
      "team_b": ["player_id1"] or ["player_id1", "player_id2"],
      "score_a": 21, "score_b": 15,
      "group_id": "optional",
      "point_log": ["A", "A", "B", "A", ...]   (optional, from live scoring)
    }

point_log is an ordered list of which team won each point, if the match was
recorded live via the point-by-point counter. When present, it's validated
against the final score and used to compute simple momentum stats (longest
scoring streak per team, biggest deficit the eventual winner overcame).

Elo approach for doubles: team rating = average of teammates' current
ratings. Expected score computed from team ratings. The resulting rating
delta is applied in full to each teammate individually (based on their own
current rating), so two players on a winning team both move, but a much
higher-rated player carried by a lower-rated partner still updates off
their own baseline.

Env vars:
    MATCHES_TABLE - DynamoDB table name for matches
    PLAYERS_TABLE - DynamoDB table name for players
"""
import json
import os
import uuid
import boto3
from datetime import datetime, timezone, date, timedelta

dynamodb = boto3.resource('dynamodb')
matches_table = dynamodb.Table(os.environ['MATCHES_TABLE'])
players_table = dynamodb.Table(os.environ['PLAYERS_TABLE'])
tournaments_table = dynamodb.Table(os.environ['TOURNAMENTS_TABLE'])
groups_table = dynamodb.Table(os.environ['GROUPS_TABLE'])
history_table = dynamodb.Table(os.environ['PROGRESS_HISTORY_TABLE'])

K_FACTOR = 32

# ---------- XP / levels / coins ----------
# XP is a participation reward: it only ever goes UP (win or lose), unlike
# Elo which moves both ways. It's accumulated in the same recompute loop as
# ratings - one pass, no separate calculation - so it stays consistent after
# any correction or reorder.
#
# XP per match, keyed by (stage, won). Regular matches have stage None.
# Tournament stages escalate: group < knockout < final. A win always adds a
# bonus on top of the "played" amount.
XP_PLAYED = {None: 10, 'group': 15, 'knockout': 25, 'third_place': 15, 'final': 40}
XP_WIN_BONUS = {None: 5, 'group': 8, 'knockout': 15, 'third_place': 8, 'final': 30}
XP_TOURNAMENT_WIN = 100          # one-off, awarded when a tournament is won

# Winners earn a little extra for a dominant result, scaled by point margin.
# Capped so a 21-3 thrashing doesn't dwarf the base award - it's a nudge,
# not the main event. Roughly: +1 XP per 3 points of margin, up to +7.
XP_MARGIN_PER_POINTS = 3         # 1 bonus XP per this many points of margin
XP_MARGIN_CAP = 7                # never more than this from margin alone

# Escalating curve: total XP needed to REACH level N is 5 * N^2. So level 10
# = 500, level 50 = 12,500, level 100 = 50,000, level 1000 = 5,000,000 -
# early levels are quick, high levels are a genuine long-term grind (chosen
# deliberately after seeing 50 matches logged in a single week).
XP_LEVEL_COEFF = 5

COINS_PER_LEVEL = 50             # granted each time a player gains a level


def level_from_xp(xp):
    """Inverse of xp = 5*N^2, floored: the highest level fully paid for by
    this much XP. Level starts at 1 (0 XP)."""
    if xp <= 0:
        return 1
    import math
    return max(1, int(math.isqrt(int(xp) // XP_LEVEL_COEFF)))


def xp_for_level(level):
    """Total XP needed to reach a given level - used for progress bars."""
    return XP_LEVEL_COEFF * level * level


def xp_for_match(stage, won, margin=0):
    """Base XP a single player earns for one match (before any event
    multiplier, which is applied at the point of accumulation). Winners get
    a small extra, capped, for the margin of victory - dominating earns a
    touch more than scraping through, without making blowouts everything."""
    played = XP_PLAYED.get(stage, XP_PLAYED[None])
    bonus = XP_WIN_BONUS.get(stage, XP_WIN_BONUS[None]) if won else 0
    margin_bonus = 0
    if won and margin > 0:
        margin_bonus = min(XP_MARGIN_CAP, margin // XP_MARGIN_PER_POINTS)
    return played + bonus + margin_bonus


# ---------- limited-time events (XP multipliers) ----------
# Events live in one reserved row of the matches table. Each has a name,
# start/end date (inclusive, YYYY-MM-DD), and an xp_multiplier (e.g. 2.0 for
# a Diwali double-XP weekend). A match's XP is multiplied by whatever event
# covers ITS date - not "now" - so a recompute always reproduces the same
# totals. Overlapping events take the highest multiplier.
_EVENTS_ROW_ID = '__events__'
_QUESTS_ROW_ID = '__quests__'
_APP_SETTINGS_ID = '__app_settings__'

def _scan_all(table, **kw):
    """Full-table scan that follows LastEvaluatedKey - a bare .scan() returns
    only the first 1 MB page (KNOWN_ISSUES #15)."""
    items, last = [], None
    while True:
        if last:
            kw['ExclusiveStartKey'] = last
        resp = table.scan(**kw)
        items.extend(resp.get('Items', []))
        last = resp.get('LastEvaluatedKey')
        if not last:
            return items

_PRIVATE_ID_KEYS = ('player_id', 'opponent_id', 'partner_id', 'top_partner_id')

def _load_private_ids():
    """player_ids currently flagged private - only when the feature flag is on,
    so every call site is a no-op (empty set) while the feature is dark."""
    settings = players_table.get_item(Key={'player_id': _APP_SETTINGS_ID}).get('Item') or {}
    if not bool(settings.get('privacy_mode_enabled', False)):
        return set()
    ids = set()
    for it in _scan_all(players_table, ProjectionExpression='player_id, privacy_private'):
        if it.get('privacy_private'):
            ids.add(it['player_id'])
    return ids

def _entry_is_private(x, private_ids):
    return isinstance(x, dict) and any(x.get(k) in private_ids for k in _PRIVATE_ID_KEYS)

def _scrub_private(obj, private_ids):
    """Recursively drop leaderboard/distribution entries belonging to a private
    player: a dict entry is dropped if any id-key names a private player; scalar
    id-lists have private ids removed. Structure and non-player data are kept.
    No-op when private_ids is empty (feature off / admin caller)."""
    if not private_ids:
        return obj
    if isinstance(obj, list):
        out = []
        for x in obj:
            if isinstance(x, str):
                if x not in private_ids:
                    out.append(x)
            elif _entry_is_private(x, private_ids):
                continue
            else:
                out.append(_scrub_private(x, private_ids))
        return out
    if isinstance(obj, dict):
        return {k: _scrub_private(v, private_ids) for k, v in obj.items()}
    return obj

# Weekly quest condition types. Each is a rule evaluated against a player's
# matches for the current week. The admin picks a type + target + reward.
# Detection reads existing match fields (winner, scores, ratings_after) - no
# new capture needed. Adding a new type here is the only code change a new
# kind of task requires.
QUEST_TYPES = {
    'win_count':   'Win {target} matches',
    'play_count':  'Play {target} matches',
    'win_margin':  'Win {target} matches by 10+ points',
    'win_deuce':   'Win {target} deuce matches (won by exactly 2 after 20)',
    'beat_higher': 'Beat a higher-rated opponent {target} times',
}


def _load_quests():
    item = matches_table.get_item(Key={'match_id': _QUESTS_ROW_ID}).get('Item') or {}
    return item.get('quests', [])


def _week_bounds_utc(now=None):
    """Monday 00:00 (inclusive) to next Monday (exclusive), as ISO date
    strings - the same week boundary the reorder lock and scheduler use."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    now = now or _dt.now(_tz.utc)
    monday = (now - _td(days=now.weekday())).strftime('%Y-%m-%d')
    next_monday = (now - _td(days=now.weekday()) + _td(days=7)).strftime('%Y-%m-%d')
    return monday, next_monday


def _evaluate_quest(quest, player_id, week_matches, player_rating_by_id):
    """Returns how many times the player has satisfied this quest's condition
    among the week's matches. Capped display against target happens in the
    caller."""
    qtype = quest.get('type')
    count = 0
    for m in week_matches:
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        in_a = player_id in team_a
        in_b = player_id in team_b
        if not (in_a or in_b):
            continue
        winner = m.get('winner')
        won = (winner == 'A' and in_a) or (winner == 'B' and in_b)
        try:
            margin = abs(int(m.get('score_a', 0)) - int(m.get('score_b', 0)))
            hi = max(int(m.get('score_a', 0)), int(m.get('score_b', 0)))
        except (TypeError, ValueError):
            margin, hi = 0, 0

        if qtype == 'play_count':
            count += 1
        elif qtype == 'win_count' and won:
            count += 1
        elif qtype == 'win_margin' and won and margin >= 10:
            count += 1
        elif qtype == 'win_deuce' and won and hi > 21 and margin == 2:
            count += 1
        elif qtype == 'beat_higher' and won:
            # Beat someone whose pre-match rating was higher than yours.
            after = m.get('ratings_after') or {}
            opp_ids = team_b if in_a else team_a
            my_after = after.get(player_id)
            opp_afters = [after.get(o) for o in opp_ids if after.get(o) is not None]
            # Fall back to current ratings if a match predates ratings_after.
            if my_after is None:
                my_after = player_rating_by_id.get(player_id, 1000)
            if not opp_afters:
                opp_afters = [player_rating_by_id.get(o, 1000) for o in opp_ids]
            if opp_afters and max(opp_afters) > my_after:
                count += 1
    return count


# ---------- seasons (derived, soft-reset; lifetime rating never resets) ----------
_SEASON_ROW_PREFIX = '__season__'

def _season_config():
    """Season definitions + soft-reset k live in the shared app-settings row
    (players table), managed via /app-settings. Returns (enabled, k, [seasons])
    with each season's window end resolved to the next season's start."""
    s = players_table.get_item(Key={'player_id': _APP_SETTINGS_ID}).get('Item') or {}
    enabled = bool(s.get('seasons_enabled', False))
    try:
        k = max(0.0, min(1.0, float(s.get('season_reset_k', 0.3))))
    except (TypeError, ValueError):
        k = 0.3
    defs = [d for d in (s.get('seasons') or []) if d.get('start_date')]
    defs.sort(key=lambda d: d['start_date'])
    resolved = []
    for i, d in enumerate(defs):
        end = d.get('end_date') or (defs[i + 1]['start_date'] if i + 1 < len(defs) else '9999-12-31')
        resolved.append({'id': d.get('id') or d['start_date'], 'name': d.get('name') or d['start_date'],
                         'start_date': d['start_date'][:10], 'end_date': end[:10]})
    return enabled, k, resolved

def _resolve_season(resolved, which):
    if not resolved:
        return None
    if not which or which == 'current':
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        for s in resolved:
            if s['start_date'] <= today < s['end_date']:
                return s
        return resolved[-1]
    for s in resolved:
        if s['id'] == which:
            return s
    return None

def _ensure_season_baseline(season, k, items):
    """Freeze, once, each player's lifetime rating as of the season start
    (from stored ratings_after) plus the soft-reset baseline 1000+(r-1000)*k.
    Frozen so edits to OLD matches don't move where a season started you; your
    in-season movement still recalculates from the baseline. Sentinel row."""
    row_id = _SEASON_ROW_PREFIX + season['id']
    existing = matches_table.get_item(Key={'match_id': row_id}).get('Item')
    if existing and existing.get('baseline'):
        return existing
    sd = season['start_date']
    per = {}
    for m in items:
        d = m.get('date') or ''
        if not d:
            continue
        ra = m.get('ratings_after') or {}
        for pid in (m.get('team_a') or []) + (m.get('team_b') or []):
            per.setdefault(pid, []).append((d, ra.get(pid)))
    start_lifetime, baseline = {}, {}
    for pid, rows in per.items():
        rows.sort(key=lambda r: r[0])
        pre = [r[1] for r in rows if r[0][:10] < sd and r[1] is not None]
        r0 = int(pre[-1]) if pre else 1000
        start_lifetime[pid] = r0
        baseline[pid] = int(round(1000 + (r0 - 1000) * k))
    row = {'match_id': row_id, 'season_id': season['id'], 'k': str(k),
           'start_lifetime': start_lifetime, 'baseline': baseline,
           'frozen_at': datetime.now(timezone.utc).isoformat()}
    matches_table.put_item(Item=row)
    return row

def compute_season_leaderboard(season, items, k, min_games=5):
    """Derived climb board: everyone starts the season at a soft-reset baseline
    (frozen), then moves by their lifetime rating change across the window. No
    Elo replay - reads each match's stored ratings_after."""
    row = _ensure_season_baseline(season, k, items)
    baseline = row.get('baseline') or {}
    start_lifetime = row.get('start_lifetime') or {}
    sd, ed = season['start_date'], season['end_date']
    per = {}
    for m in items:
        d = m.get('date') or ''
        if not d:
            continue
        ra = m.get('ratings_after') or {}
        w = m.get('winner')
        for pid in (m.get('team_a') or []):
            per.setdefault(pid, []).append((d, ra.get(pid), w == 'A'))
        for pid in (m.get('team_b') or []):
            per.setdefault(pid, []).append((d, ra.get(pid), w == 'B'))
    leaders = []
    for pid, rows in per.items():
        rows.sort(key=lambda r: r[0])
        in_window = [r for r in rows if sd <= r[0][:10] < ed]
        if len(in_window) < min_games:
            continue
        if pid in start_lifetime:
            start_r = int(start_lifetime[pid])
        else:
            pre = [r[1] for r in rows if r[0][:10] < sd and r[1] is not None]
            start_r = int(pre[-1]) if pre else 1000
        upto = [r[1] for r in rows if r[0][:10] < ed and r[1] is not None]
        end_r = int(upto[-1]) if upto else start_r
        base = int(baseline.get(pid, round(1000 + (start_r - 1000) * k)))
        score = base + (end_r - start_r)
        wins = sum(1 for r in in_window if r[2])
        leaders.append({'player_id': pid, 'games': len(in_window), 'wins': wins,
                        'losses': len(in_window) - wins, 'season_start': base,
                        'season_score': score, 'delta': score - base})
    leaders.sort(key=lambda x: (-x['season_score'], -x['games']))
    for i, l in enumerate(leaders):
        l['rank'] = i + 1
    return {'season': season, 'leaders': leaders, 'min_games': min_games}


def _season_board_leaders(season, items, k):
    """Leaders for a season: sealed (frozen) if it has ended, else live."""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    row_id = _SEASON_ROW_PREFIX + season['id']
    if season['end_date'] <= today:
        row = matches_table.get_item(Key={'match_id': row_id}).get('Item') or {}
        if row.get('sealed_leaders') is not None:
            return row['sealed_leaders'], True
        board = compute_season_leaderboard(season, items, k)
        try:
            matches_table.update_item(Key={'match_id': row_id},
                UpdateExpression='SET sealed_leaders = :l, sealed_at = :t',
                ExpressionAttributeValues={':l': board['leaders'], ':t': today})
        except Exception:
            pass
        return board['leaders'], True
    return compute_season_leaderboard(season, items, k)['leaders'], False

def _season_badges_for(player_id, leaders):
    """A player's standing + earned badges on one season board."""
    me = next((l for l in leaders if l.get('player_id') == player_id), None)
    if not me:
        return None
    badges = []
    if me.get('rank') in (1, 2, 3):
        badges.append({'kind': 'podium', 'rank': me['rank']})
    if leaders:
        top_improved = max(leaders, key=lambda l: l.get('delta', -10**9))
        top_iron = max(leaders, key=lambda l: l.get('games', 0))
        champ_id = leaders[0].get('player_id')
        # Most-improved only if it isn't the champion - otherwise it's redundant
        # (in a uniform-baseline season like Season 0 the winner is always the
        # biggest climber, so the two badges would always coincide).
        if top_improved.get('player_id') == player_id and player_id != champ_id and me.get('delta', 0) > 0:
            badges.append({'kind': 'most_improved'})
        if top_iron.get('player_id') == player_id:
            badges.append({'kind': 'iron'})
    badges.append({'kind': 'participation'})
    return {'rank': me.get('rank'), 'season_score': me.get('season_score'),
            'delta': me.get('delta'), 'games': me.get('games'), 'badges': badges}

def compute_player_season_summary(player_id, items):
    """Per-season standing + badges for one player, across started seasons."""
    enabled, k, resolved = _season_config()
    if not enabled:
        return {'enabled': False, 'seasons': []}
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    out = []
    for s in resolved:
        if s['start_date'] > today:
            continue
        leaders, sealed = _season_board_leaders(s, items, k)
        info = _season_badges_for(player_id, leaders)
        if not info:
            continue
        info['season'] = s
        info['sealed'] = sealed
        out.append(info)
    out.reverse()
    return {'enabled': True, 'seasons': out}


def _quest_period(quest):
    """(bounds, claim_prefix, label) for a quest by scope. Season-scoped quests
    use the current season's window + id (so they reset at rollover); weekly
    quests use the current week. Returns (None, None, None) for a season quest
    when no season is active."""
    if quest.get('scope') == 'season':
        enabled, _k, resolved = _season_config()
        cur = _resolve_season(resolved, 'current') if enabled else None
        if not cur:
            return None, None, None
        return (cur['start_date'], cur['end_date']), 'season:' + cur['id'], cur['name']
    monday, next_monday = _week_bounds_utc()
    return (monday, next_monday), monday, 'This week'


def list_quests(event):
    """Returns this week's quests with the caller's progress and claim state.
    Public-ish: requires a linked player to show progress, but the quest
    definitions themselves are visible to anyone logged in."""
    claims = _caller_claims(event)
    pid = claims.get('custom:player_id')
    quests = _load_quests()

    all_matches = [m for m in _scan_all(matches_table)
                   if m.get('match_id') not in (_EVENTS_ROW_ID, _QUESTS_ROW_ID)]
    monday, _nm = _week_bounds_utc()
    player = players_table.get_item(Key={'player_id': pid}).get('Item') if pid else None
    rating_by_id = {}  # lazy - only needed for beat_higher fallback
    claimed = (player or {}).get('quest_claims', {}) if player else {}

    out = []
    for q in quests:
        bounds, prefix, period = _quest_period(q)
        if bounds is None:
            continue  # season-scoped quest with no active season - hide it
        lo, hi = bounds
        q_matches = [m for m in all_matches if lo <= (m.get('date') or '')[:10] < hi]
        target = int(q.get('target', 1))
        progress = _evaluate_quest(q, pid, q_matches, rating_by_id) if pid else 0
        done = progress >= target
        already = bool(claimed.get(f"{prefix}:{q.get('quest_id')}"))
        out.append({
            'quest_id': q.get('quest_id'),
            'type': q.get('type'),
            'label': QUEST_TYPES.get(q.get('type'), '').format(target=target),
            'target': target,
            'reward_xp': int(q.get('reward_xp', 0)),
            'reward_coins': int(q.get('reward_coins', 0)),
            'reward_cosmetic_id': q.get('reward_cosmetic_id') or None,
            'scope': q.get('scope', 'weekly'),
            'period': period,
            'progress': min(progress, target),
            'complete': done,
            'claimed': already,
        })
    return _response(200, {'quests': out, 'week_start': monday})


def save_quest(event):
    claims = _caller_claims(event)
    if not _is_super_admin(claims):
        return _response(403, {'error': 'only a SuperAdmin can manage quests'})
    body = json.loads(event.get('body') or '{}')
    qtype = body.get('type')
    if qtype not in QUEST_TYPES:
        return _response(400, {'error': f'type must be one of {list(QUEST_TYPES)}'})
    try:
        target = int(body.get('target'))
    except (TypeError, ValueError):
        return _response(400, {'error': 'target must be a whole number'})
    if target < 1:
        return _response(400, {'error': 'target must be at least 1'})

    quests = _load_quests()
    qid = body.get('quest_id') or str(uuid.uuid4())
    row = {
        'quest_id': qid, 'type': qtype, 'target': target,
        'reward_xp': int(body.get('reward_xp', 0) or 0),
        'reward_coins': int(body.get('reward_coins', 0) or 0),
        'reward_cosmetic_id': body.get('reward_cosmetic_id') or None,
        'scope': body.get('scope') if body.get('scope') in ('weekly', 'season') else 'weekly',
    }
    quests = [q for q in quests if q.get('quest_id') != qid]
    quests.append(row)
    matches_table.put_item(Item={'match_id': _QUESTS_ROW_ID, 'quests': quests})
    return _response(200, {'quest': row})


def delete_quest(event):
    claims = _caller_claims(event)
    if not _is_super_admin(claims):
        return _response(403, {'error': 'only a SuperAdmin can manage quests'})
    body = json.loads(event.get('body') or '{}')
    qid = body.get('quest_id')
    if not qid:
        return _response(400, {'error': 'quest_id is required'})
    quests = [q for q in _load_quests() if q.get('quest_id') != qid]
    matches_table.put_item(Item={'match_id': _QUESTS_ROW_ID, 'quests': quests})
    return _response(200, {'ok': True})


def claim_quest(event):
    """Player claims a completed quest's reward. Verified server-side against
    the actual match history - the client can't fake completion. Idempotent
    per week: claiming twice is refused."""
    claims = _caller_claims(event)
    pid = claims.get('custom:player_id')
    if not pid:
        return _response(403, {'error': 'link a profile first'})
    body = json.loads(event.get('body') or '{}')
    qid = body.get('quest_id')

    quest = next((q for q in _load_quests() if q.get('quest_id') == qid), None)
    if not quest:
        return _response(404, {'error': 'quest not found'})

    bounds, prefix, _period = _quest_period(quest)
    if bounds is None:
        return _response(400, {'error': 'this season quest is not active right now'})
    lo, hi = bounds
    q_matches = [m for m in _scan_all(matches_table)
                 if m.get('match_id') not in (_EVENTS_ROW_ID, _QUESTS_ROW_ID)
                 and lo <= (m.get('date') or '')[:10] < hi]
    progress = _evaluate_quest(quest, pid, q_matches, {})
    if progress < int(quest.get('target', 1)):
        return _response(400, {'error': 'quest not complete yet'})

    player = players_table.get_item(Key={'player_id': pid}).get('Item') or {}
    claimed = player.get('quest_claims') or {}
    claim_key = f"{prefix}:{qid}"
    if claimed.get(claim_key):
        return _response(400, {'error': 'already claimed'})

    # Grant rewards. XP is added on top of match-earned XP (and survives
    # recompute because we bump a separate quest_xp field the recompute adds
    # back in). Coins go straight to balance. A cosmetic is added to owned.
    reward_xp = int(quest.get('reward_xp', 0) or 0)
    reward_coins = int(quest.get('reward_coins', 0) or 0)
    cosmetic = quest.get('reward_cosmetic_id')

    claimed[claim_key] = True
    new_quest_xp = int(player.get('quest_xp', 0) or 0) + reward_xp
    new_quest_coins = int(player.get('quest_coins', 0) or 0) + reward_coins
    new_coins = int(player.get('coins', 0) or 0) + reward_coins
    owned = player.get('owned_items') or {}
    if cosmetic:
        owned[cosmetic] = True

    players_table.update_item(
        Key={'player_id': pid},
        UpdateExpression='SET quest_claims = :qc, quest_xp = :qx, quest_coins = :qco, coins = :c, owned_items = :o',
        ExpressionAttributeValues={':qc': claimed, ':qx': new_quest_xp, ':qco': new_quest_coins,
                                   ':c': new_coins, ':o': owned}
    )
    return _response(200, {'ok': True, 'reward_xp': reward_xp, 'reward_coins': reward_coins,
                           'reward_cosmetic': bool(cosmetic), 'coins': new_coins})


def _load_events():
    item = matches_table.get_item(Key={'match_id': _EVENTS_ROW_ID}).get('Item') or {}
    return item.get('events', [])

def event_multiplier_for_date(date_str, events=None):
    """The XP multiplier active on a given match date (default 1.0). Pass a
    preloaded events list during recompute to avoid a lookup per match."""
    if events is None:
        events = _load_events()
    if not date_str:
        return 1.0
    day = date_str[:10]
    best = 1.0
    for ev in events:
        start = ev.get('start_date', '')
        end = ev.get('end_date', '')
        if start <= day <= end:
            try:
                best = max(best, float(ev.get('xp_multiplier', 1.0)))
            except (TypeError, ValueError):
                pass
    return best



def display_name(player_item, fallback=None):
    """Single source of truth for name formatting: 'Nickname (Real Name)'
    when a nickname is set, plain name otherwise. Used everywhere a player
    record needs to become display text, so nickname support can't drift
    out of sync in one function while another still shows a bare name."""
    if not player_item:
        return fallback
    name = player_item.get('name', fallback)
    nickname = player_item.get('nickname')
    return f"{nickname} ({name})" if nickname else name


COMEBACK_BONUS_THRESHOLD = 5   # minimum deficit overcome to count as a genuine comeback
COMEBACK_BONUS_PER_POINT = 0.3
COMEBACK_BONUS_CAP = 8


def compute_comeback_bonus(momentum):
    """Extra rating-point bonus for the winning side, on top of the
    standard Elo delta, when they overcame a genuine mid-game deficit.
    Only ever non-zero for matches with a point-by-point log, since only
    that data can detect a comeback trajectory at all - a manually-entered
    final score has no way to know if a match was ever close."""
    if not momentum:
        return 0
    deficit = float(momentum.get('winner_overcame_deficit', 0))
    if deficit < COMEBACK_BONUS_THRESHOLD:
        return 0
    return min(deficit * COMEBACK_BONUS_PER_POINT, COMEBACK_BONUS_CAP)


def _is_valid_completed_game(score_a, score_b, target):
    """
    BWF-style badminton scoring: first to `target` points wins, but must lead
    by 2 (deuce continues past target); hard cap at target+9 (e.g. 21 -> 30),
    where reaching the cap wins outright regardless of margin.
    """
    cap = target + 9
    hi, lo = max(score_a, score_b), min(score_a, score_b)
    if hi > cap or lo > cap:
        return False
    if hi == cap:
        return True
    if hi >= target and (hi - lo) >= 2:
        return True
    return False


def _caller_claims(event):
    """Claims API Gateway's Cognito Authorizer attaches to the request.
    Only present on the isolated /record-match and /profile-secure routes."""
    return (event.get('requestContext') or {}).get('authorizer', {}).get('claims') or {}


def _is_super_admin(claims):
    groups = (claims.get('cognito:groups') or '').split(',')
    return 'SuperAdmin' in groups


def _caller_may_edit_match(claims, match):
    """Who may directly edit/delete a match (PUT/DELETE /matches/{id}):
    SuperAdmin, always; otherwise the caller's linked player must be
    owner/admin of the match's OWN group - the exact same bar players
    lambda's OWNER_DECIDABLE_TYPES already applies to match_edit/
    match_delete REQUESTS, so going direct isn't a lower bar than going
    through request+approve (it's the same approval, minus the detour). A
    match with no group_id (ungrouped/one-off) stays SuperAdmin-only, same
    as an ungrouped request today.

    This replaces what used to be the ONLY gate on these two routes: a
    shared CONFIRMATION_CODE, checked with no Cognito identity at all
    (the routes were AuthorizationType: NONE - literally open to anyone on
    the internet who knew or guessed the code). That code added real
    friction for the person actually meant to use it (SuperAdmin had to
    know and type a secret every time) while adding surprisingly little
    security - the API is public either way. Cognito + a real per-match
    ownership check is strictly better on both counts: real auth instead of
    a shared secret, and no code to type. (Owner-asked 2026-08-20: "why are
    we still needing the code to delete things ... for the match it should
    be fine right" - it wasn't the deletion itself that needed rethinking,
    it was this route having no actual authorization underneath the code.)"""
    if _is_super_admin(claims):
        return True
    gid = match.get('group_id')
    pid = claims.get('custom:player_id')
    if not gid or not pid or not groups_table:
        return False
    group = groups_table.get_item(Key={'group_id': gid}).get('Item') or {}
    return group.get('roles', {}).get(pid) in ('owner', 'admin')


def _can_view_profile(claims, target_player_id):
    """SuperAdmin sees everyone. Anyone can view their own profile. A
    logged-in member can view another player's profile only if they share
    at least one group - matches the spec: 'if I'm part of 3 groups, I
    can see all members across those 3 groups'."""
    if _is_super_admin(claims):
        return True
    caller_player_id = claims.get('custom:player_id')
    if not caller_player_id:
        return False
    if caller_player_id == target_player_id:
        return True
    groups = groups_table.scan().get('Items', [])
    for g in groups:
        members = g.get('member_ids', [])
        if caller_player_id in members and target_player_id in members:
            return True
    return False


def _requires_linked_member(claims):
    """Signing up is not the same as being a member. Cognito self-signup is
    open to anyone with a working inbox - including disposable ones - so a
    bare session proves only that someone controls an email address. It
    proves nothing about belonging to this club.

    The real membership signal is a custom:player_id that resolves to a
    LIVE player row, because that is only ever set by an approved claim or
    by an admin. Anything that creates or mutates shared data checks this,
    not merely "is logged in".

    Returns an error response, or None when the caller is a real member.
    """
    if _is_super_admin(claims):
        return None
    player_id = claims.get('custom:player_id')
    if not player_id:
        return _response(403, {'error': 'your account is not linked to a player yet - claim your profile first'})
    if not players_table.get_item(Key={'player_id': player_id}).get('Item'):
        return _response(403, {'error': 'the player linked to your account no longer exists - claim your profile again'})
    return None


def record_match_enforced(event):
    claims = _caller_claims(event)
    if not claims:
        return _response(403, {'error': 'log in to record a match'})
    not_member = _requires_linked_member(claims)
    if not_member:
        return not_member
    return record_match(event)


def profile_view_enforced(event):
    """Entry point for the isolated /profile-secure/{proxy+} catch-all.
    Determines which player's profile is being requested from whichever
    query param is present, checks _can_view_profile, then delegates to
    the existing list_matches() unchanged - same computation logic either
    way, just gated entry."""
    claims = _caller_claims(event)
    if not claims:
        return _response(403, {'error': 'log in to view profiles'})

    # SuperAdmin: full see-all access. Delegates straight to list_matches,
    # which leaves private players in when the caller is an admin.
    if _is_super_admin(claims):
        return list_matches(event)

    params = event.get('queryStringParameters') or {}
    target = (params.get('profile_bundle_for') or params.get('player_id')
              or params.get('partnerships_for') or params.get('radar_for')
              or params.get('head_to_head') or params.get('with_partner'))
    if not target:
        return _response(400, {'error': 'no player specified'})
    if not _can_view_profile(claims, target):
        return _response(403, {'error': 'you can only view profiles of players who share a group with you'})

    return list_matches(event)


def handler(event, context):
    try:
        method = event.get('httpMethod')
        match_id = (event.get('pathParameters') or {}).get('match_id')

        # Epic 7: the only way to record a match now requires a real
        # Cognito login, via this isolated top-level route (same platform
        # reason as every other isolated route this session - a specific
        # path can't sit alongside {proxy+}/ANY at the same parent).
        if event.get('resource') == '/record-match' and method == 'POST':
            return record_match_enforced(event)

        # Reorder a single day's matches by swapping their timestamps, then
        # replay ratings. SuperAdmin only - it rewrites history.
        if event.get('resource') == '/reorder-matches' and method == 'POST':
            return reorder_matches(event)

        # SuperAdmin-triggered full recompute of ratings + XP + coins across
        # all history. Handy after a manual data fix, and the way to backfill
        # XP/levels onto players who predate the XP system.
        if event.get('resource') == '/recompute' and method == 'POST':
            return recompute_now(event)
        if event.get('resource') == '/events' and method == 'GET':
            return list_events(event)
        if event.get('resource') == '/events' and method == 'POST':
            return save_event(event)
        if event.get('resource') == '/events' and method == 'DELETE':
            return delete_event(event)
        if event.get('resource') == '/quests' and method == 'GET':
            return list_quests(event)
        if event.get('resource') == '/quests' and method == 'POST':
            return save_quest(event)
        if event.get('resource') == '/quests' and method == 'DELETE':
            return delete_quest(event)
        if event.get('resource') == '/quest-claim' and method == 'POST':
            return claim_quest(event)

        # Epic 7 extension: profile viewing is now genuinely restricted -
        # guests can't view any profile at all; logged-in members can only
        # view profiles of players sharing at least one group with them
        # (or their own); SuperAdmin sees everyone. Reached via the
        # isolated /profile-secure/{proxy+} catch-all (same reasoning as
        # finance-secure - one route covers every profile-related query
        # param without needing a separate resource tree per param).
        if event.get('resource', '').startswith('/profile-secure'):
            return profile_view_enforced(event)

        if method == 'POST':
            # The original anonymous path - genuinely closed now, not left
            # as a guest fallback, since Epic 7 asked for real restriction
            # here rather than an additive stronger option.
            return _response(403, {'error': 'log in to record a match - use /record-match'})
        elif method == 'GET':
            return list_matches(event)
        elif method == 'PUT' and match_id:
            return update_match(match_id, event)
        elif method == 'DELETE' and match_id:
            return delete_match(match_id, event)
        return _response(404, {'error': 'not found'})
    except Exception as e:
        return _response(500, {'error': str(e)})


def list_events(event):
    """Public read - the frontend shows an active-event banner to everyone.
    Returns events sorted by start date."""
    events = _load_events()
    events.sort(key=lambda e: e.get('start_date', ''))
    return _response(200, {'events': events})


def save_event(event):
    """SuperAdmin creates or updates an event (upsert by event_id)."""
    claims = _caller_claims(event)
    if not _is_super_admin(claims):
        return _response(403, {'error': 'only a SuperAdmin can manage events'})
    body = json.loads(event.get('body') or '{}')
    name = (body.get('name') or '').strip()
    start = (body.get('start_date') or '').strip()[:10]
    end = (body.get('end_date') or '').strip()[:10]
    if not name or not start or not end:
        return _response(400, {'error': 'name, start_date and end_date are required'})
    if end < start:
        return _response(400, {'error': 'end_date cannot be before start_date'})
    try:
        mult = float(body.get('xp_multiplier', 1.0))
    except (TypeError, ValueError):
        return _response(400, {'error': 'xp_multiplier must be a number'})
    if mult < 1.0 or mult > 10.0:
        return _response(400, {'error': 'xp_multiplier must be between 1 and 10'})

    events = _load_events()
    eid = body.get('event_id') or str(uuid.uuid4())
    row = {'event_id': eid, 'name': name, 'start_date': start, 'end_date': end,
           'xp_multiplier': str(mult)}  # stored as string; DynamoDB has no float
    events = [e for e in events if e.get('event_id') != eid]
    events.append(row)
    matches_table.put_item(Item={'match_id': _EVENTS_ROW_ID, 'events': events})
    return _response(200, {'event': row})


def delete_event(event):
    claims = _caller_claims(event)
    if not _is_super_admin(claims):
        return _response(403, {'error': 'only a SuperAdmin can manage events'})
    body = json.loads(event.get('body') or '{}')
    eid = body.get('event_id')
    if not eid:
        return _response(400, {'error': 'event_id is required'})
    events = [e for e in _load_events() if e.get('event_id') != eid]
    matches_table.put_item(Item={'match_id': _EVENTS_ROW_ID, 'events': events})
    return _response(200, {'ok': True})


def recompute_now(event):
    """SuperAdmin-only: replay every match to rebuild ratings, XP, levels
    and coin balances from scratch. Idempotent - safe to run any time."""
    claims = _caller_claims(event)
    if not _is_super_admin(claims):
        return _response(403, {'error': 'only a SuperAdmin can trigger a recompute'})
    recompute_all_ratings()
    return _response(200, {'ok': True, 'note': 'Ratings, XP, levels and coins recomputed from full match history.'})


def reorder_matches(event):
    """Reorders a set of matches by reassigning their timestamps.

    The client sends match_ids in the desired order. We take the set of
    timestamps those matches currently hold, sort them, and hand them back
    out in the new order - so match now-first gets the earliest of the
    day's times, and so on. This keeps every timestamp within the same day
    (they're just permuted among that day's matches), then replays every
    rating from scratch in the corrected order.

    Only a SuperAdmin may do this: reordering silently rewrites every
    player's rating from the earliest changed match onward.
    """
    claims = _caller_claims(event)
    if not _is_super_admin(claims):
        return _response(403, {'error': 'only a SuperAdmin can reorder matches'})

    body = json.loads(event.get('body') or '{}')
    ordered_ids = body.get('match_ids') or []
    if len(ordered_ids) < 2:
        return _response(400, {'error': 'need at least two matches to reorder'})

    # Fetch exactly these matches.
    found = {}
    for mid in ordered_ids:
        item = matches_table.get_item(Key={'match_id': mid}).get('Item')
        if not item:
            return _response(404, {'error': f'match {mid} not found'})
        found[mid] = item

    # Guard: they must all be the same calendar day. Reordering across days
    # is almost never intended and is where months of ratings get nuked.
    days = {(found[mid].get('date') or '')[:10] for mid in ordered_ids}
    if len(days) > 1:
        return _response(400, {'error': 'all matches in a reorder must be from the same day'})

    # A day is reorderable for the whole current week (Monday-start). Once
    # the week has passed the matches are settled - this stops history being
    # quietly rewritten later and lines up with the weekly scheduler, which
    # stamps closed-week matches as approved. Belt and braces: if any match
    # in the set is already flagged approved, it's settled regardless of the
    # date maths, so an explicitly-approved match can never be reordered.
    if any(found[mid].get('approved') for mid in ordered_ids):
        return _response(403, {'error': 'these matches have been approved for a closed week and can no longer be reordered'})
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    day = next(iter(days))
    now = _dt.now(_tz.utc)
    week_start = (now - _td(days=now.weekday())).strftime('%Y-%m-%d')  # Monday
    if day and day < week_start:
        return _response(403, {'error': 'this week has closed - these matches are settled and can no longer be reordered'})

    # The pool of timestamps to redistribute, earliest first.
    timestamps = sorted(found[mid].get('date') for mid in ordered_ids)

    # Assign the earliest time to the match the admin put first, etc.
    for new_time, mid in zip(timestamps, ordered_ids):
        if found[mid].get('date') != new_time:
            matches_table.update_item(
                Key={'match_id': mid},
                UpdateExpression='SET #d = :d',
                ExpressionAttributeNames={'#d': 'date'},
                ExpressionAttributeValues={':d': new_time}
            )

    recompute_all_ratings()
    return _response(200, {'reordered': len(ordered_ids)})


def record_match(event):
    body = json.loads(event.get('body') or '{}')
    match_type = body.get('match_type', 'singles')
    team_a = body.get('team_a') or []
    team_b = body.get('team_b') or []
    score_a = body.get('score_a')
    score_b = body.get('score_b')
    group_id = body.get('group_id')
    point_log = body.get('point_log')
    points_to_win = body.get('points_to_win', 21)

    if match_type not in ('singles', 'doubles'):
        return _response(400, {'error': 'match_type must be singles or doubles'})
    expected_size = 1 if match_type == 'singles' else 2
    if len(team_a) != expected_size or len(team_b) != expected_size:
        return _response(400, {'error': f'{match_type} requires {expected_size} player(s) per team'})
    if score_a is None or score_b is None:
        return _response(400, {'error': 'score_a and score_b are required'})
    if set(team_a) & set(team_b):
        return _response(400, {'error': 'a player cannot be on both teams'})
    if not _is_valid_completed_game(int(score_a), int(score_b), int(points_to_win)):
        cap = int(points_to_win) + 9
        return _response(400, {
            'error': f'invalid final score: game must be won by 2 at {points_to_win}+ points, or reach the hard cap of {cap}'
        })

    if point_log is not None:
        if not isinstance(point_log, list) or any(p not in ('A', 'B') for p in point_log):
            return _response(400, {'error': 'point_log must be a list of "A"/"B" entries'})
        log_a = sum(1 for p in point_log if p == 'A')
        log_b = sum(1 for p in point_log if p == 'B')
        if log_a != int(score_a) or log_b != int(score_b):
            return _response(400, {'error': 'point_log does not match score_a/score_b totals'})

    item = _play_and_log(match_type, team_a, team_b, int(score_a), int(score_b), group_id, None, None,
                          point_log, int(points_to_win))
    if item is None:
        return _response(404, {'error': 'one or more players not found'})
    return _response(200, item)


def update_match(match_id, event):
    """Fix a mis-entered score on an already-recorded standalone match.
    Requires SuperAdmin, or owner/admin of the match's own group (see
    _caller_may_edit_match - replaces the old shared-code check now that
    this route is Cognito-gated). Since Elo is path-dependent, changing a
    score doesn't just affect this one match's own rating delta - it can
    shift everyone who played after it too - so every edit triggers a full
    recompute of every player's rating from scratch, replaying the
    corrected history in order."""
    body = json.loads(event.get('body') or '{}')
    existing = matches_table.get_item(Key={'match_id': match_id}).get('Item')
    if not existing:
        return _response(404, {'error': 'match not found'})
    if not _caller_may_edit_match(_caller_claims(event), existing):
        return _response(403, {'error': 'not authorized to edit this match'})

    new_score_a = body.get('score_a')
    new_score_b = body.get('score_b')
    if new_score_a is None or new_score_b is None:
        return _response(400, {'error': 'score_a and score_b are required'})
    new_score_a, new_score_b = int(new_score_a), int(new_score_b)
    if new_score_a == new_score_b:
        return _response(400, {'error': 'scores cannot be tied'})

    new_winner = 'A' if new_score_a > new_score_b else 'B'

    # Optionally change the players too (not just the score). Teams keep the
    # match's original size (singles=1, doubles=2). Validated exactly like a
    # new match, and every valid player must exist. Omitting teams leaves the
    # rosters untouched (score-only edit, the original behaviour).
    set_parts = ['score_a = :sa', 'score_b = :sb', 'winner = :w']
    vals = {':sa': new_score_a, ':sb': new_score_b, ':w': new_winner}
    if body.get('team_a') is not None or body.get('team_b') is not None:
        team_a = body.get('team_a') or existing.get('team_a') or []
        team_b = body.get('team_b') or existing.get('team_b') or []
        size = len(existing.get('team_a') or []) or 1
        if len(team_a) != size or len(team_b) != size:
            return _response(400, {'error': f'this match needs {size} player(s) per team'})
        if set(team_a) & set(team_b):
            return _response(400, {'error': 'a player cannot be on both teams'})
        # every player must exist
        for pid in list(team_a) + list(team_b):
            if not players_table.get_item(Key={'player_id': pid}).get('Item'):
                return _response(404, {'error': f'player not found: {pid}'})
        set_parts += ['team_a = :ta', 'team_b = :tb']
        vals[':ta'] = team_a
        vals[':tb'] = team_b

    matches_table.update_item(
        Key={'match_id': match_id},
        UpdateExpression='SET ' + ', '.join(set_parts),
        ExpressionAttributeValues=vals
    )

    recompute_all_ratings()

    updated = matches_table.get_item(Key={'match_id': match_id}).get('Item')
    return _response(200, {'match': updated, 'note': 'All player ratings were recomputed from the corrected match history.'})


def delete_match(match_id, event):
    """Permanently delete a mis-recorded match - e.g. the wrong player was
    selected entirely and a corrected match was recorded separately.
    Requires SuperAdmin, or owner/admin of the match's own group (see
    _caller_may_edit_match). Since Elo is path-dependent, deleting a match
    doesn't just undo its own rating delta - it can shift every match that
    happened after it too - so deletion triggers the same full recompute
    as a score correction."""
    existing = matches_table.get_item(Key={'match_id': match_id}).get('Item')
    if not existing:
        return _response(404, {'error': 'match not found'})
    if not _caller_may_edit_match(_caller_claims(event), existing):
        return _response(403, {'error': 'not authorized to delete this match'})

    matches_table.delete_item(Key={'match_id': match_id})
    recompute_all_ratings()
    return _response(200, {'deleted': True, 'match_id': match_id,
                            'note': 'All player ratings were recomputed from the remaining match history.'})


def recompute_all_ratings():
    """Elo is path-dependent - each match's rating change depends on the
    ratings at that exact moment, which depend on everything before it.
    After correcting a match, the only fully correct fix is to reset
    everyone to 1000 and replay every match in chronological order,
    recomputing from scratch - including replaying each pairing's K-factor
    exactly as it would have been at that point in time."""
    players = _scan_all(players_table)
    current_ratings = {p['player_id']: 1000.0 for p in players}
    # XP is rebuilt from scratch alongside ratings so a correction/reorder
    # keeps it consistent. It only accumulates (never subtracts), keyed by
    # player, replaying every match's award in order.
    xp_totals = {p['player_id']: 0 for p in players}
    game_counts = {p['player_id']: 0 for p in players}  # matches actually played
    pairing_counts = {}  # frozenset({p1,p2}) -> matches played together so far

    matches = _scan_all(matches_table)
    # The reserved events row lives in this table but isn't a match.
    matches = [m for m in matches if m.get('match_id') not in (_EVENTS_ROW_ID, _QUESTS_ROW_ID)]
    matches.sort(key=lambda m: m.get('date', ''))
    # Load events once for the whole replay rather than per match.
    _events = _load_events()

    for m in matches:
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        score_a = m.get('score_a')
        score_b = m.get('score_b')
        if not team_a or not team_b or score_a is None or score_b is None:
            continue

        score_a, score_b = float(score_a), float(score_b)
        rating_a_avg = sum(current_ratings.get(pid, 1000.0) for pid in team_a) / len(team_a)
        rating_b_avg = sum(current_ratings.get(pid, 1000.0) for pid in team_b) / len(team_b)

        actual_a = 1.0 if score_a > score_b else (0.0 if score_a < score_b else 0.5)
        actual_b = 1.0 - actual_a
        expected_a = 1 / (1 + 10 ** ((rating_b_avg - rating_a_avg) / 400))
        expected_b = 1 - expected_a

        if m.get('match_type') == 'doubles':
            k_a = compute_adaptive_k(pairing_counts.get(frozenset(team_a), 0)) if len(team_a) == 2 else K_FACTOR
            k_b = compute_adaptive_k(pairing_counts.get(frozenset(team_b), 0)) if len(team_b) == 2 else K_FACTOR
            if len(team_a) == 2:
                key_a = frozenset(team_a)
                pairing_counts[key_a] = pairing_counts.get(key_a, 0) + 1
            if len(team_b) == 2:
                key_b = frozenset(team_b)
                pairing_counts[key_b] = pairing_counts.get(key_b, 0) + 1
        else:
            k_a = k_b = K_FACTOR

        delta_a = k_a * (actual_a - expected_a)
        delta_b = k_b * (actual_b - expected_b)

        winner = m.get('winner')
        momentum = m.get('momentum')
        if momentum:
            bonus = compute_comeback_bonus(momentum)
            if winner == 'A':
                delta_a += bonus
            elif winner == 'B':
                delta_b += bonus

        for pid in team_a:
            current_ratings[pid] = current_ratings.get(pid, 1000.0) + delta_a
            game_counts[pid] = game_counts.get(pid, 0) + 1
        for pid in team_b:
            current_ratings[pid] = current_ratings.get(pid, 1000.0) + delta_b
            game_counts[pid] = game_counts.get(pid, 0) + 1

        # XP: every player who played earns the base for this match's stage,
        # winners earn a bonus. Stage is None for a regular match. (Event
        # multipliers will hook in here in a later stage, keyed off the
        # match date so recompute stays reproducible.)
        stage = m.get('stage')
        won_a = (winner == 'A')
        won_b = (winner == 'B')
        margin = int(abs(score_a - score_b))
        # Event multiplier for THIS match's date (events preloaded above).
        mult = event_multiplier_for_date(m.get('date'), _events)
        for pid in team_a:
            xp_totals[pid] = xp_totals.get(pid, 0) + round(xp_for_match(stage, won_a, margin) * mult)
        for pid in team_b:
            xp_totals[pid] = xp_totals.get(pid, 0) + round(xp_for_match(stage, won_b, margin) * mult)

        # The rating history graph reads ratings_after directly off each
        # match record - if we don't write the corrected values back here,
        # a correction fixes everyone's current rating but leaves the
        # historical trail permanently showing the old, wrong numbers.
        new_ratings_after = {pid: int(round(current_ratings[pid])) for pid in team_a + team_b}
        if m.get('ratings_after') != new_ratings_after:
            matches_table.update_item(
                Key={'match_id': m['match_id']},
                UpdateExpression='SET ratings_after = :r',
                ExpressionAttributeValues={':r': new_ratings_after}
            )

    for pid, rating in current_ratings.items():
        xp = xp_totals.get(pid, 0)
        player = next((p for p in players if p['player_id'] == pid), {})
        # Quest rewards are XP earned outside matches (claimed), so they're
        # added on top of match XP rather than recomputed - otherwise a
        # replay would erase them. Stored separately in quest_xp.
        xp += int(player.get('quest_xp', 0) or 0)
        level = level_from_xp(xp)
        # Coins are earned per level gained. On a full recompute we recompute
        # total coins EARNED (50 per level above 1), but must not clobber
        # coins the player has SPENT. We track coins_spent separately, so
        # spendable balance = earned - spent, never going negative.
        earned = COINS_PER_LEVEL * (level - 1)
        spent = int(player.get('coins_spent', 0) or 0)
        quest_coins = int(player.get('quest_coins', 0) or 0)
        balance = max(0, earned + quest_coins - spent)
        players_table.update_item(
            Key={'player_id': pid},
            UpdateExpression='SET rating = :r, xp = :xp, #lvl = :lvl, coins = :c, coins_earned = :ce, games_played = :g',
            ExpressionAttributeNames={'#lvl': 'level'},
            ExpressionAttributeValues={
                ':r': int(round(rating)), ':xp': xp, ':lvl': level,
                ':c': balance, ':ce': earned, ':g': game_counts.get(pid, 0)
            })


def compute_momentum_stats(point_log, winner):
    """Longest scoring streak per team, and how big a deficit the winner overcame."""
    if not point_log:
        return {}

    longest_streak = {'A': 0, 'B': 0}
    current_streak = {'A': 0, 'B': 0}
    running = {'A': 0, 'B': 0}
    worst_deficit_for_winner = 0

    scoring_runs = 0
    prev_point = None
    for point in point_log:
        other = 'B' if point == 'A' else 'A'
        if point != prev_point:
            scoring_runs += 1
            prev_point = point
        current_streak[point] += 1
        current_streak[other] = 0
        longest_streak[point] = max(longest_streak[point], current_streak[point])
        running[point] += 1

        if winner in ('A', 'B'):
            deficit = running[other] - running[winner]
            if deficit > worst_deficit_for_winner:
                worst_deficit_for_winner = deficit

    # Guard against the live counter being misused to batch-enter a final
    # score (e.g. tapping 21 points for one side, then 19 for the other).
    # A real ~40-point badminton game changes scorer dozens of times; a
    # batch entry produces 1-3 unbroken runs. Such a log is not a genuine
    # point-by-point record, so it must not fabricate a "comeback" (which
    # would both pollute the Hall of Fame and grant an undeserved Elo
    # bonus to the winner).
    suspected_batch_entry = len(point_log) >= 20 and scoring_runs <= 3

    result = {
        'longest_streak_a': longest_streak['A'],
        'longest_streak_b': longest_streak['B'],
        'winner_overcame_deficit': (worst_deficit_for_winner
                                     if winner in ('A', 'B') and not suspected_batch_entry else 0)
    }
    if suspected_batch_entry:
        result['suspected_batch_entry'] = True
    return result


def compute_adaptive_k(pairing_count):
    """Higher K for a fresh/novel doubles pairing (each match together is
    high-information, since we don't yet know how these two specific
    players perform as a unit). Lower K once a pairing is well-established
    (each additional match together adds little new information, and this
    is what keeps a fixed partnership's ratings from swinging wildly in
    lockstep every single time they play). Singles has no pairing concept,
    so it always uses the flat K_FACTOR."""
    if pairing_count == 0:
        return 40
    elif pairing_count < 5:
        return K_FACTOR
    else:
        return 20


def get_pairing_count(team_ids, exclude_match_id=None):
    """How many prior doubles matches has this exact 2-player team played
    together, based on matches already recorded (regardless of opponent)."""
    if len(team_ids) != 2:
        return 0
    pair_key = frozenset(team_ids)
    count = 0
    items = _scan_all(matches_table)
    for m in items:
        if exclude_match_id and m.get('match_id') == exclude_match_id:
            continue
        if m.get('match_type') != 'doubles':
            continue
        for team in (m.get('team_a') or [], m.get('team_b') or []):
            if len(team) == 2 and frozenset(team) == pair_key:
                count += 1
                break
    return count


def _play_and_log(match_type, team_a_ids, team_b_ids, score_a, score_b, group_id, tournament_id, stage,
                   point_log=None, points_to_win=21):
    team_a_players = [players_table.get_item(Key={'player_id': pid}).get('Item') for pid in team_a_ids]
    team_b_players = [players_table.get_item(Key={'player_id': pid}).get('Item') for pid in team_b_ids]
    if any(p is None for p in team_a_players) or any(p is None for p in team_b_players):
        return None

    rating_a_avg = sum(float(p.get('rating', 1000)) for p in team_a_players) / len(team_a_players)
    rating_b_avg = sum(float(p.get('rating', 1000)) for p in team_b_players) / len(team_b_players)

    actual_a = 1.0 if score_a > score_b else (0.0 if score_a < score_b else 0.5)
    actual_b = 1.0 - actual_a

    expected_a = 1 / (1 + 10 ** ((rating_b_avg - rating_a_avg) / 400))
    expected_b = 1 - expected_a

    if match_type == 'doubles':
        k_a = compute_adaptive_k(get_pairing_count(team_a_ids))
        k_b = compute_adaptive_k(get_pairing_count(team_b_ids))
    else:
        k_a = k_b = K_FACTOR

    delta_a = k_a * (actual_a - expected_a)
    delta_b = k_b * (actual_b - expected_b)

    winner = 'A' if score_a > score_b else ('B' if score_b > score_a else 'tie')

    momentum = None
    if point_log:
        momentum = compute_momentum_stats(point_log, winner)
        bonus = compute_comeback_bonus(momentum)
        if winner == 'A':
            delta_a += bonus
        elif winner == 'B':
            delta_b += bonus

    new_ratings = {}
    for p in team_a_players:
        new_ratings[p['player_id']] = int(round(float(p.get('rating', 1000)) + delta_a))
    for p in team_b_players:
        new_ratings[p['player_id']] = int(round(float(p.get('rating', 1000)) + delta_b))

    # Event multiplier once for this match (it's "now"), not per player.
    _live_mult = event_multiplier_for_date(datetime.now(timezone.utc).isoformat())
    for pid, new_rating in new_ratings.items():
        # Snapshot the rating this player held BEFORE this match as
        # previous_rating. Ranking players by previous_rating vs current
        # rating is what powers the up/down arrow next to their rank - it
        # captures the single most recent move without storing full history.
        player_obj = next((p for p in team_a_players + team_b_players if p['player_id'] == pid), {})
        prev = int(round(float(player_obj.get('rating', 1000))))
        # XP earned live for this match. Mirrors the recompute award so a
        # later recompute reproduces the same totals. Level and coin balance
        # are recomputed from the new XP total (coins = earned - spent).
        won = ((winner == 'A' and pid in team_a_ids) or (winner == 'B' and pid in team_b_ids))
        gained = round(xp_for_match(stage, won, int(abs(score_a - score_b))) * _live_mult)
        new_xp = int(player_obj.get('xp', 0) or 0) + gained
        new_level = level_from_xp(new_xp + int(player_obj.get('quest_xp', 0) or 0))
        earned = COINS_PER_LEVEL * (new_level - 1)
        spent = int(player_obj.get('coins_spent', 0) or 0)
        quest_coins = int(player_obj.get('quest_coins', 0) or 0)
        balance = max(0, earned + quest_coins - spent)
        players_table.update_item(
            Key={'player_id': pid},
            UpdateExpression='SET rating = :r, previous_rating = :pr, xp = :xp, #lvl = :lvl, coins = :c, coins_earned = :ce, games_played = if_not_exists(games_played, :zero) + :one',
            ExpressionAttributeNames={'#lvl': 'level'},
            ExpressionAttributeValues={':r': new_rating, ':pr': prev, ':xp': new_xp,
                                       ':lvl': new_level, ':c': balance, ':ce': earned,
                                       ':zero': 0, ':one': 1})

    item = {
        'match_id': str(uuid.uuid4()),
        'date': datetime.now(timezone.utc).isoformat(),
        'match_type': match_type,
        'team_a': team_a_ids,
        'team_b': team_b_ids,
        'team_a_names': [p['name'] for p in team_a_players],
        'team_b_names': [p['name'] for p in team_b_players],
        'score_a': score_a,
        'score_b': score_b,
        'points_to_win': points_to_win,
        'winner': winner,
        'ratings_after': new_ratings,
    }
    if group_id:
        item['group_id'] = group_id
    if tournament_id:
        item['tournament_id'] = tournament_id
        item['stage'] = stage
    if point_log:
        item['point_log'] = point_log
        item['momentum'] = momentum

    matches_table.put_item(Item=item)
    return item


def list_matches(event):
    params = event.get('queryStringParameters') or {}
    group_id = params.get('group_id')
    player_id = params.get('player_id')
    date_from = params.get('date_from')  # 'YYYY-MM-DD'
    date_to = params.get('date_to')      # 'YYYY-MM-DD'
    partnerships_for = params.get('partnerships_for')
    attendance = params.get('attendance')
    hall_of_fame = params.get('hall_of_fame')
    radar_for = params.get('radar_for')
    top_n = int(params.get('top_n', 10))
    tournament_filter = params.get('tournament_filter', 'include')  # 'include' | 'exclude'

    items = _scan_all(matches_table)
    # Reserved config rows (events) live in this table but aren't matches.
    items = [i for i in items if i.get('match_id') not in (_EVENTS_ROW_ID, _QUESTS_ROW_ID)]
    # Privacy: omit private players from comparative outputs for everyone but a
    # SuperAdmin (only ever identified via an authed route). No-op when off.
    private_ids = set() if _is_super_admin(_caller_claims(event)) else _load_private_ids()
    if params.get('stats_bundle'):
        # One call, one scan -> the 4 matches-derived Stats sections. Previously
        # 4 concurrent /matches calls, each doing its own full-table scan (a
        # big chunk of the Lambda-concurrency + DynamoDB-RCU pressure).
        # Optional group_id scopes all four the same way the individual
        # loaders already do (the frontend defaults every Stats-tab filter to
        # the caller's own group, so first paint is group-scoped without
        # costing an extra request - the bundle is still exactly one call).
        return _response(200, {
            'hall_of_fame': _scrub_private(compute_hall_of_fame(items, group_id), private_ids),
            'diversity': _scrub_private(compute_diversity(items, group_id), private_ids),
            'progress_badges': _scrub_private(compute_progress_badges(items, group_id), private_ids),
            'attendance': _scrub_private(compute_attendance(items, group_id), private_ids),
        })
    if params.get('counts_by_group'):
        # How many matches have been logged under each group - a SuperAdmin
        # sees every group; an owner/admin's own group-detail panel calls
        # this scoped to just their group(s) via the same group_id filter
        # every other branch here already supports (Owner-requested
        # 2026-08-20). One tally over the scan this route already does for
        # every other query shape - no extra scan added.
        counts = {}
        ungrouped = 0
        for i in items:
            gid = i.get('group_id')
            if gid:
                counts[gid] = counts.get(gid, 0) + 1
            else:
                ungrouped += 1
        return _response(200, {'group_counts': counts, 'ungrouped': ungrouped, 'total': len(items)})
    if params.get('player_season_summary'):
        _pss = params.get('player_season_summary')
        if _pss in private_ids:
            return _response(200, {'enabled': True, 'seasons': [], 'private': True})
        return _response(200, compute_player_season_summary(_pss, items))
    if params.get('seasons') == 'list':
        s_enabled, s_k, s_resolved = _season_config()
        s_cur = _resolve_season(s_resolved, 'current')
        return _response(200, {'enabled': s_enabled, 'soft_reset_k': s_k,
                               'current_id': s_cur['id'] if s_cur else None, 'seasons': s_resolved})
    if params.get('season_leaderboard'):
        s_enabled, s_k, s_resolved = _season_config()
        if not s_enabled:
            return _response(200, {'enabled': False, 'leaders': []})
        season = _resolve_season(s_resolved, params.get('season_leaderboard'))
        if not season:
            return _response(404, {'error': 'season not found'})
        s_today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        s_row_id = _SEASON_ROW_PREFIX + season['id']
        if season['end_date'] <= s_today:
            # Season has ended -> SEAL it: freeze the final board once so past
            # seasons are immutable (later match edits recompute lifetime but no
            # longer reshuffle a finished season).
            s_row = matches_table.get_item(Key={'match_id': s_row_id}).get('Item') or {}
            if s_row.get('sealed_leaders') is not None:
                return _response(200, {'season': season, 'enabled': True, 'sealed': True,
                                       'min_games': 5,
                                       'leaders': _scrub_private(s_row['sealed_leaders'], private_ids)})
            board = compute_season_leaderboard(season, items, s_k)
            try:
                matches_table.update_item(Key={'match_id': s_row_id},
                    UpdateExpression='SET sealed_leaders = :l, sealed_at = :t',
                    ExpressionAttributeValues={':l': board['leaders'], ':t': s_today})
            except Exception:
                pass
            board['sealed'] = True
            board['enabled'] = True
            board['leaders'] = _scrub_private(board['leaders'], private_ids)
            return _response(200, board)
        # Ongoing season -> compute live (recalculates from the frozen baseline).
        board = compute_season_leaderboard(season, items, s_k)
        board['leaders'] = _scrub_private(board['leaders'], private_ids)
        board['enabled'] = True
        return _response(200, board)

    if partnerships_for or radar_for:
        scoped_items = items
        if group_id:
            scoped_items = [i for i in scoped_items if i.get('group_id') == group_id]
        if tournament_filter == 'exclude':
            scoped_items = [i for i in scoped_items if not i.get('tournament_id')]
        if partnerships_for:
            return _response(200, _scrub_private(compute_partnerships(partnerships_for, scoped_items), private_ids))
        return _response(200, _scrub_private(compute_partner_distribution(radar_for, scoped_items, top_n), private_ids))
    if attendance:
        return _response(200, _scrub_private(compute_attendance(items, group_id), private_ids))
    if hall_of_fame:
        return _response(200, _scrub_private(compute_hall_of_fame(items, group_id), private_ids))
    if params.get('diversity'):
        return _response(200, _scrub_private(compute_diversity(items, group_id), private_ids))
    if params.get('progress_badges'):
        return _response(200, _scrub_private(compute_progress_badges(items, group_id), private_ids))
    if params.get('achievements_for'):
        all_tournaments = _scan_all(tournaments_table)
        return _response(200, compute_achievements(params.get('achievements_for'), items, all_tournaments))
    if params.get('profile_bundle_for'):
        player_id = params.get('profile_bundle_for')
        all_tournaments = _scan_all(tournaments_table)
        # Scrub leaderboard/distribution parts for private players; leave the
        # card owner's own factual history (recent_form, record) untouched.
        return _response(200, {
            'hall_of_fame': _scrub_private(compute_hall_of_fame(items), private_ids),
            'progress_badges': _scrub_private(compute_progress_badges(items), private_ids),
            'achievements': compute_achievements(player_id, items, all_tournaments),
            'recent_form': compute_recent_form(player_id, items, 10),
            'overall_record': compute_overall_record(player_id, items),
            'top_opponents': _scrub_private(compute_top_opponents(player_id, items, 15), private_ids),
            'attendance': _scrub_private(compute_attendance(items)['attendance'], private_ids),
        })
    if params.get('progress_history'):
        scope = params.get('scope', 'global')
        period = params.get('period', 'week')
        return _response(200, compute_progress_history_summary(scope, period))
    if params.get('head_to_head') and params.get('opponent'):
        return _response(200, compute_head_to_head(params.get('head_to_head'), params.get('opponent'), items))
    if params.get('with_partner') and params.get('partner'):
        return _response(200, compute_with_partner(params.get('with_partner'), params.get('partner'), items))
    if params.get('recent_form'):
        limit = int(params.get('limit', 10))
        return _response(200, compute_recent_form(params.get('recent_form'), items, limit))
    if params.get('top_opponents_for'):
        top_n = int(params.get('top_n', 15))
        return _response(200, _scrub_private(compute_top_opponents(params.get('top_opponents_for'), items, top_n), private_ids))
    if params.get('overall_record_for'):
        return _response(200, compute_overall_record(params.get('overall_record_for'), items))

    if group_id:
        items = [i for i in items if i.get('group_id') == group_id]
    if player_id:
        items = [i for i in items if player_id in (i.get('team_a') or []) or player_id in (i.get('team_b') or [])]
    if date_from:
        items = [i for i in items if i.get('date', '') >= date_from]
    if date_to:
        # date_to is a calendar day - include the whole day (matches are stored in UTC)
        items = [i for i in items if i.get('date', '') <= date_to + 'T23:59:59.999999+00:00']

    items.sort(key=lambda i: i.get('date', ''), reverse=True)

    return _response(200, {'matches': items})


def compute_partnerships(player_id, items):
    """For a given player, tally win/loss record with each doubles partner
    they've played alongside. This is a straightforward performance
    breakdown (not a full statistical synergy score against individual
    rating expectations, which would need historical pre-match ratings we
    don't currently snapshot)."""
    partner_stats = {}
    for m in items:
        if m.get('match_type') != 'doubles':
            continue
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        if player_id in team_a and len(team_a) == 2:
            team, won = team_a, (m.get('winner') == 'A')
        elif player_id in team_b and len(team_b) == 2:
            team, won = team_b, (m.get('winner') == 'B')
        else:
            continue

        partner_id = next((pid for pid in team if pid != player_id), None)
        if not partner_id:
            continue
        if partner_id not in partner_stats:
            partner_stats[partner_id] = {'partner_id': partner_id, 'matches': 0, 'wins': 0, 'losses': 0}
        stats = partner_stats[partner_id]
        stats['matches'] += 1
        if won:
            stats['wins'] += 1
        elif m.get('winner') in ('A', 'B'):
            stats['losses'] += 1

    result = []
    for pid, stats in partner_stats.items():
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        stats['partner_name'] = display_name(p, pid)
        stats['win_rate'] = round(stats['wins'] / stats['matches'] * 100, 1) if stats['matches'] else 0
        result.append(stats)

    result.sort(key=lambda s: -s['matches'])
    return {'player_id': player_id, 'partnerships': result}


def get_group_member_ids(group_id):
    """The set of player_ids belonging to a group, used to filter WHO shows
    up in a stat's results - not to restrict WHICH matches count. A group
    filter means 'show me these people's numbers', computed from their
    full match history regardless of whether any individual match happened
    to be tagged with this group at recording time (tagging is optional
    and many matches are never tagged at all)."""
    group = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    return set(group.get('member_ids', [])) if group else set()


def compute_attendance(items, group_id_filter=None):
    """Per-player attendance/consistency: total matches, distinct calendar
    dates played (a proxy for 'sessions attended'), recent activity
    windows, and their longest run of consecutive weeks with at least one
    match. Optionally scoped to a group - this shows only that group's
    members, but each member's own numbers are computed from their FULL
    match history (including standalone matches never tagged with any
    group), not just matches tagged with this specific group."""
    member_ids = get_group_member_ids(group_id_filter) if group_id_filter else None

    now = datetime.now(timezone.utc)
    player_stats = {}
    for m in items:
        date_str = m.get('date')
        if not date_str:
            continue
        try:
            match_date = datetime.fromisoformat(date_str)
        except ValueError:
            continue
        days_ago = (now - match_date).days
        day_key = date_str[:10]
        # A stable, sortable week index where consecutive calendar weeks
        # differ by exactly 1, avoiding manual year-boundary handling.
        iso_year, iso_week, _ = match_date.isocalendar()
        week_index = date.fromisocalendar(iso_year, iso_week, 1).toordinal() // 7

        for pid in (m.get('team_a') or []) + (m.get('team_b') or []):
            if pid not in player_stats:
                player_stats[pid] = {'player_id': pid, 'total_matches': 0, 'session_dates': set(),
                                      'last_30_days': 0, 'last_90_days': 0, 'week_indices': set()}
            s = player_stats[pid]
            s['total_matches'] += 1
            s['session_dates'].add(day_key)
            s['week_indices'].add(week_index)
            if days_ago <= 30:
                s['last_30_days'] += 1
            if days_ago <= 90:
                s['last_90_days'] += 1

    result = []
    for pid, s in player_stats.items():
        if member_ids is not None and pid not in member_ids:
            continue
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        weeks_sorted = sorted(s['week_indices'])
        best_streak = 1 if weeks_sorted else 0
        current_run = 1
        for i in range(1, len(weeks_sorted)):
            if weeks_sorted[i] == weeks_sorted[i - 1] + 1:
                current_run += 1
                best_streak = max(best_streak, current_run)
            else:
                current_run = 1
        result.append({
            'player_id': pid,
            'name': display_name(p, pid),
            'total_matches': s['total_matches'],
            'sessions_attended': len(s['session_dates']),
            'matches_last_30_days': s['last_30_days'],
            'matches_last_90_days': s['last_90_days'],
            'longest_week_streak': best_streak,
        })

    result.sort(key=lambda r: -r['sessions_attended'])
    return {'attendance': result}


def compute_hall_of_fame(items, group_id_filter=None):
    """Highlight stats computed from full chronological match history:
    longest win streak, biggest blowout, peak rating ever per player,
    biggest upsets (giant-killer), best comebacks (only available for
    matches recorded via the live point-by-point counter), rating
    consistency/volatility, singles-vs-doubles specialization, and
    group-stage-vs-knockout performance (deep-run rate).

    Names are resolved from the current Players table at the very end,
    not from the names frozen onto each match record at the time it was
    played - a rename should show up everywhere immediately, rather than
    only affecting matches recorded after the rename happened.

    Optionally scoped to a group: this filters WHO shows up in the
    results to that group's members, but every computation still uses
    each player's FULL match history (including standalone matches never
    tagged with any group) - a group filter means 'show me these
    people's numbers', not 'only count matches tagged with this group'."""
    member_ids = get_group_member_ids(group_id_filter) if group_id_filter else None

    matches = sorted(items, key=lambda m: m.get('date', ''))

    name_cache = {}

    def resolve_name(pid, fallback=None):
        if pid not in name_cache:
            p = players_table.get_item(Key={'player_id': pid}).get('Item')
            name_cache[pid] = display_name(p, fallback or pid)
        return name_cache[pid]

    rolling_ratings = {}
    current_streak = {}
    personal_best_streaks = {}  # pid -> their own best-ever streak
    peak_rating = {}          # pid -> rating
    blowout_candidates = []   # every match's blowout info, filtered at output time
    giant_killer_candidates = []  # will hold winner_ids/loser_ids
    comeback_candidates = []      # will hold winner_ids
    player_deltas = {}       # pid -> [deltas]
    format_stats = {}        # pid -> {'singles_w','singles_l','doubles_w','doubles_l'}
    stage_stats = {}         # pid -> {'group_w','group_l','knockout_w','knockout_l'}
    tournament_stage_sets = {}  # pid -> {'group_tournaments': set(), 'knockout_tournaments': set()}
    pair_stats = {}          # frozenset(pair) -> {'wins','losses','members'}
    deuce_wins = {}          # pid -> wins by exactly 2 points past the target
    session_record = {}      # (pid, yyyy-mm-dd) -> {'wins','losses'}
    session_deltas = {}      # yyyy-mm-dd -> {pid: total rating delta that day}

    def team_avg(team_ids):
        return sum(rolling_ratings.get(pid, 1000.0) for pid in team_ids) / len(team_ids) if team_ids else 1000.0

    for m in matches:
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        team_a_names = m.get('team_a_names') or []
        team_b_names = m.get('team_b_names') or []
        score_a = m.get('score_a')
        score_b = m.get('score_b')
        winner = m.get('winner')
        match_type = m.get('match_type')
        stage = m.get('stage')
        tournament_id = m.get('tournament_id')
        if not team_a or not team_b or score_a is None or score_b is None:
            continue
        score_a, score_b = float(score_a), float(score_b)

        pre_a = team_avg(team_a)
        pre_b = team_avg(team_b)
        pre_individual = {pid: rolling_ratings.get(pid, 1000.0) for pid in team_a + team_b}

        if winner in ('A', 'B'):
            winners = team_a if winner == 'A' else team_b
            losers = team_b if winner == 'A' else team_a
            pre_winner = pre_a if winner == 'A' else pre_b
            pre_loser = pre_b if winner == 'A' else pre_a

            for pid in winners:
                current_streak[pid] = current_streak.get(pid, 0) + 1
                if current_streak[pid] > personal_best_streaks.get(pid, 0):
                    personal_best_streaks[pid] = current_streak[pid]
            for pid in losers:
                current_streak[pid] = 0

            day = (m.get('date') or '')[:10]
            for pid in winners + losers:
                rec = session_record.setdefault((pid, day), {'wins': 0, 'losses': 0})
                rec['wins' if pid in winners else 'losses'] += 1

            # Doubles chemistry: per fixed pair, wins/losses together.
            if match_type == 'doubles':
                for side, side_won in ((team_a, winner == 'A'), (team_b, winner == 'B')):
                    if len(side) == 2:
                        key = frozenset(side)
                        ps = pair_stats.setdefault(key, {'wins': 0, 'losses': 0, 'members': sorted(side)})
                        ps['wins' if side_won else 'losses'] += 1

            # Deuce specialist: won by exactly 2, past the normal target
            # (i.e. the game went to deuce and they closed it out).
            win_score, lose_score = max(score_a, score_b), min(score_a, score_b)
            if win_score - lose_score == 2 and win_score >= 22:
                for pid in winners:
                    deuce_wins[pid] = deuce_wins.get(pid, 0) + 1

            upset_gap = pre_loser - pre_winner
            if upset_gap > 0:
                giant_killer_candidates.append({
                    'winner_ids': winners, 'loser_ids': losers,
                    'upset_gap': round(upset_gap, 1), 'date': m.get('date'),
                    'score': f"{int(score_a)}-{int(score_b)}" if winner == 'A' else f"{int(score_b)}-{int(score_a)}"
                })

            for pid in team_a + team_b:
                won = pid in winners
                if match_type in ('singles', 'doubles'):
                    fs = format_stats.setdefault(pid, {'singles_w': 0, 'singles_l': 0, 'doubles_w': 0, 'doubles_l': 0})
                    key = 'singles' if match_type == 'singles' else 'doubles'
                    fs[f'{key}_w' if won else f'{key}_l'] += 1
                if stage in ('group', 'knockout'):
                    ss = stage_stats.setdefault(pid, {'group_w': 0, 'group_l': 0, 'knockout_w': 0, 'knockout_l': 0})
                    ss[f'{stage}_w' if won else f'{stage}_l'] += 1

        if stage in ('group', 'knockout') and tournament_id:
            for pid in team_a + team_b:
                sets = tournament_stage_sets.setdefault(pid, {'group_tournaments': set(), 'knockout_tournaments': set()})
                sets[f'{stage}_tournaments'].add(tournament_id)

        margin = abs(score_a - score_b)
        winning_side_ids = team_a if score_a > score_b else team_b
        blowout_candidates.append({
            'team_a_ids': team_a, 'team_b_ids': team_b,
            'winning_side_ids': winning_side_ids,
            'score_a': int(score_a), 'score_b': int(score_b),
            'margin': int(margin), 'date': m.get('date')
        })

        momentum = m.get('momentum')
        if momentum and momentum.get('winner_overcame_deficit', 0) > 0:
            winners = team_a if winner == 'A' else team_b
            comeback_candidates.append({
                'winner_ids': winners, 'deficit_overcome': int(momentum['winner_overcame_deficit']),
                'date': m.get('date')
            })

        ratings_after = m.get('ratings_after') or {}
        day = (m.get('date') or '')[:10]
        for pid, rating in ratings_after.items():
            rating = float(rating)
            delta = rating - pre_individual.get(pid, 1000.0)
            player_deltas.setdefault(pid, []).append(delta)
            session_deltas.setdefault(day, {})[pid] = session_deltas.setdefault(day, {}).get(pid, 0) + delta

            rolling_ratings[pid] = rating
            if pid not in peak_rating or rating > peak_rating[pid]:
                peak_rating[pid] = rating

    giant_killer_candidates.sort(key=lambda g: -g['upset_gap'])
    comeback_candidates.sort(key=lambda c: -c['deficit_overcome'])

    # Group filtering happens here, at output time, not by restricting
    # which matches got processed above - every calculation above already
    # used each player's FULL history. This just decides which rows are
    # relevant to show for this group.
    def in_group(pid):
        return member_ids is None or pid in member_ids

    def side_in_group(ids):
        return member_ids is None or all(pid in member_ids for pid in ids)

    giant_killer_candidates = [g for g in giant_killer_candidates if side_in_group(g['winner_ids'])]
    comeback_candidates = [c for c in comeback_candidates if side_in_group(c['winner_ids'])]
    blowout_candidates = [b for b in blowout_candidates if side_in_group(b['winning_side_ids'])]
    biggest_blowout = max(blowout_candidates, key=lambda b: b['margin']) if blowout_candidates else None

    eligible_streaks = {pid: s for pid, s in personal_best_streaks.items() if in_group(pid)}
    best_streak = None
    if eligible_streaks:
        top_pid = max(eligible_streaks.items(), key=lambda kv: kv[1])[0]
        best_streak = {'player_id': top_pid, 'streak': eligible_streaks[top_pid]}

    # Consistency / volatility - population standard deviation of rating deltas, min 3 matches
    consistency_rows = []
    for pid, deltas in player_deltas.items():
        if len(deltas) < 3 or not in_group(pid):
            continue
        mean_delta = sum(deltas) / len(deltas)
        variance = sum((d - mean_delta) ** 2 for d in deltas) / len(deltas)
        stdev = round(variance ** 0.5, 1)
        consistency_rows.append({'player_id': pid, 'matches': len(deltas), 'volatility': stdev})
    consistency_rows.sort(key=lambda r: r['volatility'])
    most_consistent = consistency_rows[:5]
    most_volatile = sorted(consistency_rows, key=lambda r: -r['volatility'])[:5]

    # Format specialist - biggest gap between singles and doubles win rate, min 2 matches in each
    format_rows = []
    for pid, fs in format_stats.items():
        if not in_group(pid):
            continue
        singles_total = fs['singles_w'] + fs['singles_l']
        doubles_total = fs['doubles_w'] + fs['doubles_l']
        if singles_total < 2 or doubles_total < 2:
            continue
        singles_pct = round(fs['singles_w'] / singles_total * 100, 1)
        doubles_pct = round(fs['doubles_w'] / doubles_total * 100, 1)
        format_rows.append({
            'player_id': pid,
            'singles_win_pct': singles_pct, 'doubles_win_pct': doubles_pct,
            'gap': round(abs(singles_pct - doubles_pct), 1),
            'stronger_format': 'singles' if singles_pct > doubles_pct else 'doubles'
        })
    format_rows.sort(key=lambda r: -r['gap'])

    # Deep-run rate - fraction of tournament appearances that included a knockout-stage match
    deep_run_rows = []
    for pid, sets in tournament_stage_sets.items():
        if not in_group(pid):
            continue
        all_tournaments = sets['group_tournaments'] | sets['knockout_tournaments']
        if not all_tournaments:
            continue
        rate = round(len(sets['knockout_tournaments']) / len(all_tournaments) * 100, 1)
        deep_run_rows.append({
            'player_id': pid,
            'tournaments_entered': len(all_tournaments),
            'reached_knockout': len(sets['knockout_tournaments']),
            'deep_run_rate': rate
        })
    deep_run_rows.sort(key=lambda r: -r['deep_run_rate'])

    # Best partnerships - doubles pairs by win rate, minimum 3 matches together
    partnership_rows = []
    for key, ps in pair_stats.items():
        total = ps['wins'] + ps['losses']
        if total < 3 or not side_in_group(ps['members']):
            continue
        partnership_rows.append({
            'member_ids': ps['members'], 'matches': total, 'wins': ps['wins'],
            'losses': ps['losses'], 'win_pct': round(ps['wins'] / total * 100, 1)
        })
    partnership_rows.sort(key=lambda r: (-r['win_pct'], -r['matches']))

    # Deuce specialists - most wins by exactly 2 past the target
    deuce_rows = sorted(
        ({'player_id': pid, 'deuce_wins': n} for pid, n in deuce_wins.items() if in_group(pid)),
        key=lambda r: -r['deuce_wins'])

    # Undefeated sessions - days with 3+ matches and zero losses
    undefeated_counts = {}
    for (pid, day), rec in session_record.items():
        if rec['wins'] >= 3 and rec['losses'] == 0:
            undefeated_counts[pid] = undefeated_counts.get(pid, 0) + 1
    undefeated_rows = sorted(
        ({'player_id': pid, 'sessions': n} for pid, n in undefeated_counts.items() if in_group(pid)),
        key=lambda r: -r['sessions'])

    # Biggest single-match rating swing (positive) per player
    swing_rows = sorted(
        ({'player_id': pid, 'swing': round(max(deltas), 1)}
         for pid, deltas in player_deltas.items() if deltas and max(deltas) > 0 and in_group(pid)),
        key=lambda r: -r['swing'])

    # Session MVP - best total rating delta on each play date (co-winners on ties)
    session_mvp_rows = []
    mvp_counts = {}
    for day in sorted(session_deltas, reverse=True):
        eligible = {pid: d for pid, d in session_deltas[day].items() if in_group(pid)}
        if not eligible:
            continue
        best = max(eligible.values())
        mvp_ids = sorted(pid for pid, d in eligible.items() if abs(d - best) < 0.01)
        for pid in mvp_ids:
            mvp_counts[pid] = mvp_counts.get(pid, 0) + 1
        session_mvp_rows.append({'date': day, 'player_ids': mvp_ids, 'delta': round(best, 1)})
    mvp_count_rows = sorted(({'player_id': pid, 'mvp_days': n} for pid, n in mvp_counts.items()),
                             key=lambda r: -r['mvp_days'])

    # Resolve every name from the current Players table, right here at the
    # end - this is the one place names get attached to the output.
    for row in consistency_rows:
        row['name'] = resolve_name(row['player_id'])
    for row in format_rows:
        row['name'] = resolve_name(row['player_id'])
    for row in deep_run_rows:
        row['name'] = resolve_name(row['player_id'])
    for g in giant_killer_candidates:
        g['winner_names'] = [resolve_name(pid) for pid in g['winner_ids']]
        g['loser_names'] = [resolve_name(pid) for pid in g['loser_ids']]
    for c in comeback_candidates:
        c['winner_names'] = [resolve_name(pid) for pid in c['winner_ids']]
    if biggest_blowout:
        biggest_blowout['team_a_names'] = [resolve_name(pid) for pid in biggest_blowout['team_a_ids']]
        biggest_blowout['team_b_names'] = [resolve_name(pid) for pid in biggest_blowout['team_b_ids']]
    if best_streak:
        best_streak['name'] = resolve_name(best_streak['player_id'])
    peak_ratings_list = [
        {'player_id': pid, 'name': resolve_name(pid), 'rating': int(round(rating))}
        for pid, rating in peak_rating.items() if in_group(pid)
    ]
    for row in partnership_rows:
        row['names'] = [resolve_name(pid) for pid in row['member_ids']]
    for row in deuce_rows + undefeated_rows + swing_rows + mvp_count_rows:
        row['name'] = resolve_name(row['player_id'])
    for row in session_mvp_rows:
        row['names'] = [resolve_name(pid) for pid in row['player_ids']]

    return {
        'longest_win_streak': best_streak,
        'biggest_blowout': biggest_blowout,
        'peak_ratings': sorted(peak_ratings_list, key=lambda p: -p['rating'])[:10],
        'giant_killer_top5': giant_killer_candidates[:5],
        'comeback_top5': comeback_candidates[:5],
        'most_consistent': most_consistent,
        'most_volatile': most_volatile,
        'format_specialists': format_rows[:5],
        'deep_run_rates': deep_run_rows[:10],
        'best_partnerships': partnership_rows[:5],
        'deuce_specialists': deuce_rows[:5],
        'undefeated_sessions': undefeated_rows[:5],
        'biggest_swings': swing_rows[:5],
        'session_mvps': session_mvp_rows[:10],
        'mvp_counts': mvp_count_rows[:5]
    }


def compute_achievements(player_id, matches, tournaments):
    """Milestone/tiered achievement progress for one player: total matches
    played, tournament championships won, and their own personal-best win
    streak (distinct from the single overall record-holder tracked in
    hall_of_fame - this is specifically about this player's own history)."""
    total_matches = sum(
        1 for m in matches
        if player_id in (m.get('team_a') or []) or player_id in (m.get('team_b') or [])
    )

    tournament_wins = 0
    runner_ups = 0
    third_places = 0
    for t in tournaments:
        if t.get('status') != 'completed':
            continue
        knockout = t.get('knockout') or {}
        rounds = knockout.get('rounds') or []
        if rounds and rounds[-1]:
            final_match = rounds[-1][0]
            winner_id = final_match.get('winner_id')
            if winner_id:
                player_a = final_match.get('player_a') or {}
                player_b = final_match.get('player_b') or {}
                winner_entity = player_a if winner_id == player_a.get('player_id') else player_b
                loser_entity = player_b if winner_id == player_a.get('player_id') else player_a
                if player_id in (winner_entity.get('members') or []):
                    tournament_wins += 1
                elif player_id in (loser_entity.get('members') or []):
                    runner_ups += 1
        third = knockout.get('third_place_match')
        if third and third.get('winner_id'):
            ta = third.get('player_a') or {}
            tb = third.get('player_b') or {}
            third_winner = ta if third['winner_id'] == ta.get('player_id') else tb
            if player_id in (third_winner.get('members') or []):
                third_places += 1

    player_matches = sorted(
        [m for m in matches if player_id in (m.get('team_a') or []) or player_id in (m.get('team_b') or [])],
        key=lambda m: m.get('date', '')
    )
    current_streak = 0
    best_streak = 0
    total_wins = 0
    total_losses = 0
    worst_loss_streak = 0
    current_loss = 0
    for m in player_matches:
        winner = m.get('winner')
        team_a = m.get('team_a') or []
        if winner not in ('A', 'B'):
            continue
        won = (winner == 'A' and player_id in team_a) or (winner == 'B' and player_id not in team_a)
        if won:
            current_streak += 1
            best_streak = max(best_streak, current_streak)
            total_wins += 1
            current_loss = 0
        else:
            current_streak = 0
            total_losses += 1
            current_loss += 1
            worst_loss_streak = max(worst_loss_streak, current_loss)

    # Deuce wins, undefeated sessions, attendance streak, and peak rating -
    # all from this player's own match history.
    deuce_wins = 0
    session_record = {}   # yyyy-mm-dd -> {'wins','losses'}
    peak = 0
    for m in player_matches:
        winner = m.get('winner')
        team_a = m.get('team_a') or []
        score_a, score_b = m.get('score_a'), m.get('score_b')
        day = (m.get('date') or '')[:10]
        rating = (m.get('ratings_after') or {}).get(player_id)
        if rating is not None:
            peak = max(peak, int(round(float(rating))))
        if winner not in ('A', 'B') or score_a is None or score_b is None:
            continue
        won = (winner == 'A' and player_id in team_a) or (winner == 'B' and player_id not in team_a)
        rec = session_record.setdefault(day, {'wins': 0, 'losses': 0})
        rec['wins' if won else 'losses'] += 1
        win_score, lose_score = max(float(score_a), float(score_b)), min(float(score_a), float(score_b))
        if won and win_score - lose_score == 2 and win_score >= 22:
            deuce_wins += 1

    undefeated_sessions = sum(1 for rec in session_record.values()
                               if rec['wins'] >= 3 and rec['losses'] == 0)

    # Best attendance streak: longest run of consecutive CLUB session dates
    # (days when anyone played) on which this player also appeared.
    club_days = sorted({(m.get('date') or '')[:10] for m in matches if m.get('date')})
    attended = set(session_record.keys())
    best_attendance = run = 0
    for day in club_days:
        run = run + 1 if day in attended else 0
        best_attendance = max(best_attendance, run)

    # Season achievements: cumulative across every season this player qualified in.
    _season_wins = _season_podiums = _season_improved = _season_iron = _seasons_played = 0
    try:
        for _s in (compute_player_season_summary(player_id, matches).get('seasons') or []):
            if not _s.get('sealed'):
                continue
            _seasons_played += 1
            if _s.get('rank') == 1:
                _season_wins += 1
            if _s.get('rank') in (1, 2, 3):
                _season_podiums += 1
            for _b in (_s.get('badges') or []):
                if _b.get('kind') == 'most_improved':
                    _season_improved += 1
                elif _b.get('kind') == 'iron':
                    _season_iron += 1
    except Exception:
        pass
    return {
        'player_id': player_id,
        'total_matches': total_matches,
        'tournament_wins': tournament_wins,
        'runner_ups': runner_ups,
        'third_places': third_places,
        'podium_finishes': tournament_wins + runner_ups + third_places,
        'personal_best_streak': best_streak,
        'current_streak': current_streak,
        'deuce_wins': deuce_wins,
        'undefeated_sessions': undefeated_sessions,
        'best_attendance_streak': best_attendance,
        'peak_rating': peak,
        'total_wins': total_wins,
        'total_losses': total_losses,
        'worst_loss_streak': worst_loss_streak,
        'season_wins': _season_wins,
        'season_podiums': _season_podiums,
        'season_most_improved': _season_improved,
        'season_iron': _season_iron,
        'seasons_played': _seasons_played
    }


def compute_top_opponents(player_id, matches, top_n=15):
    """This player's win/loss record against every opponent they've ever
    faced (singles or doubles, as an OPPONENT - not a teammate), ranked
    by how many times they've played each other, most-played first."""
    records = {}  # opponent_id -> {'wins': int, 'losses': int}
    for m in matches:
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        winner = m.get('winner')
        if winner not in ('A', 'B'):
            continue
        player_in_a = player_id in team_a
        player_in_b = player_id in team_b
        if not (player_in_a or player_in_b):
            continue
        opponents = team_b if player_in_a else team_a
        player_won = (winner == 'A' and player_in_a) or (winner == 'B' and player_in_b)
        for opp_id in opponents:
            if opp_id == player_id:
                continue
            rec = records.setdefault(opp_id, {'wins': 0, 'losses': 0})
            if player_won:
                rec['wins'] += 1
            else:
                rec['losses'] += 1

    rows = []
    for opp_id, rec in records.items():
        p = players_table.get_item(Key={'player_id': opp_id}).get('Item')
        total = rec['wins'] + rec['losses']
        rows.append({
            'opponent_id': opp_id,
            'opponent_name': display_name(p, opp_id),
            'matches': total,
            'wins': rec['wins'],
            'losses': rec['losses'],
            'win_rate': round(rec['wins'] / total * 100, 1) if total else 0
        })
    rows.sort(key=lambda r: -r['matches'])
    return {'player_id': player_id, 'opponents': rows[:top_n]}


def compute_overall_record(player_id, matches):
    """This player's total win/loss record, split by singles and doubles."""
    record = {'singles_wins': 0, 'singles_losses': 0, 'doubles_wins': 0, 'doubles_losses': 0}
    for m in matches:
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        winner = m.get('winner')
        match_type = m.get('match_type')
        if winner not in ('A', 'B') or match_type not in ('singles', 'doubles'):
            continue
        if player_id in team_a:
            won = winner == 'A'
        elif player_id in team_b:
            won = winner == 'B'
        else:
            continue
        key = 'singles' if match_type == 'singles' else 'doubles'
        record[f'{key}_wins' if won else f'{key}_losses'] += 1

    total_wins = record['singles_wins'] + record['doubles_wins']
    total_losses = record['singles_losses'] + record['doubles_losses']
    return {
        'player_id': player_id,
        'total_wins': total_wins, 'total_losses': total_losses,
        'singles_wins': record['singles_wins'], 'singles_losses': record['singles_losses'],
        'doubles_wins': record['doubles_wins'], 'doubles_losses': record['doubles_losses']
    }


def compute_head_to_head(player_id, opponent_id, matches):
    """One player's win/loss record specifically as an OPPONENT of another
    player - distinct from partnerships, which only covers doubles
    teammates. Counts any match (singles or doubles) where the two were
    on opposite teams."""
    wins = 0
    losses = 0
    for m in matches:
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        winner = m.get('winner')
        if winner not in ('A', 'B'):
            continue
        player_in_a = player_id in team_a
        player_in_b = player_id in team_b
        opponent_in_a = opponent_id in team_a
        opponent_in_b = opponent_id in team_b
        if not ((player_in_a and opponent_in_b) or (player_in_b and opponent_in_a)):
            continue  # not opposing teams in this match (or one/both absent)
        player_won = (winner == 'A' and player_in_a) or (winner == 'B' and player_in_b)
        if player_won:
            wins += 1
        else:
            losses += 1
    total = wins + losses
    return {
        'player_id': player_id, 'opponent_id': opponent_id,
        'matches': total, 'wins': wins, 'losses': losses,
        'win_rate': round(wins / total * 100, 1) if total else 0
    }


def compute_with_partner(player_id, partner_id, matches):
    """One player's win/loss record when partnered WITH another player on
    the SAME side - the mirror of compute_head_to_head, which counts them on
    opposite sides. Doubles only (both must be on the same team). Distinct
    from compute_partnerships, which tallies every partner at once; this
    answers 'how do I do specifically alongside X'."""
    wins = 0
    losses = 0
    games = []
    for m in matches:
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        winner = m.get('winner')
        if winner not in ('A', 'B'):
            continue
        both_a = player_id in team_a and partner_id in team_a
        both_b = player_id in team_b and partner_id in team_b
        if not (both_a or both_b):
            continue  # not teammates in this match (or one/both absent)
        team_won = (winner == 'A' and both_a) or (winner == 'B' and both_b)
        if team_won:
            wins += 1
        else:
            losses += 1
        # The opponents are whichever side the pair was NOT on. Names/scores
        # are stored on the match, so no extra lookups are needed.
        if both_a:
            opp_names = m.get('team_b_names') or []
            our_score, their_score = m.get('score_a'), m.get('score_b')
        else:
            opp_names = m.get('team_a_names') or []
            our_score, their_score = m.get('score_b'), m.get('score_a')
        games.append({
            'match_id': m.get('match_id'),
            'date': m.get('date'),
            'opponents': opp_names,
            'our_score': our_score,
            'their_score': their_score,
            'won': team_won,
        })
    # Most recent first, to match how the game log reads.
    games.sort(key=lambda g: g.get('date') or '', reverse=True)
    total = wins + losses
    return {
        'player_id': player_id, 'partner_id': partner_id,
        'matches': total, 'wins': wins, 'losses': losses,
        'win_rate': round(wins / total * 100, 1) if total else 0,
        'games': games
    }


def compute_recent_form(player_id, matches, limit=10):
    """A player's last N matches, in chronological order (oldest to
    newest) so a left-to-right rendering naturally puts the most recent
    result on the right. Walks the player's FULL match history (not just
    the last N) so the rating delta on the oldest match shown is still
    measured against the correct prior rating, then trims to `limit`
    afterwards."""
    player_matches = sorted(
        [m for m in matches if player_id in (m.get('team_a') or []) or player_id in (m.get('team_b') or [])],
        key=lambda m: m.get('date', '')
    )

    form = []
    prev_rating = 1000.0  # starting Elo, same baseline used everywhere else
    for m in player_matches:
        winner = m.get('winner')
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        team_a_names = m.get('team_a_names') or []
        team_b_names = m.get('team_b_names') or []
        ratings_after = m.get('ratings_after') or {}

        # Update the running baseline regardless of whether this match ends
        # up in the displayed window, so the delta shown for the first
        # match in that window is still correct.
        # ratings_after values come back from DynamoDB as Decimal, not
        # float - subtracting a float from a Decimal raises a TypeError,
        # which was crashing this whole endpoint with a 500. Cast explicitly.
        after_raw = ratings_after.get(player_id)
        after = float(after_raw) if after_raw is not None else None
        delta = round(after - prev_rating) if after is not None else 0
        if after is not None:
            prev_rating = after

        if winner not in ('A', 'B'):
            continue

        on_team_a = player_id in team_a
        won = (winner == 'A' and on_team_a) or (winner == 'B' and not on_team_a)
        opponent_names = team_b_names if on_team_a else team_a_names
        own_ids = team_a if on_team_a else team_b
        own_names = team_a_names if on_team_a else team_b_names
        # Doubles partner(s): everyone on the player's own team besides
        # themselves. Empty for singles.
        partner_names = [name for pid, name in zip(own_ids, own_names) if pid != player_id]

        form.append({
            'date': m.get('date'),
            'result': 'W' if won else 'L',
            'delta': delta,
            'opponent_names': opponent_names or [],
            'partner_names': partner_names
        })

    return {'player_id': player_id, 'form': form[-limit:]}


def compute_diversity(items, group_id_filter=None):
    """For every player: how concentrated their doubles partnerships are.
    'top_partner_pct' is the share of their matches played with their single
    most frequent partner - a simple, intuitive stand-in for 'how entangled
    is this rating with one fixed pairing'. Sorted with the most
    concentrated (least-mixed) players first. Optionally scoped to a
    group - shows only that group's members, but each member's own
    partner-concentration is computed from their full history."""
    member_ids = get_group_member_ids(group_id_filter) if group_id_filter else None

    partner_counts = {}  # player_id -> {partner_id: count}
    for m in items:
        if m.get('match_type') != 'doubles':
            continue
        for team in (m.get('team_a') or [], m.get('team_b') or []):
            if len(team) != 2:
                continue
            p1, p2 = team
            partner_counts.setdefault(p1, {}).setdefault(p2, 0)
            partner_counts[p1][p2] += 1
            partner_counts.setdefault(p2, {}).setdefault(p1, 0)
            partner_counts[p2][p1] += 1

    result = []
    for pid, counts in partner_counts.items():
        if member_ids is not None and pid not in member_ids:
            continue
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        total = sum(counts.values())
        top_partner_id, top_count = max(counts.items(), key=lambda kv: kv[1])
        top_partner = players_table.get_item(Key={'player_id': top_partner_id}).get('Item')
        result.append({
            'player_id': pid,
            'name': display_name(p, pid),
            'total_matches': total,
            'distinct_partners': len(counts),
            'top_partner_id': top_partner_id,
            'top_partner_name': display_name(top_partner, top_partner_id),
            'top_partner_pct': round(top_count / total * 100, 1) if total else 0
        })

    result.sort(key=lambda r: -r['top_partner_pct'])
    return {'players': result}


def compute_progress_history_summary(scope_label, period_name):
    """Reads the permanent, locked-in weekly/monthly/yearly winner history
    for one scope (global or group_{id}) and one period type, computing
    streaks (consecutive periods won in a row) and lifetime holder counts
    for 'most improved' - the gamified badges built on top of history that
    can never be recomputed retroactively once it's been overwritten."""
    items = _scan_all(history_table)
    filtered = [i for i in items if i.get('scope') == scope_label and i.get('period') == period_name]
    filtered.sort(key=lambda i: i.get('period_start', ''))

    def winner_ids(entry):
        """Set of 'most improved' co-winners for a history row. New rows
        store a list (ties are structural: both doubles partners always get
        identical Elo deltas); rows written before that change only have the
        singular field, so fall back to it."""
        ids = entry.get('most_improved_player_ids')
        if ids:
            return set(ids)
        pid = entry.get('most_improved_player_id')
        return {pid} if pid else set()

    # A "hold" and a "streak" belong to each individual co-winner: if A & B
    # tie this week and A alone wins next week, A is on a 2-streak while B
    # is on a 1-streak.
    holder_counts = {}
    for entry in filtered:
        for pid in winner_ids(entry):
            holder_counts[pid] = holder_counts.get(pid, 0) + 1

    current_streaks = []
    if filtered:
        latest_winners = winner_ids(filtered[-1])
        for pid in latest_winners:
            streak = 0
            for entry in reversed(filtered):
                if pid in winner_ids(entry):
                    streak += 1
                else:
                    break
            current_streaks.append({'player_id': pid, 'streak': streak})
        current_streaks.sort(key=lambda s: (-s['streak'], s['player_id']))

    longest_streaks = {}
    running = {}  # pid -> current consecutive count
    for entry in filtered:
        winners = winner_ids(entry)
        for pid in winners:
            running[pid] = running.get(pid, 0) + 1
            longest_streaks[pid] = max(longest_streaks.get(pid, 0), running[pid])
        for pid in list(running):
            if pid not in winners:
                running[pid] = 0

    return {
        'history': [
            {
                'period_start': e.get('period_start'), 'period_end': e.get('period_end'),
                'computed_at': e.get('computed_at'),
                'most_improved_name': e.get('most_improved_name'), 'most_improved_delta': e.get('most_improved_delta'),
                'most_improved_names': e.get('most_improved_names'),
                'most_improved_player_ids': e.get('most_improved_player_ids')
                    or ([e['most_improved_player_id']] if e.get('most_improved_player_id') else []),
                'most_active_name': e.get('most_active_name'), 'most_active_matches': e.get('most_active_matches'),
                'most_active_names': e.get('most_active_names'),
                'most_active_player_ids': e.get('most_active_player_ids')
                    or ([e['most_active_player_id']] if e.get('most_active_player_id') else []),
            } for e in filtered
        ],
        'holder_counts': [{'player_id': pid, 'count': c} for pid, c in sorted(holder_counts.items(), key=lambda kv: (-kv[1], kv[0]))],
        # Legacy shape (single winner) plus the full co-winner list.
        'current_streak': ({'player_id': current_streaks[0]['player_id'], 'streak': current_streaks[0]['streak']}
                            if current_streaks else None),
        'current_streaks': current_streaks,
        'longest_streaks': [{'player_id': pid, 'streak': s} for pid, s in sorted(longest_streaks.items(), key=lambda kv: (-kv[1], kv[0]))]
    }


def compute_progress_badges(items, group_id_filter=None):
    """For each of the last week/month/year: who improved their rating the
    most, and who played the most matches. Names are resolved live from
    the current Players table, same reasoning as hall_of_fame - a rename
    should show up immediately everywhere, not just in future matches.
    Optionally scoped to a group - shows only that group's members
    competing against each other for the badge, but each member's own
    rating delta is computed from their full match history."""
    member_ids = get_group_member_ids(group_id_filter) if group_id_filter else None

    matches = sorted(items, key=lambda m: m.get('date', ''))
    now = datetime.now(timezone.utc)
    periods = {
        'week': now - timedelta(days=7),
        'month': now - timedelta(days=30),
        'year': now - timedelta(days=365),
    }

    name_cache = {}

    def resolve_name(pid):
        if pid not in name_cache:
            p = players_table.get_item(Key={'player_id': pid}).get('Item')
            name_cache[pid] = display_name(p, pid)
        return name_cache[pid]

    result = {}
    for period_name, cutoff in periods.items():
        rating_before_cutoff = {}
        rating_current = {}
        matches_in_period = {}

        for m in matches:
            date_str = m.get('date', '')
            try:
                match_date = datetime.fromisoformat(date_str)
            except ValueError:
                continue
            ratings_after = m.get('ratings_after') or {}
            for pid, rating in ratings_after.items():
                rating = float(rating)
                if match_date < cutoff:
                    rating_before_cutoff[pid] = rating
                rating_current[pid] = rating
                if match_date >= cutoff:
                    matches_in_period[pid] = matches_in_period.get(pid, 0) + 1

        progress_rows = []
        for pid, current in rating_current.items():
            if member_ids is not None and pid not in member_ids:
                continue
            start = rating_before_cutoff.get(pid, 1000.0)
            delta = round(current - start, 1)
            progress_rows.append({
                'player_id': pid, 'name': resolve_name(pid),
                'delta': delta, 'current_rating': int(round(current)),
                'matches_in_period': matches_in_period.get(pid, 0)
            })
        progress_rows.sort(key=lambda r: -r['delta'])

        most_active = None
        eligible_activity = {pid: cnt for pid, cnt in matches_in_period.items()
                              if member_ids is None or pid in member_ids}
        if eligible_activity:
            active_pid = max(eligible_activity.items(), key=lambda kv: kv[1])[0]
            most_active = {'player_id': active_pid, 'name': resolve_name(active_pid),
                            'matches': eligible_activity[active_pid]}

        result[period_name] = {
            'most_improved_top5': [r for r in progress_rows if r['matches_in_period'] > 0][:5],
            'most_active': most_active
        }

    return result


def compute_partner_distribution(player_id, items, top_n=10):
    """For the radar/spider chart: one player's doubles partners, sorted by
    how often they've played together, capped at top_n so the chart stays
    readable. Percentages are based on the total within whatever scope was
    already applied to `items` (a specific group, or every match). Also
    tracks how many of each partner's matches came from a tournament
    (fixed pairing) versus standalone play, so the frontend can optionally
    highlight the tournament-driven share separately."""
    partner_counts = {}
    partner_tournament_counts = {}
    total = 0
    for m in items:
        if m.get('match_type') != 'doubles':
            continue
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        if player_id in team_a and len(team_a) == 2:
            team = team_a
        elif player_id in team_b and len(team_b) == 2:
            team = team_b
        else:
            continue
        partner_id = next((pid for pid in team if pid != player_id), None)
        if not partner_id:
            continue
        partner_counts[partner_id] = partner_counts.get(partner_id, 0) + 1
        if m.get('tournament_id'):
            partner_tournament_counts[partner_id] = partner_tournament_counts.get(partner_id, 0) + 1
        total += 1

    result = []
    for pid, count in partner_counts.items():
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        tcount = partner_tournament_counts.get(pid, 0)
        result.append({
            'partner_id': pid,
            'name': display_name(p, pid),
            'matches': count,
            'percentage': round(count / total * 100, 1) if total else 0,
            'tournament_matches': tcount,
            'tournament_percentage': round(tcount / total * 100, 1) if total else 0
        })

    result.sort(key=lambda r: -r['matches'])
    return {'player_id': player_id, 'total_matches': total, 'partners': result[:top_n]}


def _response(status_code, body_dict):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body_dict, default=str)
    }
