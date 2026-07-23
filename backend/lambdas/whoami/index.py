"""
NetWorth - Epic 4 verification only: GET /whoami

Deliberately the simplest possible Lambda - no DynamoDB access, no
business logic. Its only job is to echo back whatever Cognito claims API
Gateway's Authorizer attached to the request, so you can prove end-to-end
that: a request WITHOUT a token is rejected before it even reaches this
code, and a request WITH a valid token arrives here carrying the caller's
email, custom:player_id, and cognito:groups (so you can see SuperAdmin
membership reflected here too).

This route is NOT nested under any existing {proxy+} catch-all - it's a
brand new top-level resource, specifically so attaching a Cognito
Authorizer to it can't change the authorization behavior of any existing
route. Nothing here is wired into matches/players/groups/finance yet.
"""
import json


def handler(event, context):
    authorizer = (event.get('requestContext') or {}).get('authorizer') or {}
    claims = authorizer.get('claims') or {}

    if not claims:
        # Should be unreachable in practice - API Gateway's Cognito
        # Authorizer rejects unauthenticated requests before they ever
        # reach this Lambda. If you see this, something about the
        # Authorizer wiring isn't enforcing the way it should.
        return _response(200, {
            'warning': 'no claims present - the Authorizer may not be attached correctly',
            'raw_authorizer_context': authorizer
        })

    groups_raw = claims.get('cognito:groups', '')
    groups = groups_raw.split(',') if groups_raw else []

    return _response(200, {
        'email': claims.get('email'),
        'player_id': claims.get('custom:player_id'),
        'is_super_admin': 'SuperAdmin' in groups,
        'cognito_groups': groups,
        'token_subject': claims.get('sub'),
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
