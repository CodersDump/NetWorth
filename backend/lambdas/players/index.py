"""
NetWorth - players Lambda (list all, update one, delete one)

Mirrors the inline code in infrastructure/template.yaml (PlayersFunction).
Edit here, then paste into the template's ZipFile block before redeploying.

Routes:
    GET    /players              -> list all players
    PUT    /players/{player_id}  -> update a player's name and/or skill_level
    DELETE /players/{player_id}  -> delete one player

Env vars:
    PLAYERS_TABLE - DynamoDB table name for players
"""
import json
import os
import boto3

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['PLAYERS_TABLE'])

DELETE_KEYWORD = 'DELETE'  # must be typed exactly to confirm a delete


def handler(event, context):
    try:
        method = event.get('httpMethod')
        player_id = (event.get('pathParameters') or {}).get('player_id')

        if method == 'GET' and not player_id:
            return list_players()
        elif method == 'PUT' and player_id:
            return update_player(player_id, event)
        elif method == 'DELETE' and player_id:
            return delete_player(player_id, event)
        return _response(400, {'error': 'unsupported operation'})
    except Exception as e:
        return _response(500, {'error': str(e)})


def list_players():
    items = table.scan().get('Items', [])
    players = [
        {
            'player_id': i['player_id'],
            'name': i['name'],
            'skill_level': i.get('skill_level'),
            'rating': i.get('rating', 1000)
        }
        for i in items
    ]
    return _response(200, {'players': players})


def update_player(player_id, event):
    existing = table.get_item(Key={'player_id': player_id}).get('Item')
    if not existing:
        return _response(404, {'error': 'player not found'})

    body = json.loads(event.get('body') or '{}')
    name = (body.get('name') or '').strip()
    skill_level = body.get('skill_level')

    if not name and not skill_level:
        return _response(400, {'error': 'provide name and/or skill_level to update'})

    update_parts = []
    names = {}
    values = {}
    if name:
        update_parts.append('#n = :n')
        names['#n'] = 'name'
        values[':n'] = name
    if skill_level:
        update_parts.append('skill_level = :s')
        values[':s'] = skill_level

    kwargs = {
        'Key': {'player_id': player_id},
        'UpdateExpression': 'SET ' + ', '.join(update_parts),
        'ExpressionAttributeValues': values
    }
    if names:
        kwargs['ExpressionAttributeNames'] = names

    table.update_item(**kwargs)
    return _response(200, {'player_id': player_id, 'updated': True})


def delete_player(player_id, event):
    body = json.loads(event.get('body') or '{}')
    if body.get('confirm') != DELETE_KEYWORD:
        return _response(400, {'error': f'confirmation required: send confirm: "{DELETE_KEYWORD}" in the request body'})

    existing = table.get_item(Key={'player_id': player_id}).get('Item')
    if not existing:
        return _response(404, {'error': 'player not found'})
    table.delete_item(Key={'player_id': player_id})
    return _response(200, {'deleted': player_id})


def _response(status_code, body_dict):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body_dict, default=str)
    }
