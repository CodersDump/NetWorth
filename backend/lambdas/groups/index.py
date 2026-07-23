"""
NetWorth - groups Lambda

Mirrors the inline code in infrastructure/template.yaml (GroupsFunction).
Edit here, then paste into the template's ZipFile block before redeploying.

Routes (via API Gateway {proxy+} on /groups):
    POST   /groups                              -> create group (optional creator_player_id -> owner)
    GET    /groups                              -> list groups
    GET    /groups/{group_id}                   -> get group + members + roles
    POST   /groups/{group_id}/players            -> add a player (body: player_id[s], optional role)
    DELETE /groups/{group_id}/players/{player_id} -> remove a player
    PUT    /group-role/{group_id}/{player_id}     -> set a member's role (owner/admin/member) - see note below

Env vars:
    GROUPS_TABLE  - DynamoDB table name for groups
    PLAYERS_TABLE - DynamoDB table name for players

NOTE on roles (Epic 3 + Epic 4 of the auth backlog): the roles route lives
at a deliberately separate top-level path, /group-role/{group_id}/{player_id}
- NOT nested under /groups - because API Gateway forbids a named path
parameter (like {group_id}) from being a sibling of the existing {proxy+}
catch-all under the same parent resource. Putting it at the top level
sidesteps that constraint entirely (same technique as the isolated
/whoami route). It requires a valid Cognito token and checks the caller
is either a SuperAdmin or already owner/admin of THIS group before
allowing the change - the first genuinely enforced route in the app.
Every other route in this file (create/list/get/add/remove) still has no
caller identity check at all - that's deliberate, staged rollout.
"""
import json
import os
import uuid
import boto3

dynamodb = boto3.resource('dynamodb')
groups_table = dynamodb.Table(os.environ['GROUPS_TABLE'])
players_table = dynamodb.Table(os.environ['PLAYERS_TABLE'])

CONFIRMATION_CODE = os.environ['CONFIRMATION_CODE']  # supplied at deploy time via GitHub Secrets -> CFN parameter, never stored in the repo


def handler(event, context):
    try:
        method = event.get('httpMethod')
        path_params = event.get('pathParameters') or {}

        # New isolated route (Epic 4 increment 2, corrected): PUT
        # /group-role/{group_id}/{player_id}. This arrives with its own
        # named pathParameters, not the combined 'proxy' string every
        # other route below uses - checked first, separately, so it can
        # never collide with the proxy-based dispatch underneath.
        # New isolated routes (Epic 4 increment 3): all live at their own
        # top-level paths for the same reason /group-role does - API
        # Gateway won't allow named path params as siblings of {proxy+}.
        # Method disambiguates between them since they share a path shape.
        if 'group_id' in path_params and 'player_id' in path_params and method == 'PUT':
            return set_role(path_params['group_id'], path_params['player_id'], event)
        if 'group_id' in path_params and 'player_id' in path_params and method == 'DELETE':
            return remove_player_enforced(path_params['group_id'], path_params['player_id'], event)
        if 'group_id' in path_params and 'player_id' not in path_params and method == 'DELETE':
            return delete_group_enforced(path_params['group_id'], event)

        proxy = path_params.get('proxy', '')
        parts = [p for p in proxy.split('/') if p] if proxy else []

        if not parts:
            if method == 'POST':
                return create_group(event)
            elif method == 'GET':
                return list_groups()
        elif len(parts) == 1:
            if method == 'GET':
                return get_group(parts[0])
            elif method == 'DELETE':
                return delete_group(parts[0], event)
            elif method == 'PUT':
                return update_group_defaults(parts[0], event)
        elif len(parts) == 2 and parts[1] == 'players':
            if method == 'POST':
                return add_player(parts[0], event)
        elif len(parts) == 3 and parts[1] == 'players':
            if method == 'DELETE':
                return remove_player(parts[0], parts[2], event)
        # (roles route now lives at the dedicated /group-role/{group_id}/{player_id}
        # path handled above, not here - see the top of this function)

        return _response(404, {'error': 'not found'})
    except Exception as e:
        return _response(500, {'error': str(e)})


def _authorize_group_action(group_id, claims):
    """Shared check for Epic 4's group-scoped write actions: SuperAdmin, or
    already owner/admin of THIS group. Returns None if allowed, or an
    error response if not - callers just do `if denied: return denied`."""
    if _is_super_admin(claims):
        return None
    caller_player_id = claims.get('custom:player_id')
    group = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    if not group:
        return _response(404, {'error': 'group not found'})
    caller_role = group.get('roles', {}).get(caller_player_id) if caller_player_id else None
    if caller_role not in ('owner', 'admin'):
        return _response(403, {'error': 'you must be an owner or admin of this group to do this'})
    return None


def delete_group_enforced(group_id, event):
    """Dual-gated (Epic 4 increment 3): a valid Cognito identity that's
    SuperAdmin or owner/admin of this group is now required IN ADDITION
    TO the existing CONFIRMATION_CODE check inside delete_group() itself -
    neither check alone is enough, matching the backlog's rule for routes
    that already had a real gate (unlike set_role, which had none)."""
    denied = _authorize_group_action(group_id, _caller_claims(event))
    if denied:
        return denied
    return delete_group(group_id, event)


def remove_player_enforced(group_id, player_id, event):
    """Same dual-gate as delete_group_enforced, for member removal."""
    denied = _authorize_group_action(group_id, _caller_claims(event))
    if denied:
        return denied
    return remove_player(group_id, player_id, event)


def create_group(event):
    body = json.loads(event.get('body') or '{}')
    group_name = (body.get('group_name') or '').strip()
    if not group_name:
        return _response(400, {'error': 'group_name is required'})

    group_id = str(uuid.uuid4())
    member_ids = []
    roles = {}

    creator_player_id = body.get('creator_player_id')
    if creator_player_id:
        creator = players_table.get_item(Key={'player_id': creator_player_id}).get('Item')
        if not creator:
            return _response(400, {'error': f'creator_player_id {creator_player_id} is not a known player'})
        member_ids = [creator_player_id]
        roles = {creator_player_id: 'owner'}

    groups_table.put_item(Item={
        'group_id': group_id, 'group_name': group_name, 'member_ids': member_ids, 'roles': roles
    })
    return _response(200, {'group_id': group_id, 'group_name': group_name, 'roles': roles})


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
    roles = item.get('roles', {})
    members = []
    for pid in member_ids:
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        if p:
            members.append({'player_id': p['player_id'], 'name': p['name'], 'rating': p.get('rating', 1000),
                             'role': roles.get(pid, 'member')})
    return _response(200, {
        'group_id': item['group_id'], 'group_name': item['group_name'], 'members': members,
        'roles': roles,
        'default_tournament_settings': item.get('default_tournament_settings')
    })


def update_group_defaults(group_id, event):
    """Save a group's default tournament creation settings (format, points,
    best_of, pairing_mode), so creating a new tournament for this group can
    pre-fill sensible values instead of re-picking every time."""
    body = json.loads(event.get('body') or '{}')
    settings = body.get('default_tournament_settings')
    if not settings:
        return _response(400, {'error': 'default_tournament_settings is required'})

    existing = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    if not existing:
        return _response(404, {'error': 'group not found'})

    groups_table.update_item(
        Key={'group_id': group_id},
        UpdateExpression='SET default_tournament_settings = :s',
        ExpressionAttributeValues={':s': settings}
    )
    return _response(200, {'group_id': group_id, 'default_tournament_settings': settings})


def delete_group(group_id, event):
    """Deletes only the group record itself. Player records are never
    touched here, since the same player can belong to multiple groups."""
    body = json.loads(event.get('body') or '{}')
    if body.get('confirm') != CONFIRMATION_CODE:
        return _response(400, {'error': "confirmation code is missing or incorrect"})

    existing = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    if not existing:
        return _response(404, {'error': 'group not found'})
    groups_table.delete_item(Key={'group_id': group_id})
    return _response(200, {'deleted': group_id, 'name': existing.get('group_name')})


def add_player(group_id, event):
    body = json.loads(event.get('body') or '{}')
    single_id = body.get('player_id')
    bulk_ids = body.get('player_ids')
    default_role = body.get('role', 'member')

    if bulk_ids:
        requested_ids = bulk_ids
    elif single_id:
        requested_ids = [single_id]
    else:
        return _response(400, {'error': 'player_id or player_ids is required'})

    group = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    if not group:
        return _response(404, {'error': 'group not found'})

    member_ids = set(group.get('member_ids', []))
    roles = dict(group.get('roles', {}))
    added = []
    not_found = []
    for pid in requested_ids:
        player = players_table.get_item(Key={'player_id': pid}).get('Item')
        if not player:
            not_found.append(pid)
            continue
        member_ids.add(pid)
        roles.setdefault(pid, default_role)  # don't clobber an existing role on re-add
        added.append(pid)

    groups_table.update_item(
        Key={'group_id': group_id},
        UpdateExpression='SET member_ids = :m, #r = :r',
        ExpressionAttributeNames={'#r': 'roles'},  # 'roles' is a DynamoDB reserved keyword - must be aliased
        ExpressionAttributeValues={':m': list(member_ids), ':r': roles}
    )
    result = {'group_id': group_id, 'added': added}
    if not_found:
        result['not_found'] = not_found
    return _response(200, result)


def remove_player(group_id, player_id, event):
    body = json.loads(event.get('body') or '{}')
    if body.get('confirm') != CONFIRMATION_CODE:
        return _response(400, {'error': "confirmation code is missing or incorrect"})

    group = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    if not group:
        return _response(404, {'error': 'group not found'})
    member_ids = [m for m in group.get('member_ids', []) if m != player_id]
    roles = {pid: r for pid, r in group.get('roles', {}).items() if pid != player_id}
    groups_table.update_item(
        Key={'group_id': group_id},
        UpdateExpression='SET member_ids = :m, #r = :r',
        ExpressionAttributeNames={'#r': 'roles'},
        ExpressionAttributeValues={':m': member_ids, ':r': roles}
    )
    return _response(200, {'group_id': group_id, 'removed': player_id})


VALID_ROLES = {'owner', 'admin', 'member'}


def _caller_claims(event):
    """Claims API Gateway's Cognito Authorizer attaches to the request.
    Only present on routes with COGNITO_USER_POOLS auth attached - which,
    as of Epic 4 increment 2, is just this one route. Every other route
    in this Lambda is untouched and still has no caller identity at all."""
    return (event.get('requestContext') or {}).get('authorizer', {}).get('claims') or {}


def _is_super_admin(claims):
    groups = (claims.get('cognito:groups') or '').split(',')
    return 'SuperAdmin' in groups


def set_role(group_id, player_id, event):
    """Set (or change) a member's role within this group.

    NOW ENFORCED (Epic 4 increment 2) - this is the first route in the
    app where a real caller identity is checked. The caller must be
    either a SuperAdmin, or already owner/admin of THIS SPECIFIC group -
    checked against the roles map itself, so ownership of one group never
    grants power over another.
    """
    claims = _caller_claims(event)
    caller_player_id = claims.get('custom:player_id')

    body = json.loads(event.get('body') or '{}')
    role = body.get('role')
    if role not in VALID_ROLES:
        return _response(400, {'error': f"role must be one of {sorted(VALID_ROLES)}"})

    group = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    if not group:
        return _response(404, {'error': 'group not found'})

    existing_roles = group.get('roles', {})
    caller_role = existing_roles.get(caller_player_id) if caller_player_id else None
    if not _is_super_admin(claims) and caller_role not in ('owner', 'admin'):
        return _response(403, {'error': 'you must be an owner or admin of this group to change roles'})

    if player_id not in group.get('member_ids', []):
        return _response(400, {'error': 'player is not a member of this group - add them first'})

    roles = dict(existing_roles)
    roles[player_id] = role
    groups_table.update_item(
        Key={'group_id': group_id},
        UpdateExpression='SET #r = :r',
        ExpressionAttributeNames={'#r': 'roles'},
        ExpressionAttributeValues={':r': roles}
    )
    return _response(200, {'group_id': group_id, 'player_id': player_id, 'role': role})


def _response(status_code, body_dict):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body_dict, default=str)
    }
