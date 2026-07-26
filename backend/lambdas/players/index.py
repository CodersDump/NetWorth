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
import uuid
import boto3
from datetime import datetime, timezone

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['PLAYERS_TABLE'])

# Optional so the module still imports in tests/older stacks that predate
# the approval flow - the routes that need them fail loudly instead.
CLAIM_REQUESTS_TABLE = os.environ.get('CLAIM_REQUESTS_TABLE')
USER_POOL_ID = os.environ.get('USER_POOL_ID')
UPLOADS_BUCKET = os.environ.get('UPLOADS_BUCKET')
claim_requests_table = dynamodb.Table(CLAIM_REQUESTS_TABLE) if CLAIM_REQUESTS_TABLE else None


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
        if event.get('resource') == '/claim-request' and method == 'POST':
            return create_claim_request(event)
        if event.get('resource') == '/action-request' and method == 'POST':
            return create_action_request(event)
        if event.get('resource') == '/upload-url' and method == 'POST':
            return create_upload_url(event)
        if event.get('resource') == '/claim-requests' and method == 'GET':
            return list_claim_requests(event)
        if event.get('resource') == '/claim-request-decide' and method == 'POST':
            return decide_claim_request(event)
        if event.get('resource') == '/app-settings' and method == 'GET':
            return get_app_settings(event)
        if event.get('resource') == '/app-settings' and method == 'POST':
            return set_app_setting(event)
        if event.get('resource') == '/store' and method == 'GET':
            return list_store(event)
        if event.get('resource') == '/store' and method == 'POST':
            return save_store_item(event)
        if event.get('resource') == '/store' and method == 'DELETE':
            return delete_store_item(event)
        if event.get('resource') == '/store-purchase' and method == 'POST':
            return purchase_store_item(event)
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
    # The reserved app-settings row lives in this table but isn't a player.
    items = [i for i in items if i.get('player_id') not in (_APP_SETTINGS_ID, _STORE_CATALOG_ID)]
    players = [
        {
            'player_id': i['player_id'],
            'name': i['name'],
            'nickname': i.get('nickname'),
            'skill_level': i.get('skill_level'),
            'rating': i.get('rating', 1000),
            'previous_rating': i.get('previous_rating', i.get('rating', 1000)),
            'xp': int(i.get('xp', 0) or 0),
            'level': int(i.get('level', 1) or 1),
            'coins': int(i.get('coins', 0) or 0),
            'owned_items': i.get('owned_items') or {},
            'avatar_id': i.get('avatar_id'),
            'banner_id': i.get('banner_id'),
            'background_id': i.get('background_id'),
            'avatar_url': i.get('avatar_url'),
            'banner_url': i.get('banner_url'),
            'background_url': i.get('background_url'),
            'avatar_uploads': i.get('avatar_uploads') or [],
            'banner_uploads': i.get('banner_uploads') or [],
            'background_uploads': i.get('background_uploads') or [],
            # The admin finance dropdown reads this to show the current role.
            # Without it, a player set to 'view' read back as 'none' because
            # setting finance_role clears the legacy finance_access boolean,
            # leaving the client with nothing to key off.
            'finance_role': i.get('finance_role') or ('write' if i.get('finance_access') else 'none'),
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
ALLOWED_BANNERS = {'court', 'smash', 'mesh', 'carbon', 'blueprint', 'chevron', 'dots', 'aurora', 'ember'}
# Page-level theme, deliberately a separate field from the banner: on a
# profile these are three independent layers (background, banner,
# avatar), and collapsing two of them into one means you can't have a
# calm background behind a loud banner.
ALLOWED_BACKGROUNDS = {'plain', 'court', 'nebula', 'blueprint', 'carbon', 'topo', 'weave', 'glow', 'ember'}


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


def _is_super_admin(claims):
    groups = claims.get('cognito:groups') or ''
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(',')]
    return 'SuperAdmin' in groups


def _linked_player_is_live(claims):
    """True only if the caller's custom:player_id resolves to a player that
    still exists. A deleted player leaves a stale claim in the JWT, and
    treating that as 'already linked' is what locked those accounts out."""
    pid = claims.get('custom:player_id')
    if not pid:
        return False
    return bool(table.get_item(Key={'player_id': pid}).get('Item'))


def create_claim_request(event):
    """Anyone logged in but not yet linked can ASK to be linked to an
    existing player. Deliberately requires no confirmation code: the whole
    point is to stop the destructive-ops code being handed out just so
    people can register. Nothing is linked until an admin approves."""
    if not claim_requests_table:
        return _response(500, {'error': 'claim requests are not configured on this stack'})
    claims = _caller_claims(event)
    if not claims:
        return _response(403, {'error': 'log in to request a profile'})
    if _linked_player_is_live(claims):
        return _response(400, {'error': 'your account is already linked to a player'})

    body = json.loads(event.get('body') or '{}')
    player_id = body.get('player_id')
    if not player_id:
        return _response(400, {'error': 'player_id is required'})

    player = table.get_item(Key={'player_id': player_id}).get('Item')
    if not player:
        return _response(404, {'error': 'player not found'})
    if player.get('email'):
        return _response(400, {'error': 'this player is already linked to an account'})

    email = claims.get('email')
    existing = claim_requests_table.scan().get('Items', [])
    if any(r.get('status') == 'pending' and r.get('requester_email') == email for r in existing):
        return _response(400, {'error': 'you already have a request waiting for approval'})

    request_id = str(uuid.uuid4())
    claim_requests_table.put_item(Item={
        'request_id': request_id,
        'player_id': player_id,
        'player_name': player.get('name'),
        'player_nickname': player.get('nickname'),
        'requester_email': email,
        # The Cognito username is what AdminUpdateUserAttributes needs at
        # approval time, and it is not always the same as the email.
        'requester_username': claims.get('cognito:username') or email,
        'type': 'claim',
        'status': 'pending',
        'created_at': datetime.now(timezone.utc).isoformat()
    })
    return _response(200, {'request_id': request_id, 'status': 'pending'})


def list_claim_requests(event):
    claims = _caller_claims(event)
    if not _is_super_admin(claims):
        return _response(403, {'error': 'only a SuperAdmin can review claim requests'})
    items = claim_requests_table.scan().get('Items', []) if claim_requests_table else []
    items.sort(key=lambda r: r.get('created_at', ''), reverse=True)
    pending = [r for r in items if r.get('status') == 'pending']
    # Recent decisions are worth returning too - it's the only record of
    # who approved what, and it makes a mistaken approval visible.
    decided = [r for r in items if r.get('status') != 'pending'][:10]
    return _response(200, {'pending': pending, 'decided': decided})


def create_action_request(event):
    """A non-SuperAdmin asking for a destructive action instead of doing
    it. Currently only player deletion - the queue is typed so other
    actions can join later without another table or another route.

    Edits deliberately do NOT come through here. They're attributed and
    reversible, so gating a typo fix behind an approval would just mean
    approving things blind to unblock people mid-session."""
    if not claim_requests_table:
        return _response(500, {'error': 'requests are not configured on this stack'})
    claims = _caller_claims(event)
    if not claims:
        return _response(403, {'error': 'log in to request this'})

    body = json.loads(event.get('body') or '{}')
    action_type = body.get('type')
    if action_type not in ('delete_player', 'new_profile', 'edit_own_name', 'finance_access',
                           'match_edit', 'match_delete'):
        return _response(400, {'error': "unknown request type"})

    # new_profile is the one request an unlinked account SHOULD be able to
    # make - it's how someone with no player becomes a member at all.
    # Everything else needs an existing membership, so a bare signup can't
    # fill the admin queue with requests about other people's players.
    if action_type != 'new_profile':
        if not _is_super_admin(claims) and not _linked_player_is_live(claims):
            return _response(403, {'error': 'claim your own profile before requesting changes to others'})

    if action_type == 'new_profile':
        return _create_new_profile_request(claims, body)
    if action_type == 'edit_own_name':
        return _create_edit_name_request(claims, body)
    if action_type == 'finance_access':
        return _create_finance_access_request(claims, body)
    if action_type in ('match_edit', 'match_delete'):
        return _create_match_request(claims, body, action_type)

    player_id = body.get('player_id')
    reason = (body.get('reason') or '').strip()
    if not player_id:
        return _response(400, {'error': 'player_id is required'})

    player = table.get_item(Key={'player_id': player_id}).get('Item')
    if not player:
        return _response(404, {'error': 'player not found'})

    existing = claim_requests_table.scan().get('Items', [])
    if any(r.get('status') == 'pending' and r.get('type') == 'delete_player'
           and r.get('player_id') == player_id for r in existing):
        return _response(400, {'error': 'a deletion request for this player is already waiting'})

    request_id = str(uuid.uuid4())
    claim_requests_table.put_item(Item={
        'request_id': request_id,
        'type': 'delete_player',
        'player_id': player_id,
        'player_name': player.get('name'),
        'player_nickname': player.get('nickname'),
        'requester_email': claims.get('email'),
        'requester_username': claims.get('cognito:username') or claims.get('email'),
        'reason': reason,
        'status': 'pending',
        'created_at': datetime.now(timezone.utc).isoformat()
    })
    return _response(200, {'request_id': request_id, 'status': 'pending'})


def _create_new_profile_request(claims, body):
    """Creating a brand-new profile. By default this is a REQUEST an admin
    approves - an account stays inert until then. A SuperAdmin can flip the
    'instant_create' app setting to make it create-and-link immediately
    instead (useful during a session when lots of new people are joining
    and an admin doesn't want to approve each one).

    Either way, if the chosen nickname already exists as an UNCLAIMED
    profile, that's very likely the same person - we refuse and point them
    at claiming it rather than making a duplicate history.
    """
    if _linked_player_is_live(claims):
        return _response(400, {'error': 'your account is already linked to a player'})

    name = (body.get('name') or '').strip()
    if not name:
        return _response(400, {'error': 'name is required'})
    nickname = sanitize_nickname(body.get('nickname') or '') or sanitize_nickname(name)

    existing = table.scan().get('Items', [])
    clash = next((p for p in existing
                  if (p.get('nickname') or '').strip().lower() == nickname), None)
    if clash:
        if not clash.get('email'):
            return _response(409, {
                'error': f'A profile with nickname "{nickname}" already exists and looks like it could be you. Claim it instead of creating a new one.',
                'suggest_claim_player_id': clash['player_id']
            })
        return _response(400, {'error': f'nickname "{nickname}" is already taken - pick another'})

    # Instant path, only when a SuperAdmin has enabled it.
    if _get_app_setting('instant_create'):
        player_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        table.put_item(Item={
            'player_id': player_id,
            'name': name,
            'nickname': nickname,
            'skill_level': 'intermediate',
            'rating': 1000,
            'email': claims.get('email'),
            'created_at': now,
            'created_by': claims.get('email')
        })
        return _response(200, {'player_id': player_id, 'name': name, 'nickname': nickname, 'linked': True})

    # Default guarded path: file a request for admin approval.
    pending = claim_requests_table.scan().get('Items', [])
    if any(r.get('status') == 'pending' and r.get('requester_email') == claims.get('email') for r in pending):
        return _response(400, {'error': 'you already have a request waiting for approval'})

    request_id = str(uuid.uuid4())
    claim_requests_table.put_item(Item={
        'request_id': request_id,
        'type': 'new_profile',
        'player_name': name,
        'player_nickname': nickname,
        'requester_email': claims.get('email'),
        'requester_username': claims.get('cognito:username') or claims.get('email'),
        'status': 'pending',
        'created_at': datetime.now(timezone.utc).isoformat()
    })
    return _response(200, {'request_id': request_id, 'status': 'pending'})


# ---------- app-wide settings (single reserved row in the players table) ----------
_APP_SETTINGS_ID = '__app_settings__'
_STORE_CATALOG_ID = '__store_catalog__'

def _get_app_setting(key, default=False):
    """App-wide flags live in one reserved row of the players table, keyed
    by a player_id that can never collide with a real UUID. Kept here rather
    than a new table to avoid extra infra for what's just a handful of
    booleans."""
    item = table.get_item(Key={'player_id': _APP_SETTINGS_ID}).get('Item') or {}
    return item.get(key, default)


def get_app_settings(event):
    item = table.get_item(Key={'player_id': _APP_SETTINGS_ID}).get('Item') or {}
    return _response(200, {
        'instant_create': bool(item.get('instant_create', False)),
        'xp_public': bool(item.get('xp_public', False))
    })


def set_app_setting(event):
    claims = _caller_claims(event)
    if not _is_super_admin(claims):
        return _response(403, {'error': 'only a SuperAdmin can change app settings'})
    body = json.loads(event.get('body') or '{}')
    key = body.get('key')
    if key not in ('instant_create', 'xp_public'):
        return _response(400, {'error': 'unknown setting'})
    table.update_item(
        Key={'player_id': _APP_SETTINGS_ID},
        UpdateExpression='SET #k = :v',
        ExpressionAttributeNames={'#k': key},
        ExpressionAttributeValues={':v': bool(body.get('value'))}
    )
    return _response(200, {key: bool(body.get('value'))})


# ---------- store: catalog, purchase, inventory ----------
# The catalog lives in one reserved row. Each item: item_id, name, type
# ('cosmetic' | 'perk'), cost (coins), and a small payload describing what it
# grants (e.g. an avatar frame id, or 'rename_token'). Purchases deduct coins
# by bumping the player's coins_spent (which the recompute preserves) and add
# the item to their owned list. Perks like rename tokens are consumable.
_STORE_ITEM_TYPES = ('cosmetic', 'perk')

def _load_catalog():
    item = table.get_item(Key={'player_id': _STORE_CATALOG_ID}).get('Item') or {}
    return item.get('items', [])


def list_store(event):
    """Public read - anyone can browse the store. Returns the catalog."""
    items = _load_catalog()
    return _response(200, {'items': items})


def save_store_item(event):
    claims = _caller_claims(event)
    if not _is_super_admin(claims):
        return _response(403, {'error': 'only a SuperAdmin can manage the store'})
    body = json.loads(event.get('body') or '{}')
    name = (body.get('name') or '').strip()
    itype = body.get('type')
    if not name:
        return _response(400, {'error': 'name is required'})
    if itype not in _STORE_ITEM_TYPES:
        return _response(400, {'error': f'type must be one of {_STORE_ITEM_TYPES}'})
    try:
        cost = int(body.get('cost'))
    except (TypeError, ValueError):
        return _response(400, {'error': 'cost must be a whole number of coins'})
    if cost < 0:
        return _response(400, {'error': 'cost cannot be negative'})

    items = _load_catalog()
    iid = body.get('item_id') or str(uuid.uuid4())
    row = {'item_id': iid, 'name': name, 'type': itype, 'cost': cost,
           'payload': body.get('payload') or {},
           'active': bool(body.get('active', True))}
    items = [i for i in items if i.get('item_id') != iid]
    items.append(row)
    table.put_item(Item={'player_id': _STORE_CATALOG_ID, 'items': items})
    return _response(200, {'item': row})


def delete_store_item(event):
    claims = _caller_claims(event)
    if not _is_super_admin(claims):
        return _response(403, {'error': 'only a SuperAdmin can manage the store'})
    body = json.loads(event.get('body') or '{}')
    iid = body.get('item_id')
    if not iid:
        return _response(400, {'error': 'item_id is required'})
    items = [i for i in _load_catalog() if i.get('item_id') != iid]
    table.put_item(Item={'player_id': _STORE_CATALOG_ID, 'items': items})
    return _response(200, {'ok': True})


def purchase_store_item(event):
    """A player spends coins on an item. Coins are deducted by bumping
    coins_spent (preserved across recompute); the item is added to their
    owned inventory. Consumable perks (rename tokens) can be bought
    repeatedly and stack as a count."""
    claims = _caller_claims(event)
    pid = claims.get('custom:player_id')
    if not pid:
        return _response(403, {'error': 'link a profile before buying'})
    player = table.get_item(Key={'player_id': pid}).get('Item')
    if not player:
        return _response(404, {'error': 'player not found'})

    body = json.loads(event.get('body') or '{}')
    iid = body.get('item_id')
    item = next((i for i in _load_catalog() if i.get('item_id') == iid), None)
    if not item or not item.get('active', True):
        return _response(404, {'error': 'item not available'})

    cost = int(item.get('cost', 0))
    balance = int(player.get('coins', 0) or 0)
    if balance < cost:
        return _response(400, {'error': f'not enough coins - need {cost}, you have {balance}'})

    owned = player.get('owned_items') or {}
    is_perk = item.get('type') == 'perk'
    # Cosmetics are owned once; perks stack as a consumable count.
    if not is_perk and iid in owned:
        return _response(400, {'error': 'you already own this'})

    new_spent = int(player.get('coins_spent', 0) or 0) + cost
    new_balance = balance - cost
    if is_perk:
        owned[iid] = int(owned.get(iid, 0) or 0) + 1
    else:
        owned[iid] = True

    table.update_item(
        Key={'player_id': pid},
        UpdateExpression='SET coins = :c, coins_spent = :cs, owned_items = :o',
        ExpressionAttributeValues={':c': new_balance, ':cs': new_spent, ':o': owned}
    )
    return _response(200, {'ok': True, 'coins': new_balance, 'owned_items': owned})


def _create_edit_name_request(claims, body):
    """Renaming is now self-service-only: the target is always the
    caller's OWN player, taken from their token rather than from the
    request body. Previously the edit form let you pick anyone from a
    dropdown, which meant any member could rename any other member."""
    player_id = claims.get('custom:player_id')
    player = table.get_item(Key={'player_id': player_id}).get('Item') if player_id else None
    if not player:
        return _response(403, {'error': 'you need a linked profile before you can edit it'})

    name = (body.get('name') or '').strip()
    nickname = sanitize_nickname(body.get('nickname') or '')
    if not name:
        return _response(400, {'error': 'name is required'})
    if not nickname:
        return _response(400, {'error': 'nickname is required'})

    if name == player.get('name') and nickname == (player.get('nickname') or ''):
        return _response(400, {'error': 'that is already your name and nickname'})

    others = [p for p in table.scan().get('Items', []) if p['player_id'] != player_id]
    if any((p.get('nickname') or '').strip().lower() == nickname for p in others):
        return _response(400, {'error': f'nickname "{nickname}" is already taken'})

    pending = claim_requests_table.scan().get('Items', [])
    if any(r.get('status') == 'pending' and r.get('type') == 'edit_own_name'
           and r.get('player_id') == player_id for r in pending):
        return _response(400, {'error': 'you already have a name change waiting for approval'})

    request_id = str(uuid.uuid4())
    claim_requests_table.put_item(Item={
        'request_id': request_id,
        'type': 'edit_own_name',
        'player_id': player_id,
        'player_name': player.get('name'),
        'player_nickname': player.get('nickname'),
        'new_name': name,
        'new_nickname': nickname,
        'requester_email': claims.get('email'),
        'requester_username': claims.get('cognito:username') or claims.get('email'),
        'status': 'pending',
        'created_at': datetime.now(timezone.utc).isoformat()
    })
    return _response(200, {'request_id': request_id, 'status': 'pending'})


def _approve_edit_name(req, claims):
    others = [p for p in table.scan().get('Items', []) if p['player_id'] != req['player_id']]
    if any((p.get('nickname') or '').strip().lower() == req['new_nickname'] for p in others):
        return _response(400, {'error': 'that nickname was taken while this request was waiting'})
    table.update_item(
        Key={'player_id': req['player_id']},
        UpdateExpression='SET #n = :n, nickname = :nk, last_edited_by = :leb, last_edited_at = :lea',
        ExpressionAttributeNames={'#n': 'name'},
        ExpressionAttributeValues={
            ':n': req['new_name'], ':nk': req['new_nickname'],
            ':leb': claims.get('email'), ':lea': datetime.now(timezone.utc).isoformat()
        }
    )
    return None


def _approve_new_profile(req, claims):
    """Creates the player only at approval time, and links it to the
    requester in the same step - so nothing exists until someone said yes."""
    nickname = req.get('player_nickname')
    existing = table.scan().get('Items', [])
    # Re-check at decision time: the nickname may have been taken while
    # the request sat in the queue.
    if any((p.get('nickname') or '').strip().lower() == nickname for p in existing):
        return _response(400, {'error': f'nickname "{nickname}" was taken while this request was waiting'})

    player_id = str(uuid.uuid4())
    table.put_item(Item={
        'player_id': player_id,
        'name': req.get('player_name'),
        'nickname': nickname,
        'skill_level': 'unrated',
        'rating': 1000,
        'email': req.get('requester_email'),
        'created_by': req.get('requester_email'),
        'created_at': datetime.now(timezone.utc).isoformat(),
        'approved_by': claims.get('email')
    })
    cognito = boto3.client('cognito-idp')
    cognito.admin_update_user_attributes(
        UserPoolId=USER_POOL_ID,
        Username=req['requester_username'],
        UserAttributes=[{'Name': 'custom:player_id', 'Value': player_id}]
    )
    return None



FINANCE_LEVELS = {'none': 0, 'view': 1, 'write': 2, 'delete': 3}



def _create_match_request(claims, body, action_type):
    """A match edit or delete, filed as a request rather than executed. The
    match's group_id is stored on the row now so approval can later be
    routed to that group's owner as well as the SuperAdmin - without a
    schema migration when that lands. A reason is required so the approver
    has context.
    """
    match_id = body.get('match_id')
    reason = (body.get('reason') or '').strip()
    if not match_id:
        return _response(400, {'error': 'match_id is required'})
    if not reason:
        return _response(400, {'error': 'a reason is required for match changes'})

    # We don't have the matches table in this Lambda, so trust the client's
    # supplied context (group_id, label, proposed scores) for display and
    # routing. The matches Lambda re-validates on execution, so a spoofed
    # field can only mislabel a request, never force an unauthorized change.
    row = {
        'request_id': str(uuid.uuid4()),
        'type': action_type,
        'match_id': match_id,
        'group_id': body.get('group_id') or None,
        'match_label': body.get('match_label') or match_id,
        'reason': reason,
        'requester_email': claims.get('email'),
        'requester_username': claims.get('cognito:username') or claims.get('email'),
        'requester_player_id': claims.get('custom:player_id'),
        'status': 'pending',
        'created_at': datetime.now(timezone.utc).isoformat()
    }
    if action_type == 'match_edit':
        try:
            row['new_score_a'] = int(body.get('new_score_a'))
            row['new_score_b'] = int(body.get('new_score_b'))
        except (TypeError, ValueError):
            return _response(400, {'error': 'valid new scores are required for an edit'})

    # One pending request per match is enough - collapse duplicates.
    pending = claim_requests_table.scan().get('Items', [])
    if any(r.get('status') == 'pending' and r.get('type') == action_type
           and r.get('match_id') == match_id for r in pending):
        return _response(400, {'error': 'a request for this match is already waiting'})

    claim_requests_table.put_item(Item=row)
    return _response(200, {'request_id': row['request_id'], 'status': 'pending'})


def _create_finance_access_request(claims, body):
    """A member asking for a finance role (view / write / delete). Approving
    it sets that role - handled in decide_claim_request so the same
    approve/reject UI covers it."""
    pid = claims.get('custom:player_id')
    player = table.get_item(Key={'player_id': pid}).get('Item') if pid else None
    if not player:
        return _response(403, {'error': 'link your profile before requesting finance access'})
    requested = (body or {}).get('role', 'view')
    if requested not in ('view', 'write', 'delete'):
        return _response(400, {'error': 'role must be view, write or delete'})
    # Don't let someone request a level they already have or below.
    current = player.get('finance_role')
    if not current and player.get('finance_access'):
        current = 'write'  # legacy boolean
    current = current or 'none'
    if FINANCE_LEVELS.get(current, 0) >= FINANCE_LEVELS[requested]:
        return _response(400, {'error': f'you already have {current} access'})
    pending = claim_requests_table.scan().get('Items', [])
    if any(r.get('status') == 'pending' and r.get('type') == 'finance_access'
           and r.get('player_id') == pid for r in pending):
        return _response(400, {'error': 'your finance access request is already waiting'})
    request_id = str(uuid.uuid4())
    claim_requests_table.put_item(Item={
        'request_id': request_id, 'type': 'finance_access',
        'player_id': pid, 'player_name': player.get('name'),
        'player_nickname': player.get('nickname'),
        'requested_role': requested,
        'requester_email': claims.get('email'),
        'requester_username': claims.get('cognito:username') or claims.get('email'),
        'status': 'pending', 'created_at': datetime.now(timezone.utc).isoformat()
    })
    return _response(200, {'request_id': request_id, 'status': 'pending'})


def _approve_finance_access(req):
    role = req.get('requested_role', 'view')
    table.update_item(
        Key={'player_id': req['player_id']},
        UpdateExpression='SET finance_role = :r REMOVE finance_access',
        ExpressionAttributeValues={':r': role}
    )
    return None


def decide_claim_request(event):
    """Approve or reject. On approval this writes the link on BOTH sides:
    the email onto the player record, and custom:player_id onto the
    requester's Cognito account. The second half needs the admin API,
    because the self-service updateAttributes call requires the
    requester's own session, which we obviously don't have here."""
    claims = _caller_claims(event)
    if not _is_super_admin(claims):
        return _response(403, {'error': 'only a SuperAdmin can decide claim requests'})
    if not claim_requests_table:
        return _response(500, {'error': 'claim requests are not configured on this stack'})

    body = json.loads(event.get('body') or '{}')
    request_id = body.get('request_id')
    action = body.get('action')
    if action not in ('approve', 'reject'):
        return _response(400, {'error': "action must be 'approve' or 'reject'"})
    if not request_id:
        return _response(400, {'error': 'request_id is required'})

    req = claim_requests_table.get_item(Key={'request_id': request_id}).get('Item')
    if not req:
        return _response(404, {'error': 'request not found'})
    if req.get('status') != 'pending':
        return _response(400, {'error': f"this request was already {req.get('status')}"})

    decided = {
        'status': 'approved' if action == 'approve' else 'rejected',
        'decided_at': datetime.now(timezone.utc).isoformat(),
        'decided_by': claims.get('email')
    }

    if action == 'approve' and req.get('type') == 'finance_access':
        _approve_finance_access(req)

    elif action == 'approve' and req.get('type') == 'edit_own_name':
        failed = _approve_edit_name(req, claims)
        if failed:
            return failed

    elif action == 'approve' and req.get('type') in ('match_edit', 'match_delete'):
        # Run the real change through the matches function so its rating
        # recompute happens exactly as it would for a direct admin edit -
        # no duplicated logic here. The confirmation code is supplied
        # server-side; the requester never sees or needs it.
        import os as _os
        fn = _os.environ.get('MATCHES_FUNCTION')
        if not fn:
            return _response(500, {'error': 'matches function not configured'})
        if req['type'] == 'match_delete':
            payload = {
                'resource': '/matches/{match_id}', 'httpMethod': 'DELETE',
                'pathParameters': {'match_id': req['match_id']},
                'body': json.dumps({'confirm': CONFIRMATION_CODE}),
                'requestContext': {'authorizer': {'claims': claims}}
            }
        else:
            # Scores come off the DynamoDB request row as Decimal, which
            # json.dumps can't serialize - coerce to int for the payload.
            payload = {
                'resource': '/matches/{match_id}', 'httpMethod': 'PUT',
                'pathParameters': {'match_id': req['match_id']},
                'body': json.dumps({'score_a': int(req['new_score_a']), 'score_b': int(req['new_score_b']),
                                    'confirm': CONFIRMATION_CODE}),
                'requestContext': {'authorizer': {'claims': claims}}
            }
        resp = boto3.client('lambda').invoke(
            FunctionName=fn, InvocationType='RequestResponse',
            Payload=json.dumps(payload).encode('utf-8'))
        inner = json.loads(resp['Payload'].read() or '{}')
        if inner.get('statusCode') != 200:
            body_err = json.loads(inner.get('body') or '{}').get('error', 'match change failed')
            return _response(400, {'error': f'could not apply change: {body_err}'})

    elif action == 'approve' and req.get('type') == 'new_profile':
        failed = _approve_new_profile(req, claims)
        if failed:
            return failed

    elif action == 'approve' and req.get('type') == 'delete_player':
        # Reuse the real handler rather than re-implementing deletion here,
        # so the Cognito cleanup and audit logging can't drift apart from
        # the direct-delete path.
        forged = {
            'body': json.dumps({'confirm': CONFIRMATION_CODE}),
            'requestContext': {'authorizer': {'claims': claims}}
        }
        result = delete_player(req['player_id'], forged)
        if result['statusCode'] != 200:
            return result

    elif action == 'approve':
        player = table.get_item(Key={'player_id': req['player_id']}).get('Item')
        if not player:
            return _response(404, {'error': 'that player no longer exists'})
        # Re-check at decision time, not just at request time: someone
        # else may have been approved for this player in between.
        if player.get('email'):
            return _response(400, {'error': 'that player was linked to another account in the meantime'})

        cognito = boto3.client('cognito-idp')
        cognito.admin_update_user_attributes(
            UserPoolId=USER_POOL_ID,
            Username=req['requester_username'],
            UserAttributes=[{'Name': 'custom:player_id', 'Value': req['player_id']}]
        )
        table.update_item(
            Key={'player_id': req['player_id']},
            UpdateExpression='SET email = :e',
            ExpressionAttributeValues={':e': req['requester_email']}
        )

    if action == 'reject' and req.get('type') in ('claim', 'new_profile') and USER_POOL_ID:
        # Rejecting used to leave the account stranded: it can't claim
        # anything, can't create anything, and can't sign up again because
        # Cognito says the address is taken - a dead end with no way out
        # from the user's side. Removing the account frees the address so
        # they can start over, which is the only sane meaning of "no".
        try:
            cognito = boto3.client('cognito-idp')
            cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=req['requester_username'])
        except Exception as e:
            print(json.dumps({'warn': 'cognito delete on reject failed',
                              'user': req.get('requester_username'), 'detail': str(e)}))

    claim_requests_table.update_item(
        Key={'request_id': request_id},
        UpdateExpression='SET #s = :s, decided_at = :d, decided_by = :b',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={
            ':s': decided['status'], ':d': decided['decided_at'], ':b': decided['decided_by']
        }
    )
    return _response(200, {'request_id': request_id, **decided})


# Deliberately narrow. SVG is excluded on purpose: it's an image format
# that can carry script, and these files are served from the same origin
# as the app, so an uploaded SVG would be stored XSS.
ALLOWED_UPLOAD_TYPES = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}
UPLOAD_KINDS = {'avatar', 'banner', 'background'}


def create_upload_url(event):
    """Hands back a short-lived presigned PUT. The browser uploads straight
    to S3 rather than through the Lambda - a 5MB body through API Gateway
    would hit its 10MB payload ceiling and cost Lambda time for what is
    just a byte pipe."""
    claims = _caller_claims(event)
    if not claims:
        return _response(403, {'error': 'log in to upload an image'})
    player_id = claims.get('custom:player_id')
    if not player_id or not table.get_item(Key={'player_id': player_id}).get('Item'):
        return _response(403, {'error': 'link your profile before uploading images'})
    if not UPLOADS_BUCKET:
        return _response(500, {'error': 'uploads are not configured on this stack'})

    body = json.loads(event.get('body') or '{}')
    kind = body.get('kind')
    content_type = body.get('content_type')
    if kind not in UPLOAD_KINDS:
        return _response(400, {'error': "kind must be 'avatar' or 'banner'"})
    if content_type not in ALLOWED_UPLOAD_TYPES:
        return _response(400, {'error': 'only JPEG, PNG and WebP images are allowed'})

    # The player_id is taken from the token, never the request body, so a
    # presigned URL can only ever write into the caller's own folder.
    ext = ALLOWED_UPLOAD_TYPES[content_type]
    # The client sends a hash of the processed image so the same picture
    # always maps to the same key. Validated rather than trusted - it goes
    # into an S3 path, so anything but plain hex is rejected. Falls back to
    # a random id if absent, which keeps older clients working.
    fingerprint = (body.get('fingerprint') or '').lower()
    if not re.fullmatch(r'[0-9a-f]{8,64}', fingerprint):
        fingerprint = uuid.uuid4().hex
    key = f'uploads/{kind}s/{player_id}/{fingerprint}.{ext}'

    s3 = boto3.client('s3')
    url = s3.generate_presigned_url(
        'put_object',
        Params={'Bucket': UPLOADS_BUCKET, 'Key': key, 'ContentType': content_type},
        ExpiresIn=300,
        HttpMethod='PUT'
    )
    # Only the key is returned for storage. Served back through CloudFront
    # as a same-origin path, so no absolute domain has to be baked in - it
    # keeps working if the distribution or a custom domain ever changes.
    return _response(200, {'upload_url': url, 'key': key})


def _valid_upload_key(value, player_id, kind):
    """An uploaded image is referenced by key, and the key is checked
    against the caller before it's stored. Without this, update_my_card
    would happily accept an arbitrary string - letting someone point their
    avatar at another player's file, or at an external URL entirely."""
    if value in (None, ''):
        return True
    return isinstance(value, str) and value.startswith(f'uploads/{kind}s/{player_id}/')



# How many custom uploads a player may keep per slot. Small on purpose:
# these are cosmetic, and an unbounded history is just storage nobody
# asked for. Presets are unaffected - they cost nothing to keep.
MAX_UPLOADS_PER_KIND = 3


def _rotate_uploads(player_id, kind, new_key):
    """Maintains the player's short list of custom images, newest first,
    and hard-deletes anything that falls off the end.

    Selecting an image they already have is a REORDER, not a new upload -
    otherwise re-picking an old photo would evict a different one and the
    list would churn for no reason.
    """
    player = table.get_item(Key={'player_id': player_id}).get('Item') or {}
    field = f'{kind}_uploads'
    existing = [k for k in (player.get(field) or []) if k]

    if new_key in existing:
        existing.remove(new_key)
    kept = ([new_key] + existing) if new_key else existing
    evicted = kept[MAX_UPLOADS_PER_KIND:]
    kept = kept[:MAX_UPLOADS_PER_KIND]

    if evicted and UPLOADS_BUCKET:
        try:
            boto3.client('s3').delete_objects(
                Bucket=UPLOADS_BUCKET,
                Delete={'Objects': [{'Key': k} for k in evicted], 'Quiet': True}
            )
        except Exception as e:
            # Losing the delete is untidy, not broken - the key is already
            # off the player's list either way, so it just leaves an
            # orphan object rather than failing the user's save.
            print(json.dumps({'warn': 'upload eviction failed', 'keys': evicted, 'detail': str(e)}))
    return kept


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
    background_id = body.get('background_id')
    avatar_url = body.get('avatar_url')
    banner_url = body.get('banner_url')
    background_url = body.get('background_url')
    if not _valid_upload_key(avatar_url, player_id, 'avatar'):
        return _response(400, {'error': 'invalid avatar image reference'})
    if not _valid_upload_key(banner_url, player_id, 'banner'):
        return _response(400, {'error': 'invalid banner image reference'})
    if not _valid_upload_key(background_url, player_id, 'background'):
        return _response(400, {'error': 'invalid background image reference'})
    if avatar_id is not None and avatar_id not in ALLOWED_AVATARS:
        return _response(400, {'error': f'unknown avatar_id - choose from {sorted(ALLOWED_AVATARS)}'})
    if banner_id is not None and banner_id not in ALLOWED_BANNERS:
        return _response(400, {'error': f'unknown banner_id - choose from {sorted(ALLOWED_BANNERS)}'})
    if background_id is not None and background_id not in ALLOWED_BACKGROUNDS:
        return _response(400, {'error': f'unknown background_id - choose from {sorted(ALLOWED_BACKGROUNDS)}'})
    if all(v is None for v in (avatar_id, banner_id, background_id, avatar_url, banner_url, background_url)):
        return _response(400, {'error': 'nothing to update'})

    update_parts = []
    values = {}
    if avatar_id is not None:
        update_parts.append('avatar_id = :a')
        values[':a'] = avatar_id
        # A preset and an upload are mutually exclusive - the render picks
        # the upload when both exist, so choosing a preset has to clear the
        # upload or it silently does nothing. This is the bug behind "my
        # photo came back / my preset won't apply".
        update_parts.append('avatar_url = :au_clear')
        values[':au_clear'] = None
    if banner_id is not None:
        update_parts.append('banner_id = :b')
        values[':b'] = banner_id
        update_parts.append('banner_url = :bu_clear')
        values[':bu_clear'] = None
    if background_id is not None:
        update_parts.append('background_id = :g')
        values[':g'] = background_id
        update_parts.append('background_url = :gu_clear')
        values[':gu_clear'] = None
    if avatar_url is not None:
        update_parts.append('avatar_url = :au')
        values[':au'] = avatar_url
        if avatar_url:  # empty string = "remove photo", don't wipe the fallback preset
            update_parts.append('avatar_id = :a_clear')
            values[':a_clear'] = None
        kept = _rotate_uploads(player_id, 'avatar', avatar_url)
        update_parts.append('avatar_uploads = :aup')
        values[':aup'] = kept
    if banner_url is not None:
        update_parts.append('banner_url = :bu')
        values[':bu'] = banner_url
        if banner_url:
            update_parts.append('banner_id = :b_clear')
            values[':b_clear'] = None
        kept = _rotate_uploads(player_id, 'banner', banner_url)
        update_parts.append('banner_uploads = :bup')
        values[':bup'] = kept
    if background_url is not None:
        update_parts.append('background_url = :gu')
        values[':gu'] = background_url
        if background_url:
            update_parts.append('background_id = :g_clear')
            values[':g_clear'] = None
        kept = _rotate_uploads(player_id, 'background', background_url)
        update_parts.append('background_uploads = :gup')
        values[':gup'] = kept

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
    # Login is now enforced at the API Gateway layer too, but check here
    # as well: the Lambda shouldn't depend on the route config being right
    # to stay safe, and this is what puts a name on the change.
    claims = _caller_claims(event)
    if not claims:
        return _response(403, {'error': 'log in to edit a player'})
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
    update_parts.append('last_edited_by = :leb')
    values[':leb'] = claims.get('email')
    update_parts.append('last_edited_at = :lea')
    values[':lea'] = datetime.now(timezone.utc).isoformat()

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
    claims = _caller_claims(event)
    if not claims:
        return _response(403, {'error': 'log in to delete a player'})
    # Direct deletion is SuperAdmin-only. Without this the approval queue
    # would be advisory: any logged-in user who knew the confirmation code
    # could still delete outright, and routing them through requests in the
    # UI would just be a suggestion rather than a control.
    # decide_claim_request calls this with the APPROVING admin's claims, so
    # the approved path satisfies the same check rather than bypassing it.
    if not _is_super_admin(claims):
        return _response(403, {'error': 'only a SuperAdmin can delete a player directly - submit a deletion request instead'})
    body = json.loads(event.get('body') or '{}')
    if body.get('confirm') != CONFIRMATION_CODE:
        return _response(400, {'error': "confirmation code is missing or incorrect"})

    existing = table.get_item(Key={'player_id': player_id}).get('Item')
    if not existing:
        return _response(404, {'error': 'player not found'})
    table.delete_item(Key={'player_id': player_id})

    # Deleting the player used to leave the Cognito account orphaned, which
    # is worse than it sounds: the address stays registered, so when that
    # person tries to sign up again they're told the user already exists,
    # while the account they still have points at a player that's gone.
    # Removing both halves together is what makes "delete and start over"
    # actually work.
    cognito_removed = None
    linked_email = existing.get('email')
    if linked_email and USER_POOL_ID:
        try:
            cognito = boto3.client('cognito-idp')
            username = _cognito_username_for_email(cognito, linked_email)
            if username:
                cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=username)
                cognito_removed = linked_email
        except Exception as e:
            # The player is already gone; failing the whole call here would
            # be misleading. Surfaced in the response so it's not silent.
            print(json.dumps({'warn': 'cognito delete failed', 'email': linked_email, 'detail': str(e)}))

    # The record is gone, so the only surviving trace of who removed it is
    # this log line. Worth having when a player disappears mid-season.
    print(json.dumps({'action': 'delete_player', 'player_id': player_id,
                      'name': existing.get('name'), 'by': claims.get('email'),
                      'cognito_removed': cognito_removed}))
    return _response(200, {'deleted': player_id, 'cognito_removed': cognito_removed})


def _cognito_username_for_email(cognito, email):
    """The username is not always the email, so it has to be looked up.
    Filter syntax is Cognito's own, and the quotes are required."""
    resp = cognito.list_users(UserPoolId=USER_POOL_ID, Filter=f'email = "{email}"', Limit=1)
    users = resp.get('Users', [])
    return users[0]['Username'] if users else None


def _response(status_code, body_dict):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body_dict, default=str)
    }
