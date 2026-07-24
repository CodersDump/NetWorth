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
import re
import boto3
from datetime import datetime, timezone

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['PLAYERS_TABLE'])


def sanitize_nickname(raw):
    """Same rule as register_player's version (duplicated on purpose -
    separate Lambda): lowercase, alphanumeric + underscore only."""
    cleaned = re.sub(r'[^a-z0-9_]', '', raw.lower())
    return cleaned or 'player'

CONFIRMATION_CODE = os.environ['CONFIRMATION_CODE']  # supplied at deploy time via GitHub Secrets -> CFN parameter, never stored in the repo


def handler(event, context):
    try:
        method = event.get('httpMethod')
        player_id = (event.get('pathParameters') or {}).get('player_id')
        params = event.get('queryStringParameters') or {}

        if method == 'GET' and not player_id and params.get('login_identifier'):
            return lookup_email_for_login(params['login_identifier'])
        if event.get('resource') == '/rename-self' and method == 'PUT':
            return rename_self(event)
        if event.get('resource') == '/update-my-card' and method == 'PUT':
            return update_my_card(event)
        if event.get('resource') == '/claim-player' and method == 'POST':
            return claim_player(event)
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
        # Only player_id and nickname are guaranteed unique - real names
        # can duplicate now, so matching on name here would be genuinely
        # ambiguous (which "Rahul" did you mean?) rather than just a
        # convenience shortcut.
        if (p['player_id'] == identifier or
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
            'rating': i.get('rating', 1000),
            'avatar_id': i.get('avatar_id'),
            'banner_id': i.get('banner_id'),
            'claimed': bool(i.get('email'))  # signal only, never the actual email - that stays private
        }
        for i in items
    ]
    return _response(200, {'players': players})


def _caller_claims(event):
    return (event.get('requestContext') or {}).get('authorizer', {}).get('claims') or {}


def _can_self_rename(claims):
    """Placeholder gate - the achievement/level system this is meant to
    require doesn't exist yet, so this always returns True for now. This
    function is the ONE place to wire in the real condition later (e.g.
    "has the Court Regular Tier 2 achievement" or "level >= N") - nothing
    else about this endpoint needs to change when that's ready.

    Deliberately NOT exposed in the frontend UI yet either, precisely
    because there's no achievement/level system to actually gate it
    against - the endpoint exists and is tested, but nothing links to it
    until that's built.
    """
    return True


ALLOWED_AVATARS = {'shuttle', 'trophy', 'lightning', 'fire', 'target', 'eagle', 'tiger',
                   'lion', 'wolf', 'fox', 'dragon', 'crown', 'muscle', 'star', 'game', 'racket'}
ALLOWED_BANNERS = {'sunset', 'ocean', 'forest', 'fire', 'royal', 'candy', 'midnight', 'court'}


def claim_player(event):
    """Self-service: link my Cognito account to an EXISTING, UNCLAIMED
    player record (someone who's played before but never signed up),
    instead of creating a brand new one. Two independent safety checks,
    not just one:
      1. Only players with no email on file are eligible - once a player
         is claimed, this route can never be used to take over their
         account, regardless of who's asking or what code they have.
      2. The confirmation code is still required on top of that - being
         unclaimed isn't the same as "up for grabs by anyone who finds
         this endpoint"; it requires the same insider knowledge every
         other semi-trusted action in this app already requires.
    """
    claims = _caller_claims(event)
    if not claims:
        return _response(403, {'error': 'log in to claim a profile'})
    # An account can carry a custom:player_id that points at a player who
    # has since been DELETED. The JWT claim is baked in at login and knows
    # nothing about the deletion, so the old unconditional check here left
    # those accounts permanently stuck: not linked to anything real, yet
    # refused permission to link to anything else. Only block when the
    # existing link still resolves to a live player.
    existing_link = claims.get('custom:player_id')
    if existing_link:
        still_exists = table.get_item(Key={'player_id': existing_link}).get('Item')
        if still_exists:
            return _response(400, {'error': 'your account is already linked to a player'})

    body = json.loads(event.get('body') or '{}')
    player_id = body.get('player_id')
    confirm = body.get('confirm')
    if confirm != CONFIRMATION_CODE:
        return _response(403, {'error': 'incorrect confirmation code'})
    if not player_id:
        return _response(400, {'error': 'player_id is required'})

    player = table.get_item(Key={'player_id': player_id}).get('Item')
    if not player:
        return _response(404, {'error': 'player not found'})
    if player.get('email'):
        return _response(400, {'error': 'this player is already linked to an account'})

    table.update_item(
        Key={'player_id': player_id},
        UpdateExpression='SET email = :e',
        ExpressionAttributeValues={':e': claims.get('email')}
    )
    return _response(200, {'player_id': player_id, 'name': player.get('name'), 'nickname': player.get('nickname')})


def update_my_card(event):
    """Self-service avatar/banner customization for the CALLER'S OWN
    player - unlike rename_self, this is NOT gated behind an achievement
    or level requirement, since it's purely cosmetic rather than an
    identity change. Any logged-in, linked account can customize freely.
    Both fields are validated against a fixed preset list (not free text)
    since there's no image upload yet - just curated emoji/gradient IDs."""
    claims = _caller_claims(event)
    if not claims:
        return _response(403, {'error': 'log in to customize your card'})
    player_id = claims.get('custom:player_id')
    if not player_id:
        return _response(403, {'error': 'your account is not linked to a player yet'})

    body = json.loads(event.get('body') or '{}')
    avatar_id = body.get('avatar_id')
    banner_id = body.get('banner_id')
    if avatar_id is not None and avatar_id not in ALLOWED_AVATARS:
        return _response(400, {'error': f'unknown avatar_id - choose from {sorted(ALLOWED_AVATARS)}'})
    if banner_id is not None and banner_id not in ALLOWED_BANNERS:
        return _response(400, {'error': f'unknown banner_id - choose from {sorted(ALLOWED_BANNERS)}'})
    if avatar_id is None and banner_id is None:
        return _response(400, {'error': 'provide avatar_id and/or banner_id'})

    update_parts = []
    values = {}
    if avatar_id is not None:
        update_parts.append('avatar_id = :a')
        values[':a'] = avatar_id
    if banner_id is not None:
        update_parts.append('banner_id = :b')
        values[':b'] = banner_id

    table.update_item(
        Key={'player_id': player_id},
        UpdateExpression='SET ' + ', '.join(update_parts),
        ExpressionAttributeValues=values
    )
    return _response(200, {'player_id': player_id, 'avatar_id': avatar_id, 'banner_id': banner_id})


def rename_self(event):
    """Self-service nickname change for the CALLER'S OWN linked player -
    no CONFIRMATION_CODE needed (that's for admin actions on arbitrary
    players), just being logged in, linked to a player, and passing the
    gate above."""
    claims = _caller_claims(event)
    if not claims:
        return _response(403, {'error': 'log in to rename yourself'})
    player_id = claims.get('custom:player_id')
    if not player_id:
        return _response(403, {'error': 'your account is not linked to a player yet'})
    if not _can_self_rename(claims):
        return _response(403, {'error': 'renaming is not unlocked for your account yet'})

    body = json.loads(event.get('body') or '{}')
    new_nickname = (body.get('nickname') or '').strip()
    if not new_nickname:
        return _response(400, {'error': 'nickname is required'})
    new_nickname = sanitize_nickname(new_nickname)

    other_players = table.scan().get('Items', [])
    if any(p['player_id'] != player_id and p.get('nickname', '').strip().lower() == new_nickname for p in other_players):
        return _response(400, {'error': f'nickname "{new_nickname}" is already taken - nicknames must be unique'})

    table.update_item(
        Key={'player_id': player_id},
        UpdateExpression='SET nickname = :nk',
        ExpressionAttributeValues={':nk': new_nickname}
    )
    return _response(200, {'player_id': player_id, 'nickname': new_nickname})


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

    other_players = table.scan().get('Items', [])

    # Real names are free-form and can duplicate - nickname is the unique
    # player identifier now, so uniqueness enforcement moved there.
    if name and name != existing.get('name'):
        print(f"[AUDIT RENAME] {datetime.now(timezone.utc).isoformat()} - player_id={player_id} old_name=\"{existing.get('name')}\" new_name=\"{name}\"")

    if nickname_provided:
        if not nickname:
            return _response(400, {'error': "nickname can't be cleared - it's now this player's unique ID"})
        nickname = sanitize_nickname(nickname)
        if any(p['player_id'] != player_id and p.get('nickname', '').strip().lower() == nickname for p in other_players):
            return _response(400, {'error': f'nickname "{nickname}" is already taken - nicknames must be unique'})

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
        update_parts.append('nickname = :nk')
        values[':nk'] = nickname

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
