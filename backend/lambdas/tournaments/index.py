"""
NetWorth - tournaments Lambda (singles or doubles)

Routes (via API Gateway {proxy+} on /tournaments):
    POST /tournaments                              -> create tournament
    GET  /tournaments?group_id=X                   -> list tournaments
    GET  /tournaments/{tournament_id}               -> get tournament detail (+ standings)
    POST /tournaments/{tournament_id}/group-score   -> record a group-stage fixture score
    POST /tournaments/{tournament_id}/knockout-score -> record a knockout match score

Formats:
    "knockout"            - random single-elimination bracket
    "groups_then_knockout" - random subgroups (round robin), top N per group
                             advance to a knockout bracket

Body extra for creation:
    "match_type": "singles" | "doubles"  (default "singles")
    For doubles, group members are randomly paired into 2-player teams
    before bracket/group generation. Each "entity" in the bracket has a
    synthetic id, a display name ("Alice & Bob"), and a "members" list of
    the underlying player_id(s) used for Elo updates.

Env vars:
    TOURNAMENTS_TABLE, GROUPS_TABLE, PLAYERS_TABLE, MATCHES_TABLE
"""
import json
import os
import uuid
import random
import boto3
from datetime import datetime, timezone
from string import ascii_uppercase

dynamodb = boto3.resource('dynamodb')
tournaments_table = dynamodb.Table(os.environ['TOURNAMENTS_TABLE'])
groups_table = dynamodb.Table(os.environ['GROUPS_TABLE'])
players_table = dynamodb.Table(os.environ['PLAYERS_TABLE'])
matches_table = dynamodb.Table(os.environ['MATCHES_TABLE'])

K_FACTOR = 32
CONFIRMATION_CODE = 'Matchpoint-Falcon-77'  # private - never shown in the UI; change this if it's ever exposed


def _is_valid_completed_game(score_a, score_b, target):
    """Same BWF-style rule as the standalone matches Lambda: win by 2 at
    target+ points, hard cap at target+9 where reaching it wins outright."""
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
        proxy = (event.get('pathParameters') or {}).get('proxy', '')
        parts = [p for p in proxy.split('/') if p] if proxy else []

        if not parts:
            if method == 'POST':
                return create_tournament(event)
            if method == 'GET':
                return list_tournaments(event)
        elif len(parts) == 1:
            if method == 'GET':
                return get_tournament(parts[0])
            elif method == 'DELETE':
                return delete_tournament(parts[0], event)
        elif len(parts) == 2 and parts[1] == 'group-score':
            if method == 'POST':
                return record_group_score(parts[0], event)
        elif len(parts) == 2 and parts[1] == 'knockout-score':
            if method == 'POST':
                return record_knockout_score(parts[0], event)
        elif len(parts) == 2 and parts[1] == 'substitute':
            if method == 'POST':
                return substitute_player(parts[0], event)

        return _response(404, {'error': 'not found'})
    except Exception as e:
        return _response(500, {'error': str(e)})


# ---------- creation ----------

def get_matches_played_counts():
    """Scan the Matches table once and count appearances per player_id,
    across both standalone and tournament matches."""
    items = matches_table.scan().get('Items', [])
    counts = {}
    for m in items:
        for pid in (m.get('team_a') or []) + (m.get('team_b') or []):
            counts[pid] = counts.get(pid, 0) + 1
    return counts


EXPERIENCED_THRESHOLD = 5  # matches played to be treated as "experienced" for seeding


def seeded_order(players, matches_played):
    """Sort experienced players by rating (desc); interleave newer players
    evenly through that order rather than leaving them clustered together,
    since a new player's default 1000 rating isn't a reliable skill signal
    yet."""
    experienced = [p for p in players if matches_played.get(p['player_id'], 0) >= EXPERIENCED_THRESHOLD]
    newer = [p for p in players if matches_played.get(p['player_id'], 0) < EXPERIENCED_THRESHOLD]
    experienced.sort(key=lambda p: -float(p.get('rating', 1000)))
    random.shuffle(newer)

    if not newer:
        return experienced
    if not experienced:
        return newer

    result = []
    step = len(experienced) / (len(newer) + 1)
    next_insert_at = step
    newer_idx = 0
    for i, p in enumerate(experienced):
        result.append(p)
        if newer_idx < len(newer) and (i + 1) >= next_insert_at:
            result.append(newer[newer_idx])
            newer_idx += 1
            next_insert_at += step
    while newer_idx < len(newer):
        result.append(newer[newer_idx])
        newer_idx += 1
    return result


def pair_for_balance(ordered_players):
    """Given a skill-ordered list, pair strongest with weakest (snake
    pairing) so doubles teams end up roughly balanced against each other,
    rather than stacking all the strong players together."""
    n = len(ordered_players)
    pairs = []
    i, j = 0, n - 1
    while i < j:
        pairs.append((ordered_players[i], ordered_players[j]))
        i += 1
        j -= 1
    leftover = ordered_players[i] if i == j else None
    return pairs, leftover


def create_tournament(event):
    body = json.loads(event.get('body') or '{}')
    group_id = body.get('group_id')
    name = (body.get('name') or '').strip()
    fmt = body.get('format', 'knockout')
    match_type = body.get('match_type', 'singles')
    points_to_win = int(body.get('points_to_win', 21))
    best_of = int(body.get('best_of', 1))
    num_subgroups = int(body.get('num_subgroups', 2))
    advance_per_group = int(body.get('advance_per_group', 2))
    pairing_mode = body.get('pairing_mode', 'random')  # 'random' | 'seeded' | 'manual'
    manual_teams = body.get('manual_teams')  # optional: [["pid1","pid2"], ["pid3","pid4"], ...]

    if not group_id or not name:
        return _response(400, {'error': 'group_id and name are required'})
    if match_type not in ('singles', 'doubles'):
        return _response(400, {'error': 'match_type must be singles or doubles'})
    if best_of not in (1, 3):
        return _response(400, {'error': 'best_of must be 1 or 3'})
    if pairing_mode not in ('random', 'seeded', 'manual'):
        return _response(400, {'error': 'pairing_mode must be random, seeded, or manual'})

    group = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    if not group:
        return _response(404, {'error': 'group not found'})

    excluded_player = None

    if manual_teams:
        expected_size = 1 if match_type == 'singles' else 2
        entities = []
        seen_ids = set()
        for team_ids in manual_teams:
            if len(team_ids) != expected_size:
                return _response(400, {'error': f'each manual team needs exactly {expected_size} player(s) for {match_type}'})
            team_players = []
            for pid in team_ids:
                if pid in seen_ids:
                    return _response(400, {'error': 'a player appears in more than one manual team'})
                seen_ids.add(pid)
                p = players_table.get_item(Key={'player_id': pid}).get('Item')
                if not p:
                    return _response(404, {'error': f'player not found: {pid}'})
                team_players.append(p)
            name_str = ' & '.join(p['name'] for p in team_players)
            entities.append({
                'player_id': str(uuid.uuid4()),
                'name': name_str,
                'members': [p['player_id'] for p in team_players]
            })
        pairing_mode = 'manual'
    else:
        member_ids = group.get('member_ids', [])
        players = []
        for pid in member_ids:
            p = players_table.get_item(Key={'player_id': pid}).get('Item')
            if p:
                players.append({'player_id': p['player_id'], 'name': p['name'], 'rating': p.get('rating', 1000)})

        if pairing_mode == 'seeded':
            matches_played = get_matches_played_counts()
            ordered = seeded_order(players, matches_played)
        else:
            ordered = players[:]
            random.shuffle(ordered)

        if match_type == 'doubles':
            if len(ordered) < 4:
                return _response(400, {'error': 'doubles needs at least 4 players'})
            if pairing_mode == 'seeded':
                pairs, leftover = pair_for_balance(ordered)
                if leftover:
                    excluded_player = leftover['name']
            else:
                if len(ordered) % 2 == 1:
                    excluded_player = ordered.pop()['name']
                pairs = [(ordered[i], ordered[i + 1]) for i in range(0, len(ordered), 2)]
            entities = []
            for p1, p2 in pairs:
                entities.append({
                    'player_id': str(uuid.uuid4()),
                    'name': f"{p1['name']} & {p2['name']}",
                    'members': [p1['player_id'], p2['player_id']]
                })
        else:
            if len(ordered) < 2:
                return _response(400, {'error': 'group needs at least 2 players'})
            entities = [{'player_id': p['player_id'], 'name': p['name'], 'members': [p['player_id']]} for p in ordered]

    tournament_id = str(uuid.uuid4())
    item = {
        'tournament_id': tournament_id,
        'group_id': group_id,
        'name': name,
        'format': fmt,
        'match_type': match_type,
        'points_to_win': points_to_win,
        'best_of': best_of,
        'pairing_mode': pairing_mode,
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    if excluded_player:
        item['excluded_player'] = excluded_player

    if fmt == 'groups_then_knockout':
        if num_subgroups < 2 or num_subgroups > len(entities):
            return _response(400, {'error': 'num_subgroups must be between 2 and the number of teams/players'})
        subgroup_names = list(ascii_uppercase[:num_subgroups])
        subgroups = {n: {'members': [], 'fixtures': []} for n in subgroup_names}
        for idx, entity in enumerate(entities):
            subgroups[subgroup_names[idx % num_subgroups]]['members'].append(entity)
        for sg in subgroups.values():
            sg['fixtures'] = build_round_robin(sg['members'])
        item['subgroups'] = subgroups
        item['advance_per_group'] = advance_per_group
        item['status'] = 'group_stage'
    else:
        item['knockout'] = {'rounds': [build_knockout_round(entities)]}
        item['status'] = 'knockout'

    tournaments_table.put_item(Item=item)
    return _response(200, item)


def build_round_robin(entities):
    fixtures = []
    for i in range(len(entities)):
        for j in range(i + 1, len(entities)):
            fixtures.append({
                'fixture_id': str(uuid.uuid4()),
                'player_a': entities[i],
                'player_b': entities[j],
                'games': [],
                'games_won_a': 0,
                'games_won_b': 0,
                'played': False,
                'winner_id': None
            })
    return fixtures


def build_knockout_round(entities):
    n = len(entities)
    next_pow2 = 1
    while next_pow2 < n:
        next_pow2 *= 2
    byes_needed = next_pow2 - n

    matches = []
    i = 0
    byes_given = 0
    while i < len(entities):
        if byes_given < byes_needed:
            matches.append(_bye_match(entities[i]))
            byes_given += 1
            i += 1
        else:
            entity_a = entities[i]
            entity_b = entities[i + 1] if i + 1 < len(entities) else None
            if entity_b is None:
                matches.append(_bye_match(entity_a))
            else:
                matches.append({
                    'match_id': str(uuid.uuid4()),
                    'player_a': entity_a,
                    'player_b': entity_b,
                    'games': [],
                    'games_won_a': 0,
                    'games_won_b': 0,
                    'played': False,
                    'winner_id': None
                })
            i += 2
    return matches


def _bye_match(entity):
    return {
        'match_id': str(uuid.uuid4()),
        'player_a': entity,
        'player_b': None,
        'games': [],
        'games_won_a': 0,
        'games_won_b': 0,
        'played': True,
        'winner_id': entity['player_id'],
        'bye': True
    }


# ---------- reads ----------

def list_tournaments(event):
    params = event.get('queryStringParameters') or {}
    group_id = params.get('group_id')
    items = tournaments_table.scan().get('Items', [])
    if group_id:
        items = [i for i in items if i.get('group_id') == group_id]
    result = [
        {
            'tournament_id': i['tournament_id'],
            'name': i['name'],
            'group_id': i['group_id'],
            'format': i['format'],
            'match_type': i.get('match_type', 'singles'),
            'points_to_win': i.get('points_to_win', 21),
            'best_of': i.get('best_of', 1),
            'status': i['status'],
            'created_at': i['created_at']
        }
        for i in items
    ]
    result.sort(key=lambda i: i['created_at'], reverse=True)
    return _response(200, {'tournaments': result})


def get_tournament(tournament_id):
    item = tournaments_table.get_item(Key={'tournament_id': tournament_id}).get('Item')
    if not item:
        return _response(404, {'error': 'tournament not found'})
    if 'subgroups' in item:
        item['standings'] = compute_all_standings(item)
    return _response(200, item)


def recompute_all_ratings():
    """Elo is path-dependent - each match's rating change depends on the
    ratings at that exact moment, which depend on everything before it.
    Simply subtracting a delta when a match is deleted isn't mathematically
    safe if anything happened after it. The only fully correct fix: reset
    everyone to 1000 and replay every remaining match in chronological
    order, recomputing from scratch."""
    players = players_table.scan().get('Items', [])
    current_ratings = {p['player_id']: 1000.0 for p in players}

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
        delta_a = K_FACTOR * (actual_a - expected_a)
        delta_b = K_FACTOR * (actual_b - expected_b)

        for pid in team_a:
            current_ratings[pid] = current_ratings.get(pid, 1000.0) + delta_a
        for pid in team_b:
            current_ratings[pid] = current_ratings.get(pid, 1000.0) + delta_b

    for pid, rating in current_ratings.items():
        players_table.update_item(Key={'player_id': pid}, UpdateExpression='SET rating = :r',
                                   ExpressionAttributeValues={':r': int(round(rating))})


def delete_tournament(tournament_id, event):
    """Deletes this tournament AND every match record tagged with its
    tournament_id (e.g. from a test tournament you're cleaning up), then
    recomputes every player's rating from scratch off the remaining match
    history - see recompute_all_ratings() for why a simple delta-subtract
    isn't safe."""
    body = json.loads(event.get('body') or '{}')
    if body.get('confirm') != CONFIRMATION_CODE:
        return _response(400, {'error': "confirmation code is missing or incorrect"})

    existing = tournaments_table.get_item(Key={'tournament_id': tournament_id}).get('Item')
    if not existing:
        return _response(404, {'error': 'tournament not found'})

    related_matches = matches_table.scan().get('Items', [])
    deleted_match_count = 0
    for m in related_matches:
        if m.get('tournament_id') == tournament_id:
            matches_table.delete_item(Key={'match_id': m['match_id']})
            deleted_match_count += 1

    tournaments_table.delete_item(Key={'tournament_id': tournament_id})

    if deleted_match_count > 0:
        recompute_all_ratings()

    return _response(200, {
        'deleted': tournament_id,
        'name': existing.get('name'),
        'matches_deleted': deleted_match_count,
        'note': 'Player ratings were recomputed from scratch using the remaining match history.' if deleted_match_count > 0 else 'No matches were attached to this tournament.'
    })


def compute_standings(fixtures, entities):
    stats = {
        e['player_id']: {
            'player_id': e['player_id'], 'name': e['name'],
            'wins': 0, 'losses': 0, 'points_won': 0, 'points_lost': 0
        }
        for e in entities
    }
    for f in fixtures:
        if not f['played'] or f.get('bye'):
            continue
        a_id = f['player_a']['player_id']
        b_id = f['player_b']['player_id']
        total_a = sum(g['score_a'] for g in f.get('games', []))
        total_b = sum(g['score_b'] for g in f.get('games', []))
        stats[a_id]['points_won'] += total_a
        stats[a_id]['points_lost'] += total_b
        stats[b_id]['points_won'] += total_b
        stats[b_id]['points_lost'] += total_a
        if f['winner_id'] == a_id:
            stats[a_id]['wins'] += 1
            stats[b_id]['losses'] += 1
        elif f['winner_id'] == b_id:
            stats[b_id]['wins'] += 1
            stats[a_id]['losses'] += 1
    standings = list(stats.values())
    for s in standings:
        s['point_diff'] = s['points_won'] - s['points_lost']
    standings.sort(key=lambda s: (-s['wins'], -s['point_diff']))
    return standings


def compute_all_standings(item):
    return {name: compute_standings(sg['fixtures'], sg['members']) for name, sg in item.get('subgroups', {}).items()}


# ---------- group stage scoring ----------

def _submit_game(fixture, score_a, score_b, best_of, target=21):
    """Append one game's score to a fixture/match. Returns True if the match is now decided."""
    if not _is_valid_completed_game(score_a, score_b, target):
        cap = target + 9
        raise ValueError(f'invalid game score: must be won by 2 at {target}+ points, or reach the hard cap of {cap}')

    fixture['games'].append({'score_a': score_a, 'score_b': score_b})
    if score_a > score_b:
        fixture['games_won_a'] += 1
    else:
        fixture['games_won_b'] += 1

    needed_wins = (best_of // 2) + 1
    if fixture['games_won_a'] >= needed_wins or fixture['games_won_b'] >= needed_wins:
        a_id = fixture['player_a']['player_id']
        b_id = fixture['player_b']['player_id']
        fixture['winner_id'] = a_id if fixture['games_won_a'] > fixture['games_won_b'] else b_id
        fixture['played'] = True
        return True
    return False


def record_group_score(tournament_id, event):
    body = json.loads(event.get('body') or '{}')
    subgroup = body.get('subgroup')
    fixture_id = body.get('fixture_id')
    score_a = body.get('score_a')
    score_b = body.get('score_b')

    if not subgroup or not fixture_id or score_a is None or score_b is None:
        return _response(400, {'error': 'subgroup, fixture_id, score_a, score_b are required'})

    score_a, score_b = int(score_a), int(score_b)

    item = tournaments_table.get_item(Key={'tournament_id': tournament_id}).get('Item')
    if not item:
        return _response(404, {'error': 'tournament not found'})
    if item.get('status') != 'group_stage':
        return _response(400, {'error': 'tournament is not in group stage'})

    sg = item['subgroups'].get(subgroup)
    if not sg:
        return _response(404, {'error': 'subgroup not found'})

    fixture = next((f for f in sg['fixtures'] if f['fixture_id'] == fixture_id), None)
    if not fixture:
        return _response(404, {'error': 'fixture not found'})
    if fixture['played']:
        return _response(400, {'error': 'this fixture is already decided'})

    best_of = item.get('best_of', 1)
    target = item.get('points_to_win', 21)
    try:
        decided = _submit_game(fixture, score_a, score_b, best_of, target)
    except ValueError as e:
        return _response(400, {'error': str(e)})

    if decided:
        total_a = sum(g['score_a'] for g in fixture['games'])
        total_b = sum(g['score_b'] for g in fixture['games'])
        winner = 'A' if fixture['games_won_a'] > fixture['games_won_b'] else 'B'
        update_elo_and_log(item.get('match_type', 'singles'), fixture['player_a'], fixture['player_b'],
                            total_a, total_b, item['group_id'], tournament_id, 'group',
                            winner_override=winner, games=fixture['games'])

        all_played = all(f['played'] for sg2 in item['subgroups'].values() for f in sg2['fixtures'])
        if all_played:
            if not inject_tiebreakers_if_needed(item):
                advance_to_knockout(item)

    tournaments_table.put_item(Item=item)
    return _response(200, item)


def inject_tiebreakers_if_needed(item):
    """Checks each subgroup for a genuine tie (same wins AND point_diff) at
    the qualifying boundary. If found, appends an unplayed tiebreaker
    fixture between exactly those two teams instead of advancing to
    knockout. Returns True if any subgroup still needs a tiebreaker played
    (meaning advancement should wait)."""
    advance_n = item.get('advance_per_group', 2)
    needs_tiebreaker = False

    for sg in item['subgroups'].values():
        # already has one pending (unplayed) - waiting on it, don't add another
        if any(f.get('tiebreaker') and not f['played'] for f in sg['fixtures']):
            needs_tiebreaker = True
            continue

        standings = compute_standings(sg['fixtures'], sg['members'])
        if len(standings) <= advance_n:
            continue

        boundary_a = standings[advance_n - 1]
        boundary_b = standings[advance_n]
        tied = (boundary_a['wins'] == boundary_b['wins'] and boundary_a['point_diff'] == boundary_b['point_diff'])
        if not tied:
            continue

        pair_key = {boundary_a['player_id'], boundary_b['player_id']}
        already_resolved = any(
            f.get('tiebreaker') and f['played'] and
            {f['player_a']['player_id'], f['player_b']['player_id']} == pair_key
            for f in sg['fixtures']
        )
        if already_resolved:
            continue

        entity_by_id = {e['player_id']: e for e in sg['members']}
        sg['fixtures'].append({
            'fixture_id': str(uuid.uuid4()),
            'player_a': entity_by_id[boundary_a['player_id']],
            'player_b': entity_by_id[boundary_b['player_id']],
            'games': [],
            'games_won_a': 0,
            'games_won_b': 0,
            'played': False,
            'winner_id': None,
            'tiebreaker': True
        })
        needs_tiebreaker = True

    return needs_tiebreaker


def advance_to_knockout(item):
    advance_per_group = item.get('advance_per_group', 2)
    qualifiers = []
    for name, sg in item['subgroups'].items():
        standings = compute_standings(sg['fixtures'], sg['members'])
        entity_by_id = {e['player_id']: e for e in sg['members']}
        for rank, s in enumerate(standings[:advance_per_group]):
            entity = entity_by_id[s['player_id']]
            qualifiers.append({'entity': entity, 'subgroup': name})

    random.shuffle(qualifiers)
    for i in range(0, len(qualifiers) - 1, 2):
        if qualifiers[i]['subgroup'] == qualifiers[i + 1]['subgroup']:
            for j in range(i + 2, len(qualifiers)):
                if qualifiers[j]['subgroup'] != qualifiers[i]['subgroup']:
                    qualifiers[i + 1], qualifiers[j] = qualifiers[j], qualifiers[i + 1]
                    break

    entities = [q['entity'] for q in qualifiers]
    item['knockout'] = {'rounds': [build_knockout_round(entities)]}
    item['status'] = 'knockout'


# ---------- knockout scoring ----------

def record_knockout_score(tournament_id, event):
    body = json.loads(event.get('body') or '{}')
    round_index = body.get('round_index')
    match_index = body.get('match_index')
    third_place = bool(body.get('third_place'))
    score_a = body.get('score_a')
    score_b = body.get('score_b')

    if score_a is None or score_b is None:
        return _response(400, {'error': 'score_a and score_b are required'})
    if not third_place and (round_index is None or match_index is None):
        return _response(400, {'error': 'round_index and match_index are required (or set third_place: true)'})

    score_a, score_b = int(score_a), int(score_b)

    item = tournaments_table.get_item(Key={'tournament_id': tournament_id}).get('Item')
    if not item:
        return _response(404, {'error': 'tournament not found'})
    if item.get('status') not in ('knockout', 'completed'):
        return _response(400, {'error': 'tournament is not in knockout stage'})

    best_of = item.get('best_of', 1)
    target = item.get('points_to_win', 21)

    if third_place:
        match = item['knockout'].get('third_place_match')
        if not match:
            return _response(404, {'error': 'no third place match exists yet for this tournament'})
        if match['played']:
            return _response(400, {'error': 'this match is already decided'})

        try:
            decided = _submit_game(match, score_a, score_b, best_of, target)
        except ValueError as e:
            return _response(400, {'error': str(e)})

        if decided:
            total_a = sum(g['score_a'] for g in match['games'])
            total_b = sum(g['score_b'] for g in match['games'])
            winner = 'A' if match['games_won_a'] > match['games_won_b'] else 'B'
            update_elo_and_log(item.get('match_type', 'singles'), match['player_a'], match['player_b'],
                                total_a, total_b, item['group_id'], tournament_id, 'third_place',
                                winner_override=winner, games=match['games'])

        tournaments_table.put_item(Item=item)
        return _response(200, item)

    round_index, match_index = int(round_index), int(match_index)
    rounds = item['knockout']['rounds']
    if round_index >= len(rounds):
        return _response(400, {'error': 'invalid round_index'})
    match = rounds[round_index][match_index]
    if match.get('bye'):
        return _response(400, {'error': 'this match is a bye, no score needed'})
    if match['played']:
        return _response(400, {'error': 'this match is already decided'})

    try:
        decided = _submit_game(match, score_a, score_b, best_of, target)
    except ValueError as e:
        return _response(400, {'error': str(e)})

    if decided:
        total_a = sum(g['score_a'] for g in match['games'])
        total_b = sum(g['score_b'] for g in match['games'])
        winner = 'A' if match['games_won_a'] > match['games_won_b'] else 'B'
        update_elo_and_log(item.get('match_type', 'singles'), match['player_a'], match['player_b'],
                            total_a, total_b, item['group_id'], tournament_id, 'knockout',
                            winner_override=winner, games=match['games'])

        current_round = rounds[round_index]
        if all(m['played'] for m in current_round):
            if len(current_round) == 1:
                item['status'] = 'completed'
                item['champion_id'] = current_round[0]['winner_id']
            else:
                winners = []
                for m in current_round:
                    pid = m['winner_id']
                    entity = m['player_a'] if m['player_a']['player_id'] == pid else m['player_b']
                    winners.append(entity)
                rounds.append(build_knockout_round(winners))

                # If this round had exactly 2 matches, it was the semifinal
                # stage feeding a single-match final - set up a 3rd place
                # match between the two losers.
                if len(current_round) == 2 and 'third_place_match' not in item['knockout']:
                    losers = []
                    for m in current_round:
                        pid = m['winner_id']
                        loser_entity = m['player_b'] if m['player_a']['player_id'] == pid else m['player_a']
                        losers.append(loser_entity)
                    item['knockout']['third_place_match'] = {
                        'match_id': str(uuid.uuid4()),
                        'player_a': losers[0],
                        'player_b': losers[1],
                        'games': [],
                        'games_won_a': 0,
                        'games_won_b': 0,
                        'played': False,
                        'winner_id': None
                    }

    tournaments_table.put_item(Item=item)
    return _response(200, item)


# ---------- shared Elo + game-log write (entity = single player or doubles team) ----------

def update_elo_and_log(match_type, entity_a, entity_b, score_a, score_b, group_id, tournament_id, stage,
                        winner_override=None, games=None):
    team_a_ids = entity_a.get('members', [entity_a['player_id']])
    team_b_ids = entity_b.get('members', [entity_b['player_id']])

    team_a_players = [players_table.get_item(Key={'player_id': pid}).get('Item') for pid in team_a_ids]
    team_b_players = [players_table.get_item(Key={'player_id': pid}).get('Item') for pid in team_b_ids]
    if any(p is None for p in team_a_players) or any(p is None for p in team_b_players):
        return

    rating_a_avg = sum(float(p.get('rating', 1000)) for p in team_a_players) / len(team_a_players)
    rating_b_avg = sum(float(p.get('rating', 1000)) for p in team_b_players) / len(team_b_players)

    if winner_override == 'A':
        actual_a = 1.0
    elif winner_override == 'B':
        actual_a = 0.0
    else:
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

    winner = winner_override if winner_override else ('A' if score_a > score_b else ('B' if score_b > score_a else 'tie'))

    log_item = {
        'match_id': str(uuid.uuid4()),
        'date': datetime.now(timezone.utc).isoformat(),
        'match_type': match_type,
        'team_a': team_a_ids,
        'team_b': team_b_ids,
        'team_a_names': [p['name'] for p in team_a_players],
        'team_b_names': [p['name'] for p in team_b_players],
        'score_a': score_a,
        'score_b': score_b,
        'winner': winner,
        'ratings_after': new_ratings,
        'group_id': group_id,
        'tournament_id': tournament_id,
        'stage': stage
    }
    if games:
        log_item['games'] = games
    matches_table.put_item(Item=log_item)


def substitute_player(tournament_id, event):
    """Swap a player out of a team for all of that team's FUTURE (unplayed)
    matches in this tournament. Already-played fixtures/matches keep their
    original recorded entity untouched, so past results and Elo stay
    attributed to whoever actually played them."""
    body = json.loads(event.get('body') or '{}')
    team_entity_id = body.get('team_entity_id')
    old_player_id = body.get('old_player_id')
    new_player_id = body.get('new_player_id')

    if not team_entity_id or not old_player_id or not new_player_id:
        return _response(400, {'error': 'team_entity_id, old_player_id, new_player_id are required'})

    item = tournaments_table.get_item(Key={'tournament_id': tournament_id}).get('Item')
    if not item:
        return _response(404, {'error': 'tournament not found'})

    new_player = players_table.get_item(Key={'player_id': new_player_id}).get('Item')
    if not new_player:
        return _response(404, {'error': 'new player not found'})

    def apply_substitution(entity):
        if not entity or entity.get('player_id') != team_entity_id:
            return False
        members = entity.get('members', [])
        if old_player_id not in members:
            return False
        idx = members.index(old_player_id)
        members[idx] = new_player_id
        if len(members) == 2:
            names = []
            for pid in members:
                if pid == new_player_id:
                    names.append(new_player['name'])
                else:
                    p = players_table.get_item(Key={'player_id': pid}).get('Item')
                    names.append(p['name'] if p else pid)
            entity['name'] = ' & '.join(names)
        else:
            entity['name'] = new_player['name']
        return True

    updated_any = False

    for sg in item.get('subgroups', {}).values():
        for f in sg['fixtures']:
            if not f['played']:
                updated_any |= apply_substitution(f['player_a'])
                updated_any |= apply_substitution(f['player_b'])
        for e in sg['members']:
            updated_any |= apply_substitution(e)

    if 'knockout' in item:
        for round_ in item['knockout'].get('rounds', []):
            for m in round_:
                if not m['played']:
                    updated_any |= apply_substitution(m['player_a'])
                    updated_any |= apply_substitution(m['player_b'])
        tp = item['knockout'].get('third_place_match')
        if tp and not tp['played']:
            updated_any |= apply_substitution(tp['player_a'])
            updated_any |= apply_substitution(tp['player_b'])

    if not updated_any:
        return _response(404, {'error': 'no unplayed matches found for that team/player combination'})

    substitutions = item.setdefault('substitutions', [])
    substitutions.append({
        'team_entity_id': team_entity_id,
        'old_player_id': old_player_id,
        'new_player_id': new_player_id,
        'new_player_name': new_player['name'],
        'timestamp': datetime.now(timezone.utc).isoformat()
    })

    tournaments_table.put_item(Item=item)
    return _response(200, item)


def _response(status_code, body_dict):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body_dict, default=str)
    }
