"""
NetWorth - groups Lambda

Mirrors the inline code in infrastructure/template.yaml (GroupsFunction).
Edit here, then paste into the template's ZipFile block before redeploying.

Routes (via API Gateway {proxy+} on /groups):
    POST   /groups                              -> create group
    GET    /groups                              -> list groups
    GET    /groups/{group_id}                   -> get group + members
    POST   /groups/{group_id}/players            -> add a player (body: player_id)
    DELETE /groups/{group_id}/players/{player_id} -> remove a player

Env vars:
    GROUPS_TABLE  - DynamoDB table name for groups
    PLAYERS_TABLE - DynamoDB table name for players
"""
import json
import os
import uuid
import boto3

dynamodb = boto3.resource('dynamodb')
groups_table = dynamodb.Table(os.environ['GROUPS_TABLE'])
players_table = dynamodb.Table(os.environ['PLAYERS_TABLE'])


def handler(event, context):
    try:
        method = event.get('httpMethod')
        proxy = (event.get('pathParameters') or {}).get('proxy', '')
        parts = [p for p in proxy.split('/') if p] if proxy else []

        if not parts:
            if method == 'POST':
                return create_group(event)
            elif method == 'GET':
                return list_groups()
        elif len(parts) == 1:
            if method == 'GET':
                return get_group(parts[0])
        elif len(parts) == 2 and parts[1] == 'players':
            if method == 'POST':
                return add_player(parts[0], event)
        elif len(parts) == 3 and parts[1] == 'players':
            if method == 'DELETE':
                return remove_player(parts[0], parts[2])

        return _response(404, {'error': 'not found'})
    except Exception as e:
        return _response(500, {'error': str(e)})


def create_group(event):
    body = json.loads(event.get('body') or '{}')
    group_name = (body.get('group_name') or '').strip()
    if not group_name:
        return _response(400, {'error': 'group_name is required'})
    group_id = str(uuid.uuid4())
    groups_table.put_item(Item={'group_id': group_id, 'group_name': group_name, 'member_ids': []})
    return _response(200, {'group_id': group_id, 'group_name': group_name})


def list_groups():
    items = groups_table.scan().get('Items', [])
    result = [
        {'group_id': i['group_id'], 'group_name': i['group_name'], 'member_count': len(i.get('member_ids', []))}
        for i in items
    ]
    return _response(200, {'groups': result})


def get_group(group_id):
    item = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    if not item:
        return _response(404, {'error': 'group not found'})
    member_ids = item.get('member_ids', [])
    members = []
    for pid in member_ids:
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        if p:
            members.append({'player_id': p['player_id'], 'name': p['name'], 'rating': p.get('rating', 1000)})
    return _response(200, {'group_id': item['group_id'], 'group_name': item['group_name'], 'members': members})


def add_player(group_id, event):
    body = json.loads(event.get('body') or '{}')
    player_id = body.get('player_id')
    if not player_id:
        return _response(400, {'error': 'player_id is required'})
    player = players_table.get_item(Key={'player_id': player_id}).get('Item')
    if not player:
        return _response(404, {'error': 'player not found'})
    group = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    if not group:
        return _response(404, {'error': 'group not found'})
    member_ids = set(group.get('member_ids', []))
    member_ids.add(player_id)
    groups_table.update_item(
        Key={'group_id': group_id},
        UpdateExpression='SET member_ids = :m',
        ExpressionAttributeValues={':m': list(member_ids)}
    )
    return _response(200, {'group_id': group_id, 'added': player_id})


def remove_player(group_id, player_id):
    group = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    if not group:
        return _response(404, {'error': 'group not found'})
    member_ids = [m for m in group.get('member_ids', []) if m != player_id]
    groups_table.update_item(
        Key={'group_id': group_id},
        UpdateExpression='SET member_ids = :m',
        ExpressionAttributeValues={':m': member_ids}
    )
    return _response(200, {'group_id': group_id, 'removed': player_id})


def _response(status_code, body_dict):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body_dict, default=str)
    }
