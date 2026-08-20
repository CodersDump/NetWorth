"""
NetWorth - register_player Lambda

This file mirrors the inline code deployed via CloudFormation
(infrastructure/template.yaml). Edit here for local development /
version control, then paste into the template's ZipFile block
when you're ready to redeploy.

Env vars:
    PLAYERS_TABLE - DynamoDB table name for players
"""
import json
import os
import re
import uuid
import boto3
from datetime import datetime, timezone

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['PLAYERS_TABLE'])


def _scan_all(table, **kw):
    """Full-table scan that follows LastEvaluatedKey - a bare .scan() returns
    only the first 1 MB page (KNOWN_ISSUES #15). Used here to check nickname
    uniqueness across every player - a truncated page would silently let a
    duplicate nickname through instead of catching it. Copied from
    matches/players lambdas (KNOWN_ISSUES #6 - not shared, keep every copy
    in sync)."""
    items, last = [], None
    while True:
        if last:
            kw['ExclusiveStartKey'] = last
        resp = table.scan(**kw)
        items.extend(resp.get('Items', []))
        last = resp.get('LastEvaluatedKey')
        if not last:
            return items


def sanitize_nickname(raw):
    """Hard format rule: lowercase, alphanumeric + underscore only.
    Applied uniformly whether the nickname was auto-derived from a real
    name or typed explicitly - 'SneakShot!' and 'Sneak Shot' both become
    'sneakshot'. Never rejects; just strips whatever doesn't fit."""
    cleaned = re.sub(r'[^a-z0-9_]', '', raw.lower())
    return cleaned or 'player'  # defensive fallback if literally nothing survives sanitizing


def _caller_claims(event):
    return (event.get('requestContext') or {}).get('authorizer', {}).get('claims') or {}


def handler(event, context):
    try:
        # Registration used to be fully anonymous, which meant a junk
        # player could be created with no way to trace who did it. The
        # route now requires a Cognito session AND records the creator, so
        # the same thing is at worst attributable.
        claims = _caller_claims(event)
        if not claims:
            return _response(403, {'error': 'log in to register a player'})

        body = json.loads(event.get('body') or '{}')
        name = (body.get('name') or '').strip()
        skill_level = body.get('skill_level', 'unrated')
        nickname = (body.get('nickname') or '').strip()

        if not name:
            return _response(400, {'error': 'name is required'})

        existing_players = _scan_all(table)
        existing_nicknames = {p.get('nickname', '').strip().lower() for p in existing_players if p.get('nickname')}

        # Nickname format is a hard rule regardless of source (auto-derived
        # or explicitly typed): lowercase, alphanumeric + underscore only.
        # Sanitized rather than rejected - typing "SneakShot!" just becomes
        # "sneakshot" instead of bouncing the request back with an error.
        nickname = sanitize_nickname(nickname) if nickname else ''

        if not nickname:
            base = sanitize_nickname(name)
            nickname = base
            n = 2
            while nickname in existing_nicknames:
                nickname = f"{base}{n}"
                n += 1
        elif nickname in existing_nicknames:
            return _response(400, {'error': f'nickname "{nickname}" is already taken - nicknames must be unique'})

        player_id = str(uuid.uuid4())
        table.put_item(Item={
            'player_id': player_id,
            'name': name,
            'nickname': nickname,
            'skill_level': skill_level,
            'rating': 1000,  # starting Elo rating
            'created_by': claims.get('email'),
            'created_at': datetime.now(timezone.utc).isoformat()
        })

        return _response(200, {'player_id': player_id, 'name': name, 'nickname': nickname})
    except Exception as e:
        return _response(500, {'error': str(e)})


def _response(status_code, body_dict):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body_dict, default=str)
    }
