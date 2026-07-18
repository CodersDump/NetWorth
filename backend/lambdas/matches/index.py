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
      "group_id": "optional"
    }

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

    if match_type not in ('singles', 'doubles'):
        return _response(400, {'error': 'match_type must be singles or doubles'})
    expected_size = 1 if match_type == 'singles' else 2
    if len(team_a) != expected_size or len(team_b) != expected_size:
        return _response(400, {'error': f'{match_type} requires {expected_size} player(s) per team'})
    if score_a is None or score_b is None:
        return _response(400, {'error': 'score_a and score_b are required'})
    if set(team_a) & set(team_b):
        return _response(400, {'error': 'a player cannot be on both teams'})

    item = _play_and_log(match_type, team_a, team_b, int(score_a), int(score_b), group_id, None, None)
    if item is None:
        return _response(404, {'error': 'one or more players not found'})
    return _response(200, item)


def _play_and_log(match_type, team_a_ids, team_b_ids, score_a, score_b, group_id, tournament_id, stage):
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
        'winner': winner,
        'ratings_after': new_ratings,
    }
    if group_id:
        item['group_id'] = group_id
    if tournament_id:
        item['tournament_id'] = tournament_id
        item['stage'] = stage

    matches_table.put_item(Item=item)
    return item


def list_matches(event):
    params = event.get('queryStringParameters') or {}
    group_id = params.get('group_id')
    player_id = params.get('player_id')

    items = matches_table.scan().get('Items', [])

    if group_id:
        items = [i for i in items if i.get('group_id') == group_id]
    if player_id:
        items = [i for i in items if player_id in (i.get('team_a') or []) or player_id in (i.get('team_b') or [])]

    items.sort(key=lambda i: i.get('date', ''), reverse=True)

    return _response(200, {'matches': items})


def _response(status_code, body_dict):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body_dict, default=str)
    }
