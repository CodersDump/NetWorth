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
import uuid
import boto3

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['PLAYERS_TABLE'])


def handler(event, context):
    try:
        body = json.loads(event.get('body') or '{}')
        name = (body.get('name') or '').strip()
        skill_level = body.get('skill_level', 'unrated')

        if not name:
            return _response(400, {'error': 'name is required'})

        player_id = str(uuid.uuid4())
        table.put_item(Item={
            'player_id': player_id,
            'name': name,
            'skill_level': skill_level,
            'rating': 1000  # starting Elo rating
        })

        return _response(200, {'player_id': player_id, 'name': name})
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
