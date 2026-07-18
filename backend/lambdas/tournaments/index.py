"""
NetWorth - tournaments Lambda

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
    num_subgroups = int(body.get('num_subgroups', 2))
    advance_per_group = int(body.get('advance_per_group', 2))

    if not group_id or not name:
        return _response(400, {'error': 'group_id and name are required'})

    group = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    if not group:
        return _response(404, {'error': 'group not found'})

    member_ids = group.get('member_ids', [])
    if len(member_ids) < 2:
        return _response(400, {'error': 'group needs at least 2 players'})

    members = []
    for pid in member_ids:
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        if p:
            members.append({'player_id': p['player_id'], 'name': p['name']})

    random.shuffle(members)

    tournament_id = str(uuid.uuid4())
    item = {
        'tournament_id': tournament_id,
        'group_id': group_id,
        'name': name,
        'format': fmt,
        'created_at': datetime.now(timezone.utc).isoformat(),
    }

    if fmt == 'groups_then_knockout':
        if num_subgroups < 2 or num_subgroups > len(members):
            return _response(400, {'error': 'num_subgroups must be between 2 and the number of players'})
        subgroup_names = list(ascii_uppercase[:num_subgroups])
        subgroups = {n: {'members': [], 'fixtures': []} for n in subgroup_names}
        for idx, member in enumerate(members):
            subgroups[subgroup_names[idx % num_subgroups]]['members'].append(member)
        for sg in subgroups.values():
            sg['fixtures'] = build_round_robin(sg['members'])
        item['subgroups'] = subgroups
        item['advance_per_group'] = advance_per_group
        item['status'] = 'group_stage'
    else:
        item['knockout'] = {'rounds': [build_knockout_round(members)]}
        item['status'] = 'knockout'

    tournaments_table.put_item(Item=item)
    return _response(200, item)


def build_round_robin(members):
    fixtures = []
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            fixtures.append({
                'fixture_id': str(uuid.uuid4()),
                'player_a': members[i],
                'player_b': members[j],
                'played': False,
                'score_a': None,
                'score_b': None,
                'winner_id': None
            })
    return fixtures


def build_knockout_round(members):
    n = len(members)
    next_pow2 = 1
    while next_pow2 < n:
        next_pow2 *= 2
    byes_needed = next_pow2 - n

    matches = []
    i = 0
    byes_given = 0
    while i < len(members):
        if byes_given < byes_needed:
            matches.append(_bye_match(members[i]))
            byes_given += 1
            i += 1
        else:
            player_a = members[i]
            player_b = members[i + 1] if i + 1 < len(members) else None
            if player_b is None:
                matches.append(_bye_match(player_a))
            else:
                matches.append({
                    'match_id': str(uuid.uuid4()),
                    'player_a': player_a,
                    'player_b': player_b,
                    'played': False,
                    'winner_id': None,
                    'score_a': None,
                    'score_b': None
                })
            i += 2
    return matches


def _bye_match(player):
    return {
        'match_id': str(uuid.uuid4()),
        'player_a': player,
        'player_b': None,
        'played': True,
        'winner_id': player['player_id'],
        'bye': True,
        'score_a': None,
        'score_b': None
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


def compute_standings(fixtures, members):
    stats = {
        m['player_id']: {
            'player_id': m['player_id'], 'name': m['name'],
            'wins': 0, 'losses': 0, 'points_won': 0, 'points_lost': 0
        }
        for m in members
    }
    for f in fixtures:
        if not f['played']:
            continue
        a_id = f['player_a']['player_id']
        b_id = f['player_b']['player_id']
        stats[a_id]['points_won'] += f['score_a']
        stats[a_id]['points_lost'] += f['score_b']
        stats[b_id]['points_won'] += f['score_b']
        stats[b_id]['points_lost'] += f['score_a']
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

    winner_id = fixture['player_a']['player_id'] if score_a > score_b else (
        fixture['player_b']['player_id'] if score_b > score_a else None)
    fixture['score_a'] = score_a
    fixture['score_b'] = score_b
    fixture['played'] = True
    fixture['winner_id'] = winner_id or 'tie'

    update_elo_and_log(fixture['player_a']['player_id'], fixture['player_b']['player_id'],
                        score_a, score_b, item['group_id'], tournament_id, 'group')

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
        for rank, s in enumerate(standings[:advance_per_group]):
            qualifiers.append({'player_id': s['player_id'], 'name': s['name'], 'subgroup': name, 'rank': rank})

    random.shuffle(qualifiers)
    # best-effort: avoid same-subgroup rematches in round 1 where possible
    for i in range(0, len(qualifiers) - 1, 2):
        if qualifiers[i]['subgroup'] == qualifiers[i + 1]['subgroup']:
            for j in range(i + 2, len(qualifiers)):
                if qualifiers[j]['subgroup'] != qualifiers[i]['subgroup']:
                    qualifiers[i + 1], qualifiers[j] = qualifiers[j], qualifiers[i + 1]
                    break

    members = [{'player_id': q['player_id'], 'name': q['name']} for q in qualifiers]
    item['knockout'] = {'rounds': [build_knockout_round(members)]}
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
    if score_a == score_b:
        return _response(400, {'error': 'score cannot be a tie in knockout stage'})

    winner_id = match['player_a']['player_id'] if score_a > score_b else match['player_b']['player_id']
    match['score_a'] = score_a
    match['score_b'] = score_b
    match['played'] = True
    match['winner_id'] = winner_id

    update_elo_and_log(match['player_a']['player_id'], match['player_b']['player_id'],
                        score_a, score_b, item['group_id'], tournament_id, 'knockout')

    current_round = rounds[round_index]
    if all(m['played'] for m in current_round):
        if len(current_round) == 1:
            item['status'] = 'completed'
            item['champion_id'] = current_round[0]['winner_id']
        else:
            winners = []
            for m in current_round:
                pid = m['winner_id']
                name = m['player_a']['name'] if m['player_a']['player_id'] == pid else m['player_b']['name']
                winners.append({'player_id': pid, 'name': name})
            rounds.append(build_knockout_round(winners))

    tournaments_table.put_item(Item=item)
    return _response(200, item)


# ---------- shared Elo + game-log write ----------

def update_elo_and_log(player_a_id, player_b_id, score_a, score_b, group_id, tournament_id, stage):
    player_a = players_table.get_item(Key={'player_id': player_a_id}).get('Item')
    player_b = players_table.get_item(Key={'player_id': player_b_id}).get('Item')
    if not player_a or not player_b:
        return

    rating_a = float(player_a.get('rating', 1000))
    rating_b = float(player_b.get('rating', 1000))

    actual_a = 1.0 if score_a > score_b else (0.0 if score_a < score_b else 0.5)
    actual_b = 1.0 - actual_a

    expected_a = 1 / (1 + 10 ** ((rating_b - rating_a) / 400))
    expected_b = 1 - expected_a

    new_rating_a = int(round(rating_a + K_FACTOR * (actual_a - expected_a)))
    new_rating_b = int(round(rating_b + K_FACTOR * (actual_b - expected_b)))

    players_table.update_item(Key={'player_id': player_a_id}, UpdateExpression='SET rating = :r',
                               ExpressionAttributeValues={':r': new_rating_a})
    players_table.update_item(Key={'player_id': player_b_id}, UpdateExpression='SET rating = :r',
                               ExpressionAttributeValues={':r': new_rating_b})

    matches_table.put_item(Item={
        'match_id': str(uuid.uuid4()),
        'date': datetime.now(timezone.utc).isoformat(),
        'player_a_id': player_a_id,
        'player_b_id': player_b_id,
        'score_a': score_a,
        'score_b': score_b,
        'winner_id': player_a_id if score_a > score_b else (player_b_id if score_b > score_a else 'tie'),
        'rating_a_after': new_rating_a,
        'rating_b_after': new_rating_b,
        'group_id': group_id,
        'tournament_id': tournament_id,
        'stage': stage
    })


def _response(status_code, body_dict):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body_dict, default=str)
    }
