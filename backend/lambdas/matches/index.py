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
from datetime import datetime, timezone

dynamodb = boto3.resource('dynamodb')
matches_table = dynamodb.Table(os.environ['MATCHES_TABLE'])
players_table = dynamodb.Table(os.environ['PLAYERS_TABLE'])

K_FACTOR = 32


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
        if method == 'POST':
            return record_match(event)
        elif method == 'GET':
            return list_matches(event)
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

    delta_a = K_FACTOR * (actual_a - expected_a)
    delta_b = K_FACTOR * (actual_b - expected_b)

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
    network = params.get('network')
    hall_of_fame = params.get('hall_of_fame')
    radar_for = params.get('radar_for')
    top_n = int(params.get('top_n', 10))

    items = matches_table.scan().get('Items', [])

    if partnerships_for:
        return _response(200, compute_partnerships(partnerships_for, items))
    if attendance:
        return _response(200, compute_attendance(items, group_id))
    if network:
        return _response(200, compute_network(items, group_id))
    if hall_of_fame:
        return _response(200, compute_hall_of_fame(items, group_id))
    if radar_for:
        scoped_items = items
        if group_id:
            scoped_items = [i for i in scoped_items if i.get('group_id') == group_id]
        return _response(200, compute_partner_distribution(radar_for, scoped_items, top_n))

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
    dates played (a proxy for 'sessions attended'), and recent activity
    windows. Optionally scoped to one group."""
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

        for pid in (m.get('team_a') or []) + (m.get('team_b') or []):
            if pid not in player_stats:
                player_stats[pid] = {'player_id': pid, 'total_matches': 0, 'session_dates': set(),
                                      'last_30_days': 0, 'last_90_days': 0}
            s = player_stats[pid]
            s['total_matches'] += 1
            s['session_dates'].add(day_key)
            if days_ago <= 30:
                s['last_30_days'] += 1
            if days_ago <= 90:
                s['last_90_days'] += 1

    result = []
    for pid, s in player_stats.items():
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        result.append({
            'player_id': pid,
            'name': p['name'] if p else pid,
            'total_matches': s['total_matches'],
            'sessions_attended': len(s['session_dates']),
            'matches_last_30_days': s['last_30_days'],
            'matches_last_90_days': s['last_90_days'],
        })

    result.sort(key=lambda r: -r['sessions_attended'])
    return {'attendance': result}


def compute_network(items, group_id_filter=None):
    """Partnership graph data: one node per player who's appeared in a
    doubles match, one edge per pair who've been teammates, weighted by
    how many times. Same data feeds both a force-directed view and a
    circular/chord view on the frontend - only the drawing differs."""
    if group_id_filter:
        items = [i for i in items if i.get('group_id') == group_id_filter]

    node_names = {}
    edge_counts = {}

    for m in items:
        if m.get('match_type') != 'doubles':
            continue
        for team, names in ((m.get('team_a') or [], m.get('team_a_names') or []),
                             (m.get('team_b') or [], m.get('team_b_names') or [])):
            if len(team) != 2:
                continue
            for pid, name in zip(team, names):
                node_names[pid] = name
            pair_key = tuple(sorted(team))
            edge_counts[pair_key] = edge_counts.get(pair_key, 0) + 1

    nodes = [{'player_id': pid, 'name': name} for pid, name in node_names.items()]
    edges = [{'player_a': pair[0], 'player_b': pair[1], 'matches_together': count}
              for pair, count in edge_counts.items()]

    return {'nodes': nodes, 'edges': edges}


def compute_hall_of_fame(items, group_id_filter=None):
    """Highlight stats computed from full chronological match history:
    longest win streak, biggest blowout, peak rating ever per player,
    biggest upsets (giant-killer), and best comebacks (only available for
    matches recorded via the live point-by-point counter, since that's the
    only source of momentum data)."""
    if group_id_filter:
        items = [i for i in items if i.get('group_id') == group_id_filter]

    matches = sorted(items, key=lambda m: m.get('date', ''))

    rolling_ratings = {}
    current_streak = {}
    best_streak = {'player_id': None, 'name': None, 'streak': 0}
    peak_rating = {}
    biggest_blowout = None
    giant_killer_candidates = []
    comeback_candidates = []

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
        if not team_a or not team_b or score_a is None or score_b is None:
            continue
        score_a, score_b = float(score_a), float(score_b)

        pre_a = team_avg(team_a)
        pre_b = team_avg(team_b)

        if winner in ('A', 'B'):
            winners = team_a if winner == 'A' else team_b
            losers = team_b if winner == 'A' else team_a
            winner_names = team_a_names if winner == 'A' else team_b_names
            loser_names = team_b_names if winner == 'A' else team_a_names
            pre_winner = pre_a if winner == 'A' else pre_b
            pre_loser = pre_b if winner == 'A' else pre_a

            for pid in winners:
                current_streak[pid] = current_streak.get(pid, 0) + 1
                if current_streak[pid] > best_streak['streak']:
                    name = next((n for p, n in zip(winners, winner_names) if p == pid), pid)
                    best_streak.update({'player_id': pid, 'name': name, 'streak': current_streak[pid]})
            for pid in losers:
                current_streak[pid] = 0

            upset_gap = pre_loser - pre_winner
            if upset_gap > 0:
                giant_killer_candidates.append({
                    'winner_names': winner_names, 'loser_names': loser_names,
                    'upset_gap': round(upset_gap, 1), 'date': m.get('date'),
                    'score': f"{int(score_a)}-{int(score_b)}" if winner == 'A' else f"{int(score_b)}-{int(score_a)}"
                })

        margin = abs(score_a - score_b)
        if biggest_blowout is None or margin > biggest_blowout['margin']:
            biggest_blowout = {
                'team_a_names': team_a_names, 'team_b_names': team_b_names,
                'score_a': int(score_a), 'score_b': int(score_b),
                'margin': int(margin), 'date': m.get('date')
            }

        momentum = m.get('momentum')
        if momentum and momentum.get('winner_overcame_deficit', 0) > 0:
            winner_names = team_a_names if winner == 'A' else team_b_names
            comeback_candidates.append({
                'winner_names': winner_names, 'deficit_overcome': int(momentum['winner_overcame_deficit']),
                'date': m.get('date')
            })

        ratings_after = m.get('ratings_after') or {}
        for pid, rating in ratings_after.items():
            rating = float(rating)
            rolling_ratings[pid] = rating
            if pid not in peak_rating or rating > peak_rating[pid]['rating']:
                name = None
                for ids, names in ((team_a, team_a_names), (team_b, team_b_names)):
                    if pid in ids:
                        name = names[ids.index(pid)]
                        break
                peak_rating[pid] = {'player_id': pid, 'name': name or pid, 'rating': int(round(rating))}

    giant_killer_candidates.sort(key=lambda g: -g['upset_gap'])
    comeback_candidates.sort(key=lambda c: -c['deficit_overcome'])

    return {
        'longest_win_streak': best_streak if best_streak['player_id'] else None,
        'biggest_blowout': biggest_blowout,
        'peak_ratings': sorted(peak_rating.values(), key=lambda p: -p['rating'])[:10],
        'giant_killer_top5': giant_killer_candidates[:5],
        'comeback_top5': comeback_candidates[:5]
    }


def compute_partner_distribution(player_id, items, top_n=10):
    """For the radar/spider chart: one player's doubles partners, sorted by
    how often they've played together, capped at top_n so the chart stays
    readable. Percentages are based on the total within whatever scope was
    already applied to `items` (a specific group, or every match)."""
    partner_counts = {}
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
        total += 1

    result = []
    for pid, count in partner_counts.items():
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        result.append({
            'partner_id': pid,
            'name': p['name'] if p else pid,
            'matches': count,
            'percentage': round(count / total * 100, 1) if total else 0
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
