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
        elif len(parts) == 2 and parts[1] == 'group-score':
            if method == 'POST':
                return record_group_score(parts[0], event)
        elif len(parts) == 2 and parts[1] == 'knockout-score':
            if method == 'POST':
                return record_knockout_score(parts[0], event)

        return _response(404, {'error': 'not found'})
    except Exception as e:
        return _response(500, {'error': str(e)})


# ---------- creation ----------

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

    if not group_id or not name:
        return _response(400, {'error': 'group_id and name are required'})
    if match_type not in ('singles', 'doubles'):
        return _response(400, {'error': 'match_type must be singles or doubles'})
    if best_of not in (1, 3):
        return _response(400, {'error': 'best_of must be 1 or 3'})

    group = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    if not group:
        return _response(404, {'error': 'group not found'})

    member_ids = group.get('member_ids', [])
    players = []
    for pid in member_ids:
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        if p:
            players.append({'player_id': p['player_id'], 'name': p['name']})

    excluded_player = None
    if match_type == 'doubles':
        if len(players) < 4:
            return _response(400, {'error': 'doubles needs at least 4 players'})
        random.shuffle(players)
        if len(players) % 2 == 1:
            excluded_player = players.pop()['name']
        entities = []
        for i in range(0, len(players), 2):
            p1, p2 = players[i], players[i + 1]
            entities.append({
                'player_id': str(uuid.uuid4()),
                'name': f"{p1['name']} & {p2['name']}",
                'members': [p1['player_id'], p2['player_id']]
            })
    else:
        if len(players) < 2:
            return _response(400, {'error': 'group needs at least 2 players'})
        random.shuffle(players)
        entities = [{'player_id': p['player_id'], 'name': p['name'], 'members': [p['player_id']]} for p in players]

    tournament_id = str(uuid.uuid4())
    item = {
        'tournament_id': tournament_id,
        'group_id': group_id,
        'name': name,
        'format': fmt,
        'match_type': match_type,
        'points_to_win': points_to_win,
        'best_of': best_of,
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
    if item.get('status') == 'group_stage':
        item['standings'] = compute_all_standings(item)
    return _response(200, item)


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

def _submit_game(fixture, score_a, score_b, best_of):
    """Append one game's score to a fixture/match. Returns True if the match is now decided."""
    if score_a == score_b:
        raise ValueError('a single game cannot end in a tie')

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
    try:
        decided = _submit_game(fixture, score_a, score_b, best_of)
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
            advance_to_knockout(item)

    tournaments_table.put_item(Item=item)
    return _response(200, item)


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
    score_a = body.get('score_a')
    score_b = body.get('score_b')

    if round_index is None or match_index is None or score_a is None or score_b is None:
        return _response(400, {'error': 'round_index, match_index, score_a, score_b are required'})

    round_index, match_index = int(round_index), int(match_index)
    score_a, score_b = int(score_a), int(score_b)

    item = tournaments_table.get_item(Key={'tournament_id': tournament_id}).get('Item')
    if not item:
        return _response(404, {'error': 'tournament not found'})
    if item.get('status') != 'knockout':
        return _response(400, {'error': 'tournament is not in knockout stage'})

    rounds = item['knockout']['rounds']
    if round_index >= len(rounds):
        return _response(400, {'error': 'invalid round_index'})
    match = rounds[round_index][match_index]
    if match.get('bye'):
        return _response(400, {'error': 'this match is a bye, no score needed'})
    if match['played']:
        return _response(400, {'error': 'this match is already decided'})

    best_of = item.get('best_of', 1)
    try:
        decided = _submit_game(match, score_a, score_b, best_of)
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


def _response(status_code, body_dict):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body_dict, default=str)
    }
