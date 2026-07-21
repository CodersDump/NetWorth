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
CONFIRMATION_CODE = 'Matchpoint-Falcon-77'  # private - never shown in the UI; change this if it's ever exposed


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


def handler(event, context):
    try:
        method = event.get('httpMethod')
        match_id = (event.get('pathParameters') or {}).get('match_id')
        if method == 'POST':
            return record_match(event)
        elif method == 'GET':
            return list_matches(event)
        elif method == 'PUT' and match_id:
            return update_match(match_id, event)
        return _response(404, {'error': 'not found'})
    except Exception as e:
        return _response(500, {'error': str(e)})


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
    Requires the confirmation code, same as deletions and renames. Since
    Elo is path-dependent, changing a score doesn't just affect this one
    match's own rating delta - it can shift everyone who played after it
    too - so every edit triggers a full recompute of every player's rating
    from scratch, replaying the corrected history in order."""
    body = json.loads(event.get('body') or '{}')
    if body.get('confirm') != CONFIRMATION_CODE:
        return _response(400, {'error': 'confirmation code is missing or incorrect'})

    existing = matches_table.get_item(Key={'match_id': match_id}).get('Item')
    if not existing:
        return _response(404, {'error': 'match not found'})

    new_score_a = body.get('score_a')
    new_score_b = body.get('score_b')
    if new_score_a is None or new_score_b is None:
        return _response(400, {'error': 'score_a and score_b are required'})
    new_score_a, new_score_b = int(new_score_a), int(new_score_b)
    if new_score_a == new_score_b:
        return _response(400, {'error': 'scores cannot be tied'})

    new_winner = 'A' if new_score_a > new_score_b else 'B'

    matches_table.update_item(
        Key={'match_id': match_id},
        UpdateExpression='SET score_a = :sa, score_b = :sb, winner = :w',
        ExpressionAttributeValues={':sa': new_score_a, ':sb': new_score_b, ':w': new_winner}
    )

    recompute_all_ratings()

    updated = matches_table.get_item(Key={'match_id': match_id}).get('Item')
    return _response(200, {'match': updated, 'note': 'All player ratings were recomputed from the corrected match history.'})


def recompute_all_ratings():
    """Elo is path-dependent - each match's rating change depends on the
    ratings at that exact moment, which depend on everything before it.
    After correcting a match, the only fully correct fix is to reset
    everyone to 1000 and replay every match in chronological order,
    recomputing from scratch - including replaying each pairing's K-factor
    exactly as it would have been at that point in time."""
    players = players_table.scan().get('Items', [])
    current_ratings = {p['player_id']: 1000.0 for p in players}
    pairing_counts = {}  # frozenset({p1,p2}) -> matches played together so far

    matches = matches_table.scan().get('Items', [])
    matches.sort(key=lambda m: m.get('date', ''))

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

        for pid in team_a:
            current_ratings[pid] = current_ratings.get(pid, 1000.0) + delta_a
        for pid in team_b:
            current_ratings[pid] = current_ratings.get(pid, 1000.0) + delta_b

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
        players_table.update_item(Key={'player_id': pid}, UpdateExpression='SET rating = :r',
                                   ExpressionAttributeValues={':r': int(round(rating))})


def compute_momentum_stats(point_log, winner):
    """Longest scoring streak per team, and how big a deficit the winner overcame."""
    if not point_log:
        return {}

    longest_streak = {'A': 0, 'B': 0}
    current_streak = {'A': 0, 'B': 0}
    running = {'A': 0, 'B': 0}
    worst_deficit_for_winner = 0

    for point in point_log:
        other = 'B' if point == 'A' else 'A'
        current_streak[point] += 1
        current_streak[other] = 0
        longest_streak[point] = max(longest_streak[point], current_streak[point])
        running[point] += 1

        if winner in ('A', 'B'):
            deficit = running[other] - running[winner]
            if deficit > worst_deficit_for_winner:
                worst_deficit_for_winner = deficit

    return {
        'longest_streak_a': longest_streak['A'],
        'longest_streak_b': longest_streak['B'],
        'winner_overcame_deficit': worst_deficit_for_winner if winner in ('A', 'B') else 0
    }


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
    items = matches_table.scan().get('Items', [])
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

    new_ratings = {}
    for p in team_a_players:
        new_ratings[p['player_id']] = int(round(float(p.get('rating', 1000)) + delta_a))
    for p in team_b_players:
        new_ratings[p['player_id']] = int(round(float(p.get('rating', 1000)) + delta_b))

    for pid, new_rating in new_ratings.items():
        players_table.update_item(Key={'player_id': pid}, UpdateExpression='SET rating = :r',
                                   ExpressionAttributeValues={':r': new_rating})

    winner = 'A' if score_a > score_b else ('B' if score_b > score_a else 'tie')

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
        item['momentum'] = compute_momentum_stats(point_log, winner)

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

    items = matches_table.scan().get('Items', [])

    if partnerships_for or radar_for:
        scoped_items = items
        if group_id:
            scoped_items = [i for i in scoped_items if i.get('group_id') == group_id]
        if tournament_filter == 'exclude':
            scoped_items = [i for i in scoped_items if not i.get('tournament_id')]
        if partnerships_for:
            return _response(200, compute_partnerships(partnerships_for, scoped_items))
        return _response(200, compute_partner_distribution(radar_for, scoped_items, top_n))
    if attendance:
        return _response(200, compute_attendance(items, group_id))
    if hall_of_fame:
        return _response(200, compute_hall_of_fame(items, group_id))
    if params.get('diversity'):
        return _response(200, compute_diversity(items, group_id))
    if params.get('progress_badges'):
        return _response(200, compute_progress_badges(items, group_id))
    if params.get('achievements_for'):
        all_tournaments = tournaments_table.scan().get('Items', [])
        return _response(200, compute_achievements(params.get('achievements_for'), items, all_tournaments))
    if params.get('progress_history'):
        scope = params.get('scope', 'global')
        period = params.get('period', 'week')
        return _response(200, compute_progress_history_summary(scope, period))

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
        stats['partner_name'] = p['name'] if p else pid
        stats['win_rate'] = round(stats['wins'] / stats['matches'] * 100, 1) if stats['matches'] else 0
        result.append(stats)

    result.sort(key=lambda s: -s['matches'])
    return {'player_id': player_id, 'partnerships': result}


def compute_attendance(items, group_id_filter=None):
    """Per-player attendance/consistency: total matches, distinct calendar
    dates played (a proxy for 'sessions attended'), recent activity
    windows, and their longest run of consecutive weeks with at least one
    match. Optionally scoped to one group."""
    if group_id_filter:
        items = [i for i in items if i.get('group_id') == group_id_filter]

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
            'name': p['name'] if p else pid,
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
    only affecting matches recorded after the rename happened."""
    if group_id_filter:
        items = [i for i in items if i.get('group_id') == group_id_filter]

    matches = sorted(items, key=lambda m: m.get('date', ''))

    name_cache = {}

    def resolve_name(pid, fallback=None):
        if pid not in name_cache:
            p = players_table.get_item(Key={'player_id': pid}).get('Item')
            name_cache[pid] = p['name'] if p else (fallback or pid)
        return name_cache[pid]

    rolling_ratings = {}
    current_streak = {}
    best_streak = {'player_id': None, 'streak': 0}
    peak_rating = {}          # pid -> rating
    biggest_blowout = None    # will hold team_a_ids/team_b_ids, resolved to names at the end
    giant_killer_candidates = []  # will hold winner_ids/loser_ids
    comeback_candidates = []      # will hold winner_ids
    player_deltas = {}       # pid -> [deltas]
    format_stats = {}        # pid -> {'singles_w','singles_l','doubles_w','doubles_l'}
    stage_stats = {}         # pid -> {'group_w','group_l','knockout_w','knockout_l'}
    tournament_stage_sets = {}  # pid -> {'group_tournaments': set(), 'knockout_tournaments': set()}

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
                if current_streak[pid] > best_streak['streak']:
                    best_streak.update({'player_id': pid, 'streak': current_streak[pid]})
            for pid in losers:
                current_streak[pid] = 0

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
        if biggest_blowout is None or margin > biggest_blowout['margin']:
            biggest_blowout = {
                'team_a_ids': team_a, 'team_b_ids': team_b,
                'score_a': int(score_a), 'score_b': int(score_b),
                'margin': int(margin), 'date': m.get('date')
            }

        momentum = m.get('momentum')
        if momentum and momentum.get('winner_overcame_deficit', 0) > 0:
            winners = team_a if winner == 'A' else team_b
            comeback_candidates.append({
                'winner_ids': winners, 'deficit_overcome': int(momentum['winner_overcame_deficit']),
                'date': m.get('date')
            })

        ratings_after = m.get('ratings_after') or {}
        for pid, rating in ratings_after.items():
            rating = float(rating)
            player_deltas.setdefault(pid, []).append(rating - pre_individual.get(pid, 1000.0))

            rolling_ratings[pid] = rating
            if pid not in peak_rating or rating > peak_rating[pid]:
                peak_rating[pid] = rating

    giant_killer_candidates.sort(key=lambda g: -g['upset_gap'])
    comeback_candidates.sort(key=lambda c: -c['deficit_overcome'])

    # Consistency / volatility - population standard deviation of rating deltas, min 3 matches
    consistency_rows = []
    for pid, deltas in player_deltas.items():
        if len(deltas) < 3:
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
    if best_streak['player_id']:
        best_streak['name'] = resolve_name(best_streak['player_id'])
    peak_ratings_list = [
        {'player_id': pid, 'name': resolve_name(pid), 'rating': int(round(rating))}
        for pid, rating in peak_rating.items()
    ]

    return {
        'longest_win_streak': best_streak if best_streak['player_id'] else None,
        'biggest_blowout': biggest_blowout,
        'peak_ratings': sorted(peak_ratings_list, key=lambda p: -p['rating'])[:10],
        'giant_killer_top5': giant_killer_candidates[:5],
        'comeback_top5': comeback_candidates[:5],
        'most_consistent': most_consistent,
        'most_volatile': most_volatile,
        'format_specialists': format_rows[:5],
        'deep_run_rates': deep_run_rows[:10]
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
    for t in tournaments:
        if t.get('status') != 'completed':
            continue
        knockout = t.get('knockout') or {}
        rounds = knockout.get('rounds') or []
        if not rounds or not rounds[-1]:
            continue
        final_match = rounds[-1][0]
        winner_id = final_match.get('winner_id')
        if not winner_id:
            continue
        player_a = final_match.get('player_a') or {}
        player_b = final_match.get('player_b') or {}
        winner_entity = player_a if winner_id == player_a.get('player_id') else player_b
        if player_id in (winner_entity.get('members') or []):
            tournament_wins += 1

    player_matches = sorted(
        [m for m in matches if player_id in (m.get('team_a') or []) or player_id in (m.get('team_b') or [])],
        key=lambda m: m.get('date', '')
    )
    current_streak = 0
    best_streak = 0
    for m in player_matches:
        winner = m.get('winner')
        team_a = m.get('team_a') or []
        if winner not in ('A', 'B'):
            continue
        won = (winner == 'A' and player_id in team_a) or (winner == 'B' and player_id not in team_a)
        if won:
            current_streak += 1
            best_streak = max(best_streak, current_streak)
        else:
            current_streak = 0

    return {
        'player_id': player_id,
        'total_matches': total_matches,
        'tournament_wins': tournament_wins,
        'personal_best_streak': best_streak
    }


def compute_diversity(items, group_id_filter=None):
    """For every player: how concentrated their doubles partnerships are.
    'top_partner_pct' is the share of their matches played with their single
    most frequent partner - a simple, intuitive stand-in for 'how entangled
    is this rating with one fixed pairing'. Sorted with the most
    concentrated (least-mixed) players first."""
    if group_id_filter:
        items = [i for i in items if i.get('group_id') == group_id_filter]

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
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        total = sum(counts.values())
        top_partner_id, top_count = max(counts.items(), key=lambda kv: kv[1])
        top_partner = players_table.get_item(Key={'player_id': top_partner_id}).get('Item')
        result.append({
            'player_id': pid,
            'name': p['name'] if p else pid,
            'total_matches': total,
            'distinct_partners': len(counts),
            'top_partner_name': top_partner['name'] if top_partner else top_partner_id,
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
    items = history_table.scan().get('Items', [])
    filtered = [i for i in items if i.get('scope') == scope_label and i.get('period') == period_name]
    filtered.sort(key=lambda i: i.get('period_start', ''))

    holder_counts = {}
    for entry in filtered:
        pid = entry.get('most_improved_player_id')
        if pid:
            holder_counts[pid] = holder_counts.get(pid, 0) + 1

    current_streak_pid = None
    current_streak = 0
    for entry in reversed(filtered):
        pid = entry.get('most_improved_player_id')
        if pid is None:
            break
        if current_streak_pid is None:
            current_streak_pid = pid
            current_streak = 1
        elif pid == current_streak_pid:
            current_streak += 1
        else:
            break

    longest_streaks = {}
    running_pid = None
    running_len = 0
    for entry in filtered:
        pid = entry.get('most_improved_player_id')
        if pid == running_pid and pid is not None:
            running_len += 1
        else:
            running_pid = pid
            running_len = 1 if pid else 0
        if pid:
            longest_streaks[pid] = max(longest_streaks.get(pid, 0), running_len)

    return {
        'history': [
            {
                'period_start': e.get('period_start'), 'period_end': e.get('period_end'),
                'most_improved_name': e.get('most_improved_name'), 'most_improved_delta': e.get('most_improved_delta'),
                'most_active_name': e.get('most_active_name'), 'most_active_matches': e.get('most_active_matches'),
            } for e in filtered
        ],
        'holder_counts': [{'player_id': pid, 'count': c} for pid, c in sorted(holder_counts.items(), key=lambda kv: -kv[1])],
        'current_streak': {'player_id': current_streak_pid, 'streak': current_streak} if current_streak_pid else None,
        'longest_streaks': [{'player_id': pid, 'streak': s} for pid, s in sorted(longest_streaks.items(), key=lambda kv: -kv[1])]
    }


def compute_progress_badges(items, group_id_filter=None):
    """For each of the last week/month/year: who improved their rating the
    most, and who played the most matches. Names are resolved live from
    the current Players table, same reasoning as hall_of_fame - a rename
    should show up immediately everywhere, not just in future matches."""
    if group_id_filter:
        items = [i for i in items if i.get('group_id') == group_id_filter]

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
            name_cache[pid] = p['name'] if p else pid
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
            start = rating_before_cutoff.get(pid, 1000.0)
            delta = round(current - start, 1)
            progress_rows.append({
                'player_id': pid, 'name': resolve_name(pid),
                'delta': delta, 'current_rating': int(round(current)),
                'matches_in_period': matches_in_period.get(pid, 0)
            })
        progress_rows.sort(key=lambda r: -r['delta'])

        most_active = None
        if matches_in_period:
            active_pid = max(matches_in_period.items(), key=lambda kv: kv[1])[0]
            most_active = {'player_id': active_pid, 'name': resolve_name(active_pid),
                            'matches': matches_in_period[active_pid]}

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
            'name': p['name'] if p else pid,
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
