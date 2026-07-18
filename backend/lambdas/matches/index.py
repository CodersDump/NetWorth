"""
NetWorth - matches Lambda

Mirrors the inline code in infrastructure/template.yaml (MatchesFunction).
Edit here, then paste into the template's ZipFile block before redeploying.

Routes:
    POST /matches                          -> record a match, updates Elo ratings
    GET  /matches?group_id=X&player_id=Y   -> game log, optionally filtered

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

K_FACTOR = 32  # standard Elo sensitivity constant


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
    player_a_id = body.get('player_a_id')
    player_b_id = body.get('player_b_id')
    score_a = body.get('score_a')
    score_b = body.get('score_b')
    group_id = body.get('group_id')  # optional

    if not player_a_id or not player_b_id or score_a is None or score_b is None:
        return _response(400, {'error': 'player_a_id, player_b_id, score_a, score_b are required'})

    score_a = int(score_a)
    score_b = int(score_b)

    player_a = players_table.get_item(Key={'player_id': player_a_id}).get('Item')
    player_b = players_table.get_item(Key={'player_id': player_b_id}).get('Item')
    if not player_a or not player_b:
        return _response(404, {'error': 'one or both players not found'})

    rating_a = float(player_a.get('rating', 1000))
    rating_b = float(player_b.get('rating', 1000))

    if score_a > score_b:
        actual_a = 1.0
    elif score_a < score_b:
        actual_a = 0.0
    else:
        actual_a = 0.5
    actual_b = 1.0 - actual_a

    expected_a = 1 / (1 + 10 ** ((rating_b - rating_a) / 400))
    expected_b = 1 - expected_a

    new_rating_a = int(round(rating_a + K_FACTOR * (actual_a - expected_a)))
    new_rating_b = int(round(rating_b + K_FACTOR * (actual_b - expected_b)))

    players_table.update_item(
        Key={'player_id': player_a_id},
        UpdateExpression='SET rating = :r',
        ExpressionAttributeValues={':r': new_rating_a}
    )
    players_table.update_item(
        Key={'player_id': player_b_id},
        UpdateExpression='SET rating = :r',
        ExpressionAttributeValues={':r': new_rating_b}
    )

    winner_id = player_a_id if score_a > score_b else (player_b_id if score_b > score_a else None)

    match_id = str(uuid.uuid4())
    item = {
        'match_id': match_id,
        'date': datetime.now(timezone.utc).isoformat(),
        'player_a_id': player_a_id,
        'player_b_id': player_b_id,
        'score_a': score_a,
        'score_b': score_b,
        'winner_id': winner_id or 'tie',
        'rating_a_after': new_rating_a,
        'rating_b_after': new_rating_b,
    }
    if group_id:
        item['group_id'] = group_id

    matches_table.put_item(Item=item)

    return _response(200, item)


def list_matches(event):
    params = event.get('queryStringParameters') or {}
    group_id = params.get('group_id')
    player_id = params.get('player_id')

    items = matches_table.scan().get('Items', [])

    if group_id:
        items = [i for i in items if i.get('group_id') == group_id]
    if player_id:
        items = [i for i in items if i.get('player_a_id') == player_id or i.get('player_b_id') == player_id]

    items.sort(key=lambda i: i.get('date', ''), reverse=True)

    player_cache = {}

    def name_for(pid):
        if pid not in player_cache:
            p = players_table.get_item(Key={'player_id': pid}).get('Item')
            player_cache[pid] = p.get('name') if p else pid
        return player_cache[pid]

    for i in items:
        i['player_a_name'] = name_for(i['player_a_id'])
        i['player_b_name'] = name_for(i['player_b_id'])

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
