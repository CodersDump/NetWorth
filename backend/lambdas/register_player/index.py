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

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['PLAYERS_TABLE'])


def handler(event, context):
    try:
        body = json.loads(event.get('body') or '{}')
        name = (body.get('name') or '').strip()
        skill_level = body.get('skill_level', 'unrated')
        nickname = (body.get('nickname') or '').strip()

        if not name:
            return _response(400, {'error': 'name is required'})

        existing_players = table.scan().get('Items', [])
        existing_nicknames = {p.get('nickname', '').strip().lower() for p in existing_players if p.get('nickname')}

        if not nickname:
            # Auto-derive: name with all whitespace stripped, then
            # de-duplicated with a numeric suffix if that collides -
            # nickname is now the unique player identifier, so every
            # player needs one, not just people who opt in to a cosmetic
            # display name.
            base = re.sub(r'\s+', '', name)
            nickname = base
            n = 2
            while nickname.lower() in existing_nicknames:
                nickname = f"{base}{n}"
                n += 1
        else:
            if nickname.lower() in existing_nicknames:
                return _response(400, {'error': f'nickname "{nickname}" is already taken - nicknames must be unique'})

        player_id = str(uuid.uuid4())
        table.put_item(Item={
            'player_id': player_id,
            'name': name,
            'nickname': nickname,
            'skill_level': skill_level,
            'rating': 1000  # starting Elo rating
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
