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
from datetime import datetime, timezone

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['PLAYERS_TABLE'])

CONFIRMATION_CODE = os.environ['CONFIRMATION_CODE']  # supplied at deploy time via GitHub Secrets -> CFN parameter, never stored in the repo


def handler(event, context):
    try:
        method = event.get('httpMethod')
        player_id = (event.get('pathParameters') or {}).get('player_id')
        params = event.get('queryStringParameters') or {}

        if method == 'GET' and not player_id and params.get('login_identifier'):
            return lookup_email_for_login(params['login_identifier'])
        if method == 'GET' and not player_id:
            return list_players()
        elif method == 'PUT' and player_id:
            return update_player(player_id, event)
        elif method == 'DELETE' and player_id:
            return delete_player(player_id, event)
        return _response(400, {'error': 'unsupported operation'})
    except Exception as e:
        return _response(500, {'error': str(e)})


def lookup_email_for_login(identifier):
    """Resolves a player_id, exact name, or exact nickname to the email
    linked to their Cognito account, so the login form can accept a
    familiar identifier instead of requiring an email address up front.

    Deliberately public (this runs BEFORE login, by definition), and
    deliberately narrow: exact case-insensitive match only (no partial/
    fuzzy search), one generic "not found" for every failure case (unknown
    identifier, known player with no linked account) so this can't be used
    to enumerate who has an account versus who doesn't. This does still
    mean anyone who knows a player's name or nickname can learn whether
    they have a linked account and, if so, their email - the same
    trade-off as any "log in with username" flow that resolves to an
    email/Cognito identity behind the scenes.
    """
    identifier_norm = (identifier or '').strip().lower()
    if not identifier_norm:
        return _response(404, {'error': 'no account found for that identifier'})

    items = table.scan().get('Items', [])
    for p in items:
        email = p.get('email')
        if not email:
            continue
        if (p['player_id'] == identifier or
                p.get('name', '').strip().lower() == identifier_norm or
                p.get('nickname', '').strip().lower() == identifier_norm):
            return _response(200, {'email': email})

    return _response(404, {'error': 'no account found for that identifier'})


def list_players():
    items = table.scan().get('Items', [])
    players = [
        {
            'player_id': i['player_id'],
            'name': i['name'],
            'nickname': i.get('nickname'),
            'skill_level': i.get('skill_level'),
            'rating': i.get('rating', 1000)
        }
        for i in items
    ]
    return _response(200, {'players': players})


def update_player(player_id, event):
    body = json.loads(event.get('body') or '{}')
    if body.get('confirm') != CONFIRMATION_CODE:
        return _response(400, {'error': "confirmation code is missing or incorrect"})

    existing = table.get_item(Key={'player_id': player_id}).get('Item')
    if not existing:
        return _response(404, {'error': 'player not found'})

    name = (body.get('name') or '').strip()
    skill_level = body.get('skill_level')
    nickname_provided = 'nickname' in body
    nickname = (body.get('nickname') or '').strip() if nickname_provided else None

    if not name and not skill_level and not nickname_provided:
        return _response(400, {'error': 'provide name, skill_level, and/or nickname to update'})

    if name:
        other_players = table.scan().get('Items', [])
        if any(p['player_id'] != player_id and p.get('name', '').strip().lower() == name.lower() for p in other_players):
            return _response(400, {'error': f'a player named "{name}" already exists - names must be unique'})
        if name != existing.get('name'):
            print(f"[AUDIT RENAME] {datetime.now(timezone.utc).isoformat()} - player_id={player_id} old_name=\"{existing.get('name')}\" new_name=\"{name}\"")

    update_parts = []
    remove_parts = []
    names = {}
    values = {}
    if name:
        update_parts.append('#n = :n')
        names['#n'] = 'name'
        values[':n'] = name
    if skill_level:
        update_parts.append('skill_level = :s')
        values[':s'] = skill_level
    if nickname_provided:
        if nickname:
            update_parts.append('nickname = :nk')
            values[':nk'] = nickname
        else:
            remove_parts.append('nickname')  # empty string = clear the nickname

    expr = ''
    if update_parts:
        expr += 'SET ' + ', '.join(update_parts)
    if remove_parts:
        expr += (' ' if expr else '') + 'REMOVE ' + ', '.join(remove_parts)

    kwargs = {'Key': {'player_id': player_id}, 'UpdateExpression': expr}
    if values:
        kwargs['ExpressionAttributeValues'] = values
    if names:
        kwargs['ExpressionAttributeNames'] = names

    table.update_item(**kwargs)
    return _response(200, {'player_id': player_id, 'updated': True})


def delete_player(player_id, event):
    body = json.loads(event.get('body') or '{}')
    if body.get('confirm') != CONFIRMATION_CODE:
        return _response(400, {'error': "confirmation code is missing or incorrect"})

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
