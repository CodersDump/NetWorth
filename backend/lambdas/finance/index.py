"""
NetWorth - finance Lambda

Private club-finance ledger: monthly expenses per timeslot, monthly
membership enrollment, and per-session walk-in fees - plus the settlement
math that used to live in the Excel sheet's SUMIFS/COUNTIFS layer
(estimated vs actual cost, extra amount collected from walk-ins, cost per
head, and residual refund per member).

ACCESS MODEL
    Every route below requires the VIEW_KEY (sent as ?view_key= on GETs
    and as "view_key" in the JSON body on writes), with ONE exception:
    GET /finance/walkins/public - which returns walk-in names/dates/slots
    only (never fees, never recruit verdicts), and only while the
    walkins_public setting is enabled. Destructive deletes additionally
    require the same CONFIRMATION_CODE the rest of the app uses.

    The key is checked server-side here because the frontend is a static
    page hitting an open API - hiding a tab in the UI protects nothing.

DATA MODEL (single table, FINANCE_TABLE, hash key record_id)
    record_type='expense':    {month, year, slot, item, estimated_cost,
                               actual_cost, estimated_qty, actual_qty}
    record_type='membership': {month, year, slot, display_name,
                               player_id?, status Yes/No/NA, remark}
    record_type='walkin':     {date ISO yyyy-mm-dd, slot, display_name,
                               player_id?, fee, skill, recruit_verdict,
                               note}  (negative fee = refund/adjustment)
    record_id='settings':     {walkins_public: bool}

Routes (via API Gateway {proxy+} on /finance):
    GET    /finance/summary                 -> settlement per (month,year,slot)
    GET    /finance/expenses                -> list
    POST   /finance/expenses                -> create (or bulk with "items")
    PUT    /finance/expenses/{id}           -> update
    DELETE /finance/expenses/{id}           -> delete (confirmation code)
    GET    /finance/memberships?month=&year=&slot=  -> list (filters optional)
    POST   /finance/memberships             -> create/bulk upsert
    PUT    /finance/memberships/{id}        -> update (status/remark/link player)
    DELETE /finance/memberships/{id}        -> delete (confirmation code)
    GET    /finance/walkins                 -> list (full detail)
    POST   /finance/walkins                 -> create (or bulk with "items")
    PUT    /finance/walkins/{id}            -> update (incl. linking player_id)
    DELETE /finance/walkins/{id}            -> delete (confirmation code)
    GET    /finance/walkins/public          -> names/dates/slots only, no key,
                                               404s unless walkins_public
    GET    /finance/settings                -> current settings
    PUT    /finance/settings                -> update settings

Env vars:
    FINANCE_TABLE, PLAYERS_TABLE
"""
import json
import os
import uuid
from decimal import Decimal

import boto3

dynamodb = boto3.resource('dynamodb')
finance_table = dynamodb.Table(os.environ['FINANCE_TABLE'])
players_table = dynamodb.Table(os.environ['PLAYERS_TABLE'])
matches_table = dynamodb.Table(os.environ['MATCHES_TABLE'])
# Stage 2 of group-scoped finance: needed to resolve a caller's role in a
# group (owner/admin -> full finance for that group) and the group's
# per-member finance_roles map. Optional so older stacks still import.
GROUPS_TABLE = os.environ.get('GROUPS_TABLE')
groups_table = dynamodb.Table(GROUPS_TABLE) if GROUPS_TABLE else None
DEFAULT_GROUP_NAME = 'Club (default)'
_default_group_id_cache = None

# Both secrets arrive as environment variables set by CloudFormation
# parameters (NoEcho), which CI passes in from GitHub repository secrets.
# Rotating either = change the GitHub secret and re-run the deploy.
VIEW_KEY = os.environ['FINANCE_VIEW_KEY']  # supplied at deploy time, same mechanism
CONFIRMATION_CODE = os.environ['CONFIRMATION_CODE']  # supplied at deploy time via GitHub Secrets -> CFN parameter, never stored in the repo

MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
          'August', 'September', 'October', 'November', 'December']


def _caller_claims(event):
    """Claims API Gateway's Cognito Authorizer attaches to the request.
    Only present on the new isolated /finance-delete route - every other
    finance route still has no caller identity check at all, same
    staged-rollout approach as the groups Lambda."""
    return (event.get('requestContext') or {}).get('authorizer', {}).get('claims') or {}


def _is_super_admin(claims):
    groups = (claims.get('cognito:groups') or '').split(',')
    return 'SuperAdmin' in groups



# Finance is tiered. A player's finance_role is one of:
#   none < view < write < delete
# each level implicitly includes the ones below it. The legacy boolean
# finance_access is honoured on read (True == 'write', since that's what
# the old flag effectively allowed) so nobody loses access at deploy time.
FINANCE_LEVELS = {'none': 0, 'view': 1, 'write': 2, 'delete': 3}


def _finance_role(claims):
    if _is_super_admin(claims):
        return 'delete'  # admins get the top tier implicitly
    pid = claims.get('custom:player_id')
    if not pid:
        return 'none'
    p = players_table.get_item(Key={'player_id': pid}).get('Item') or {}
    role = p.get('finance_role')
    if role in FINANCE_LEVELS:
        return role
    # Back-compat: an old grant stored as finance_access=True maps to write.
    if p.get('finance_access'):
        return 'write'
    return 'none'


def _finance_level(claims):
    return FINANCE_LEVELS[_finance_role(claims)]


def _has_finance_access(claims):
    """View or better - the gate for reading finance at all."""
    return _finance_level(claims) >= FINANCE_LEVELS['view']


def _default_group_id():
    """The group_id of the 'Club (default)' group that the pre-migration
    ledger lives under. Cached per warm container. Returns None if the
    migration hasn't been run or the groups table isn't wired."""
    global _default_group_id_cache
    if _default_group_id_cache is not None:
        return _default_group_id_cache
    if not groups_table:
        return None
    for g in groups_table.scan().get('Items', []):
        if g.get('group_name') == DEFAULT_GROUP_NAME:
            _default_group_id_cache = g.get('group_id')
            return _default_group_id_cache
    return None


def _group_for_request(params, body):
    """The group_id this finance op targets. Falls back to the default group
    so pre-Stage-3 clients (which don't send group_id yet) keep working
    exactly as before - they operate on the existing ledger."""
    return (params.get('group_id') or body.get('group_id') or _default_group_id())


def _group_finance_level(claims, group_id):
    """A caller's finance level (0-3) FOR A SPECIFIC GROUP.
      - SuperAdmin: full on every group.
      - Group owner/admin: full on their own group.
      - Otherwise: the group's per-member finance_roles map.
      - Transition floor: a legacy GLOBAL finance_role counts only on the
        DEFAULT group (where the old shared ledger lives), so existing
        grant-holders keep their access and it never leaks into other groups.
    """
    if _is_super_admin(claims):
        return FINANCE_LEVELS['delete']
    pid = claims.get('custom:player_id')
    group = {}
    if groups_table and group_id:
        group = groups_table.get_item(Key={'group_id': group_id}).get('Item') or {}
    if pid:
        if group.get('roles', {}).get(pid) in ('owner', 'admin'):
            return FINANCE_LEVELS['delete']
        per_group = (group.get('finance_roles') or {}).get(pid)
        if per_group in FINANCE_LEVELS:
            return FINANCE_LEVELS[per_group]
    if group_id and group_id == _default_group_id():
        return _finance_level(claims)  # transition floor, default group only
    return 0


def _has_any_group_finance(claims):
    """True if the caller has finance access in ANY group (owner/admin, or a
    per-group finance_roles entry). Lets group owners obtain the shared key
    during the transition, before it's retired."""
    pid = claims.get('custom:player_id')
    if not pid or not groups_table:
        return False
    for g in groups_table.scan().get('Items', []):
        if g.get('roles', {}).get(pid) in ('owner', 'admin'):
            return True
        if (g.get('finance_roles') or {}).get(pid) in FINANCE_LEVELS:
            return True
    return False


def finance_key_for_caller(event):
    """Hands the shared view key to any caller with finance access - global
    (legacy) OR in any group (owner/admin/per-group role) - plus their global
    role so the UI can hide write/delete controls at the coarse level. Real
    per-group enforcement happens on each finance call."""
    claims = _caller_claims(event)
    if not claims:
        return _response(403, {'error': 'log in to access finance'})
    if not (_has_finance_access(claims) or _has_any_group_finance(claims)):
        return _response(403, {'error': 'you do not have finance access - ask an admin'})
    return _response(200, {'view_key': VIEW_KEY, 'finance_role': _finance_role(claims)})


def set_finance_access(event):
    """SuperAdmin sets a player's finance role directly."""
    claims = _caller_claims(event)
    if not _is_super_admin(claims):
        return _response(403, {'error': 'only a SuperAdmin can manage finance access'})
    body = json.loads(event.get('body') or '{}')
    player_id = body.get('player_id')
    role = body.get('role')
    # Accept the legacy {grant: true/false} shape too, mapping to view/none.
    if role is None and 'grant' in body:
        role = 'view' if body.get('grant') else 'none'
    if role not in FINANCE_LEVELS:
        return _response(400, {'error': f'role must be one of {sorted(FINANCE_LEVELS)}'})
    if not player_id:
        return _response(400, {'error': 'player_id is required'})
    if not players_table.get_item(Key={'player_id': player_id}).get('Item'):
        return _response(404, {'error': 'player not found'})
    players_table.update_item(
        Key={'player_id': player_id},
        UpdateExpression='SET finance_role = :r REMOVE finance_access',
        ExpressionAttributeValues={':r': role}
    )
    return _response(200, {'player_id': player_id, 'finance_role': role})

def handler(event, context):
    try:
        method = event.get('httpMethod')
        path_params = event.get('pathParameters') or {}

        # New isolated route (Epic 4 increment 4): DELETE
        # /finance-delete/{record_type}/{record_id}. Triple-gated - SuperAdmin
        # identity, the existing FINANCE_VIEW_KEY, AND the existing
        # CONFIRMATION_CODE (via the unchanged delete_record function) all
        # required. Lives at its own top-level path, not nested under
        # /finance/{proxy+}, for the same platform reason as /group-role -
        # API Gateway forbids a named path param as a sibling of {proxy+}.
        if 'record_type' in path_params and 'record_id' in path_params and method == 'DELETE':
            return delete_record_enforced(path_params['record_type'], path_params['record_id'], event)

        # /finance-access : authenticated. GET returns the view key to a
        # caller who's allowed (SuperAdmin, or a player flagged
        # finance_access) so they never type it. POST toggles a player's
        # access - SuperAdmin only. This replaces "everyone shares one
        # secret" with "an admin decides who can see finance".
        resource = event.get('resource') or ''
        if resource.endswith('/finance-access'):
            if method == 'GET':
                return finance_key_for_caller(event)
            if method == 'POST':
                return set_finance_access(event)


        # Epic 4 (increment 5): a request carrying an 'authorizer' context
        # arrived via /finance-secure/{proxy+}; one with none came via the
        # legacy open /finance/{proxy+} route. We no longer apply a coarse
        # GLOBAL access gate here - it predated group-scoped finance and would
        # wrongly block a group owner who has no global role. Access is now
        # enforced per-group, per-method below (a GET with < view on the
        # target group returns 403), which correctly admits group owners.
        claims = _caller_claims(event)

        proxy = path_params.get('proxy', '')
        parts = [p for p in proxy.split('/') if p] if proxy else []
        params = event.get('queryStringParameters') or {}
        body = json.loads(event.get('body') or '{}')

        # The one public route: names/dates only, and only when enabled.
        if parts == ['walkins', 'public'] and method == 'GET':
            return public_walkins()

        # Member-safe "what do I owe / am I owed" view. Any logged-in MEMBER of
        # the group may see their OWN settlement lines (no view key, no finance
        # role needed) - expenses and other members' numbers are never included.
        # This is the "own-settlement-only" access level (Stage 4b).
        if parts == ['my-settlement'] and method == 'GET':
            return my_settlement(claims, _group_for_request(params, body))

        # UPI payment details are public too - guests pay walk-in fees by
        # scanning the QR, so this must work with no key and no login.
        if parts == ['upi', 'public'] and method == 'GET':
            return public_upi()

        supplied_key = params.get('view_key') or body.get('view_key')
        if supplied_key != VIEW_KEY:
            return _response(403, {'error': 'view key is missing or incorrect'})

        # Which group's ledger this request targets (defaults to the "Club
        # (default)" group so existing clients keep working). All record
        # reads/writes below are scoped to it.
        target_group = _group_for_request(params, body)

        # Tiered enforcement applies only when we actually know who the
        # caller is, and is now scoped to the TARGET GROUP. Requests using
        # only the shared key (the legacy open route, no claims) have no
        # identity to gate on, so the key alone keeps the full access it
        # always had on the default group.
        claims_for_role = _caller_claims(event)
        if claims_for_role:
            lvl = _group_finance_level(claims_for_role, target_group)
            if method == 'GET' and lvl < FINANCE_LEVELS['view']:
                return _response(403, {'error': 'you do not have finance access to this group'})
            if method in ('POST', 'PUT') and lvl < FINANCE_LEVELS['write']:
                return _response(403, {'error': 'you have view-only finance access here - ask an owner for write access'})
            if method == 'DELETE' and lvl < FINANCE_LEVELS['delete']:
                return _response(403, {'error': 'you need delete access for this - ask an owner'})

        if parts == ['summary'] and method == 'GET':
            return summary(target_group)
        if parts == ['insights'] and method == 'GET':
            insights._params = params
            return insights(target_group)
        if parts == ['settings']:
            if method == 'GET':
                return get_settings()
            if method == 'PUT':
                return put_settings(body)

        for kind in ('expenses', 'memberships', 'walkins'):
            rtype = kind[:-1] if kind != 'memberships' else 'membership'
            if parts == [kind]:
                if method == 'GET':
                    return list_records(rtype, params, target_group)
                if method == 'POST':
                    return create_records(rtype, body, target_group)
            if len(parts) == 2 and parts[0] == kind:
                if method == 'PUT':
                    return update_record(rtype, parts[1], body, target_group)
                if method == 'DELETE':
                    return delete_record(rtype, parts[1], body, target_group)

        return _response(404, {'error': 'not found'})
    except Exception as e:
        return _response(500, {'error': str(e)})


# ---------- helpers ----------

def _scan_type(record_type, group_id=None):
    items = finance_table.scan().get('Items', [])
    items = [i for i in items if i.get('record_type') == record_type]
    if group_id is not None:
        # Records stamped with a different group are invisible to this group.
        # Legacy records all carry the default group_id after the Stage 1
        # migration, so nothing is silently hidden.
        items = [i for i in items if i.get('group_id') == group_id]
    return items


def _num(v, default=0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


ALLOWED_FIELDS = {
    'expense': ['month', 'year', 'slot', 'item', 'estimated_cost', 'actual_cost',
                'estimated_qty', 'actual_qty'],
    'membership': ['month', 'year', 'slot', 'display_name', 'player_id', 'status', 'remark',
                    'attended_briefly', 'attendance_note'],
    'walkin': ['date', 'slot', 'display_name', 'player_id', 'fee', 'skill',
               'recruit_verdict', 'note'],
}
NUMERIC_FIELDS = {'estimated_cost', 'actual_cost', 'estimated_qty', 'actual_qty', 'fee', 'year'}
REQUIRED_FIELDS = {
    'expense': ['month', 'year', 'slot', 'item'],
    'membership': ['month', 'year', 'slot', 'display_name', 'status'],
    'walkin': ['date', 'slot', 'display_name'],
}


def _clean(record_type, data):
    out = {}
    for f in ALLOWED_FIELDS[record_type]:
        if f not in data or data[f] is None or data[f] == '':
            continue
        v = data[f]
        if f in NUMERIC_FIELDS:
            v = Decimal(str(v))
        out[f] = v
    return out


def _resolve_name(pid_cache, player_id):
    if not player_id:
        return None
    if player_id not in pid_cache:
        p = players_table.get_item(Key={'player_id': player_id}).get('Item')
        pid_cache[player_id] = p['name'] if p else None
    return pid_cache[player_id]


# ---------- CRUD ----------

def list_records(record_type, params, group_id=None):
    items = _scan_type(record_type, group_id)
    if record_type == 'walkin':
        # Walk-ins carry an ISO date rather than month/year fields, so
        # month/year filters translate to a date prefix.
        if params.get('slot'):
            items = [i for i in items if str(i.get('slot')) == str(params['slot'])]
        if params.get('year'):
            prefix = f"{int(params['year']):04d}"
            if params.get('month') and params['month'] in MONTHS:
                prefix += f"-{MONTHS.index(params['month']) + 1:02d}"
            items = [i for i in items if str(i.get('date', '')).startswith(prefix)]
    else:
        for f in ('month', 'year', 'slot'):
            if params.get(f):
                items = [i for i in items if str(i.get(f)) == str(params[f])]
    # Live player names for linked records - a rename shows up immediately.
    cache = {}
    for i in items:
        live = _resolve_name(cache, i.get('player_id'))
        if live:
            i['display_name'] = live
    if record_type == 'walkin':
        items.sort(key=lambda i: (i.get('date', ''), i.get('display_name', '')), reverse=True)
    elif record_type == 'expense':
        # Latest month at the top; slots/items ordered within it.
        items.sort(key=lambda i: (-int(_num(i.get('year'))),
                                   -(MONTHS.index(i['month']) if i.get('month') in MONTHS else -99),
                                   i.get('slot', ''), i.get('item', '')))
    else:
        items.sort(key=lambda i: (int(_num(i.get('year'))), MONTHS.index(i['month']) if i.get('month') in MONTHS else 99,
                                   i.get('slot', ''), i.get('display_name', '')))
    resp = {record_type + 's': items}
    if record_type == 'membership' and params.get('month') and params.get('year') and params.get('slot'):
        row = _settlement_rows(group_id).get((str(params['month']), int(_num(params['year'])), str(params['slot'])))
        if row:
            resp['cost_per_head'] = row['cost_per_head']
            resp['estimated_total'] = row['estimated_total']
    return _response(200, resp)


def create_records(record_type, body, group_id=None):
    raw_items = body.get('items') if isinstance(body.get('items'), list) else [body]
    created = []
    errors = []
    for raw in raw_items:
        missing = [f for f in REQUIRED_FIELDS[record_type] if not raw.get(f) and raw.get(f) != 0]
        if missing:
            errors.append({'input': raw, 'error': f"missing required field(s): {', '.join(missing)}"})
            continue
        item = _clean(record_type, raw)
        item['record_id'] = str(uuid.uuid4())
        item['record_type'] = record_type
        if group_id:
            item['group_id'] = group_id
        finance_table.put_item(Item=item)
        created.append(item['record_id'])
    result = {'created': created}
    if errors:
        result['errors'] = errors
    return _response(200, result)


def update_record(record_type, record_id, body, group_id=None):
    existing = finance_table.get_item(Key={'record_id': record_id}).get('Item')
    if not existing or existing.get('record_type') != record_type:
        return _response(404, {'error': f'{record_type} not found'})
    # Can't reach across into another group's ledger.
    if group_id is not None and existing.get('group_id') not in (None, group_id):
        return _response(403, {'error': 'this record belongs to a different group'})

    # Payment confirmation stores the per-head AMOUNT confirmed. Validity is
    # derived, not stored: if expenses change or the Yes-roster changes, the
    # current per-head shifts away from the confirmed amount and the
    # confirmation automatically shows as needing re-confirmation.
    if record_type == 'membership' and 'confirm_payment' in body:
        if body['confirm_payment']:
            row = _settlement_rows(existing.get('group_id')).get((str(existing.get('month')), int(_num(existing.get('year'))),
                                           str(existing.get('slot'))))
            cph = row['cost_per_head'] if row else None
            if cph is None:
                return _response(400, {'error': 'per-head amount is not computable yet (no expenses or no Yes members)'})
            existing['payment_confirmed_amount'] = Decimal(str(cph))
        else:
            existing.pop('payment_confirmed_amount', None)
        finance_table.put_item(Item=existing)
        return _response(200, {'updated': record_id,
                                'payment_confirmed_amount': str(existing.get('payment_confirmed_amount', ''))})

    updates = _clean(record_type, body)
    if record_type == 'membership' and 'attended_briefly' in body:
        if body['attended_briefly']:
            existing['attended_briefly'] = True
        else:
            existing.pop('attended_briefly', None)
            existing.pop('attendance_note', None)
    # Explicit unlink: player_id: null in the body clears the link.
    if 'player_id' in body and body['player_id'] in (None, ''):
        existing.pop('player_id', None)
    if not updates and 'player_id' not in body:
        return _response(400, {'error': 'no updatable fields supplied'})
    existing.update(updates)
    finance_table.put_item(Item=existing)
    return _response(200, {'updated': record_id})


def delete_record_enforced(record_type, record_id, event):
    """Triple-gated: SuperAdmin identity + FINANCE_VIEW_KEY + the existing
    CONFIRMATION_CODE (checked inside delete_record itself). Any one
    missing or wrong -> rejected. Finance data isn't scoped per-group like
    club Groups are, so there's no owner/admin tier here - just the one
    global SuperAdmin bar, matching how FINANCE_VIEW_KEY always worked as
    a single shared gate rather than a per-group one."""
    if record_type not in ('expense', 'membership', 'walkin'):
        return _response(400, {'error': f'unknown record_type: {record_type}'})

    claims = _caller_claims(event)
    if not _is_super_admin(claims):
        return _response(403, {'error': 'SuperAdmin required to delete finance records'})

    body = json.loads(event.get('body') or '{}')
    params = event.get('queryStringParameters') or {}
    supplied_key = params.get('view_key') or body.get('view_key')
    if supplied_key != VIEW_KEY:
        return _response(403, {'error': 'view key is missing or incorrect'})

    return delete_record(record_type, record_id, body)


def delete_record(record_type, record_id, body, group_id=None):
    if body.get('confirm') != CONFIRMATION_CODE:
        return _response(400, {'error': 'confirmation code is missing or incorrect'})
    existing = finance_table.get_item(Key={'record_id': record_id}).get('Item')
    if not existing or existing.get('record_type') != record_type:
        return _response(404, {'error': f'{record_type} not found'})
    if group_id is not None and existing.get('group_id') not in (None, group_id):
        return _response(403, {'error': 'this record belongs to a different group'})
    finance_table.delete_item(Key={'record_id': record_id})
    return _response(200, {'deleted': record_id})


# ---------- settings + public walk-ins ----------

def get_settings():
    item = finance_table.get_item(Key={'record_id': 'settings'}).get('Item') or {}
    return _response(200, {
        'walkins_public': bool(item.get('walkins_public', False)),
        'upi_id': item.get('upi_id', ''),
        'upi_name': item.get('upi_name', '')
    })


def put_settings(body):
    # Preserve existing values for any field the caller didn't send, so
    # saving the walk-in toggle doesn't wipe the UPI details and vice versa.
    existing = finance_table.get_item(Key={'record_id': 'settings'}).get('Item') or {}
    item = {
        'record_id': 'settings', 'record_type': 'settings',
        'walkins_public': bool(body.get('walkins_public', existing.get('walkins_public', False))),
        'upi_id': (body.get('upi_id') if 'upi_id' in body else existing.get('upi_id', '')) or '',
        'upi_name': (body.get('upi_name') if 'upi_name' in body else existing.get('upi_name', '')) or ''
    }
    finance_table.put_item(Item=item)
    return _response(200, {'walkins_public': item['walkins_public'],
                           'upi_id': item['upi_id'], 'upi_name': item['upi_name']})


def public_upi():
    """The pay card is shown to guests (they pay walk-in fees), so the UPI
    ID must be readable without the finance key. Only the payment details
    are exposed - nothing financial about the club."""
    item = finance_table.get_item(Key={'record_id': 'settings'}).get('Item') or {}
    return _response(200, {'upi_id': item.get('upi_id', ''), 'upi_name': item.get('upi_name', '')})


def my_settlement(claims, group_id):
    """A single member's own dues in a group: for every (month, slot) where
    they were a confirmed ('Yes') member, what they were expected to pay
    (cost_per_head) and what is owed back to them (residual_per_head, the
    relief/refund - this is the walk-in share returned when actuals came in
    under estimate). Members can see ONLY their own lines; expenses and other
    members' numbers are never exposed. Available to any member of the group,
    regardless of finance role."""
    pid = claims.get('custom:player_id')
    if not pid:
        return _response(403, {'error': 'link your profile to see your dues'})
    if not groups_table or not group_id:
        return _response(400, {'error': 'group is not specified'})
    group = groups_table.get_item(Key={'group_id': group_id}).get('Item') or {}
    if pid not in group.get('member_ids', []) and not _is_super_admin(claims):
        return _response(403, {'error': 'you are not a member of this group'})

    rows = _settlement_rows(group_id)
    memberships = _scan_type('membership', group_id)
    lines = []
    owe_total = 0.0
    owed_back_total = 0.0
    for m in memberships:
        if m.get('player_id') != pid or m.get('status') != 'Yes':
            continue
        key = (str(m.get('month')), int(_num(m.get('year'))), str(m.get('slot')))
        b = rows.get(key)
        if not b:
            continue
        cph = b.get('cost_per_head')
        rph = b.get('residual_per_head')
        confirmed_amt = m.get('payment_confirmed_amount')
        # Unpaid if we have no confirmed amount matching the current per-head.
        paid = confirmed_amt is not None and cph is not None and abs(_num(confirmed_amt) - cph) < 0.01
        owe = 0.0 if (paid or cph is None) else cph
        owed_back = rph or 0.0
        owe_total += owe
        owed_back_total += owed_back
        lines.append({
            'month': b['month'], 'year': b['year'], 'slot': b['slot'],
            'expected_per_head': cph,
            'you_paid': paid,
            'you_owe': round(owe, 2),
            'owed_back_to_you': round(owed_back, 2),
            'collection_status': b.get('collection_status'),
        })
    lines.sort(key=lambda l: (l['year'], l['month'], l['slot']), reverse=True)
    payee = group.get('finance_payee') or {}
    return _response(200, {
        'group_id': group_id,
        'group_name': group.get('group_name'),
        'lines': lines,
        'total_you_owe': round(owe_total, 2),
        'total_owed_back_to_you': round(owed_back_total, 2),
        'net': round(owed_back_total - owe_total, 2),  # positive = club owes you
        # Payee (Stage 5/6): who collects, so the client can build a UPI
        # deep-link. Only the VPA + display name are exposed - member-gated
        # (this route requires group membership), never on a public route.
        'payee': {'upi_id': payee.get('upi_id') or '', 'upi_name': payee.get('upi_name') or ''},
    })


def public_walkins():
    settings = finance_table.get_item(Key={'record_id': 'settings'}).get('Item') or {}
    if not settings.get('walkins_public'):
        return _response(404, {'error': 'not available'})
    # Public list is the club-wide default group only (guests pay walk-in
    # fees for the main club sessions).
    items = _scan_type('walkin', _default_group_id())
    cache = {}
    rows = []
    for i in items:
        name = _resolve_name(cache, i.get('player_id')) or i.get('display_name')
        # Names, dates, and slots ONLY. Fees, skill ratings, and recruit
        # verdicts never leave the keyed routes.
        rows.append({'display_name': name, 'date': i.get('date'), 'slot': i.get('slot')})
    rows.sort(key=lambda r: (r['date'] or '', r['display_name'] or ''))
    return _response(200, {'walkins': rows})


# ---------- settlement summary (the Excel formulas, retired with honor) ----------

def _settlement_rows(group_id=None):
    """Per (month, year, slot): the exact math from the Calculations sheet.
        estimated_total = SUM(estimated_cost * estimated_qty)
        actual_total    = SUM(actual_cost * actual_qty)   [falls back to estimated]
        extra_collected = SUM(walk-in fees for that month+slot)
        player_count    = COUNT(memberships with status Yes)
        cost_per_head   = estimated_total / player_count   [what members paid]
        residual_per_head = (estimated_total - actual_total + extra_collected)
                             / player_count                [relief owed each]
    """
    expenses = _scan_type('expense', group_id)
    memberships = _scan_type('membership', group_id)
    walkins = _scan_type('walkin', group_id)

    periods = {}

    def bucket(month, year, slot):
        key = (str(month), int(_num(year)), str(slot))
        return periods.setdefault(key, {
            'month': key[0], 'year': key[1], 'slot': key[2],
            'estimated_total': 0.0, 'actual_total': 0.0,
            'extra_collected': 0.0, 'player_count': 0, 'items': []
        })

    for e in expenses:
        b = bucket(e.get('month'), e.get('year'), e.get('slot'))
        est = _num(e.get('estimated_cost')) * _num(e.get('estimated_qty'), 1)
        act_cost = e.get('actual_cost')
        act = (_num(act_cost) if act_cost is not None else _num(e.get('estimated_cost'))) \
            * _num(e.get('actual_qty'), _num(e.get('estimated_qty'), 1))
        b['estimated_total'] += est
        b['actual_total'] += act
        b['items'].append({'item': e.get('item'), 'estimated': est, 'actual': act})

    for m in memberships:
        if m.get('status') == 'Yes':
            bucket(m.get('month'), m.get('year'), m.get('slot'))['player_count'] += 1

    for w in walkins:
        date = w.get('date') or ''
        if len(date) >= 7:
            year, month_num = date[:4], date[5:7]
            try:
                month = MONTHS[int(month_num) - 1]
            except (ValueError, IndexError):
                continue
            bucket(month, year, w.get('slot'))['extra_collected'] += _num(w.get('fee'))

    # Collection status per period: a Yes member counts as confirmed only
    # while their stored confirmed amount equals the CURRENT per-head.
    confirmed = {}
    for m in memberships:
        if m.get('status') == 'Yes' and m.get('payment_confirmed_amount') is not None:
            key = (str(m.get('month')), int(_num(m.get('year'))), str(m.get('slot')))
            confirmed.setdefault(key, []).append(_num(m.get('payment_confirmed_amount')))

    for key, b in periods.items():
        count = b['player_count']
        b['difference'] = round(b['estimated_total'] - b['actual_total'], 2)
        b['cost_per_head'] = round(b['estimated_total'] / count, 2) if count else None
        b['residual_per_head'] = round(
            (b['estimated_total'] - b['actual_total'] + b['extra_collected']) / count, 2) if count else None
        b['estimated_total'] = round(b['estimated_total'], 2)
        b['actual_total'] = round(b['actual_total'], 2)
        b['extra_collected'] = round(b['extra_collected'], 2)
        if count and b['cost_per_head'] is not None:
            b['confirmed_count'] = sum(1 for amt in confirmed.get(key, [])
                                        if abs(amt - b['cost_per_head']) < 0.01)
            b['collection_status'] = 'settled' if b['confirmed_count'] >= count else 'collecting'
        else:
            b['confirmed_count'] = 0
            b['collection_status'] = None

    return periods


def summary(group_id=None):
    rows = list(_settlement_rows(group_id).values())
    rows.sort(key=lambda r: (r['year'], MONTHS.index(r['month']) if r['month'] in MONTHS else 99, r['slot']))
    return _response(200, {'summary': rows})


# ---- insights: per-member monthly economics + ghosts + conversion ----

# The site started capturing matches mid-month (first match July 19), so a
# calendar month's real game count is undercounted for anyone who was
# playing before tracking began. Estimation model (from club reality:
# roughly 15 sessions in the 18 untracked days, 4-5 games per session):
AVG_GAMES_PER_SESSION = 4.5
SESSION_RATE = 15.0 / 18.0          # sessions actually held per untracked day
ACTIVE_DAYS_THRESHOLD = 10          # below this, offer the estimated count


def insights(group_id=None):
    """Per-member monthly economics, ghosts, and walk-in conversion.

    Effective cost model (per member, per month, ACROSS slots):
        paid      = sum of cost_per_head for every slot enrolled (Yes) this month
        relief    = sum of last month's residual_per_head for every slot the
                     member was enrolled in LAST month (residuals aren't cash
                     refunds - they discount this month's collection)
        effective = paid - relief
        cost/match = effective / matches played this calendar month

    Match counts come in two flavours the UI can toggle:
        matches_actual    - recorded matches in that calendar month
        matches_estimated - actual + AVG_GAMES_PER_SESSION x estimated
                             sessions missed before tracking started, applied
                             only when the member has fewer than
                             ACTIVE_DAYS_THRESHOLD recorded play days and the
                             month has untracked days
    Months that ended entirely before tracking began get no cost/match rows.
    """
    params_holder = getattr(insights, '_params', {}) or {}
    f_month = params_holder.get('month')
    f_year = int(_num(params_holder.get('year'))) if params_holder.get('year') else None

    memberships = _scan_type('membership', group_id)
    walkins = _scan_type('walkin', group_id)
    matches = matches_table.scan().get('Items', [])
    settlement = _settlement_rows(group_id)

    # matches per (player_id, 'yyyy-mm') and distinct active days
    played, active_days = {}, {}
    tracking_start = None
    for m in matches:
        date = (m.get('date') or '')
        ym, day = date[:7], date[:10]
        if len(ym) != 7:
            continue
        if tracking_start is None or day < tracking_start:
            tracking_start = day
        for pid in (m.get('team_a') or []) + (m.get('team_b') or []):
            played[(pid, ym)] = played.get((pid, ym), 0) + 1
            active_days.setdefault((pid, ym), set()).add(day)

    def month_key(month, year):
        return f"{year:04d}-{MONTHS.index(month) + 1:02d}"

    def prev_period(month, year):
        i = MONTHS.index(month)
        return (MONTHS[i - 1], year - 1 if i == 0 else year)

    # memberships grouped per (member identity, month, year)
    cache = {}
    by_member_month = {}
    for mem in memberships:
        if mem.get('status') != 'Yes' or mem.get('month') not in MONTHS:
            continue
        month, year = str(mem['month']), int(_num(mem.get('year')))
        ident = mem.get('player_id') or f"name:{mem.get('display_name')}"
        entry = by_member_month.setdefault((ident, month, year), {
            'player_id': mem.get('player_id'),
            'display_name': _resolve_name(cache, mem.get('player_id')) or mem.get('display_name'),
            'slots': []
        })
        entry['month'], entry['year'] = month, year
        entry['slots'].append(str(mem.get('slot')))

    ghosts, cost_rows = [], []
    for (ident, month, year), entry in by_member_month.items():
        if f_year and year != f_year:
            continue
        if f_month and month != f_month:
            continue
        ym = month_key(month, year)
        pid = entry['player_id']

        paid = 0.0
        payable = True
        paid_breakdown = []
        for slot in entry['slots']:
            srow = settlement.get((month, year, slot)) or {}
            cph = srow.get('cost_per_head')
            if cph is None:
                payable = False
                paid_breakdown.append({'slot': slot, 'per_head': None})
            else:
                paid += cph
                paid_breakdown.append({'slot': slot, 'per_head': cph,
                                        'total': srow.get('estimated_total'),
                                        'members': srow.get('player_count')})

        p_month, p_year = prev_period(month, year)
        relief = 0.0
        for mem2 in memberships:
            if (mem2.get('status') == 'Yes' and str(mem2.get('month')) == p_month
                    and int(_num(mem2.get('year'))) == p_year
                    and (mem2.get('player_id') or f"name:{mem2.get('display_name')}") == ident):
                res = (settlement.get((p_month, p_year, str(mem2.get('slot')))) or {}).get('residual_per_head')
                if res:
                    relief += res

        attended_briefly = any(
            mem3.get('attended_briefly') for mem3 in memberships
            if mem3.get('status') == 'Yes' and str(mem3.get('month')) == month
            and int(_num(mem3.get('year'))) == year
            and (mem3.get('player_id') or f"name:{mem3.get('display_name')}") == ident)
        attendance_note = next((mem3.get('attendance_note') for mem3 in memberships
                                 if mem3.get('attendance_note') and str(mem3.get('month')) == month
                                 and int(_num(mem3.get('year'))) == year
                                 and (mem3.get('player_id') or f"name:{mem3.get('display_name')}") == ident), None)
        n_actual = played.get((pid, ym), 0) if pid else None
        n_days = len(active_days.get((pid, ym), set())) if pid else 0

        # months entirely before tracking have no match data at all
        month_end = f"{ym}-31"
        month_tracked = tracking_start is not None and month_end >= tracking_start
        untracked_days = 0
        if tracking_start and tracking_start[:7] == ym:
            untracked_days = int(tracking_start[8:10]) - 1

        n_estimated = n_actual
        estimated_applied = False
        if (pid and month_tracked and untracked_days > 0 and n_days < ACTIVE_DAYS_THRESHOLD):
            n_estimated = (n_actual or 0) + int(round(AVG_GAMES_PER_SESSION * SESSION_RATE * untracked_days))
            estimated_applied = True

        effective = round(paid - relief, 2) if payable else None
        row = {
            'month': month, 'year': year,
            'display_name': entry['display_name'], 'linked': bool(pid),
            'slots': sorted(entry['slots']),
            'paid': round(paid, 2) if payable else None,
            'relief': round(relief, 2),
            'effective_cost': effective,
            'matches_actual': n_actual,
            'matches_estimated': n_estimated,
            'estimated_applied': estimated_applied,
            'active_days': n_days,
            'cost_per_match_actual': (round(effective / n_actual, 2)
                                       if effective is not None and n_actual else None),
            'cost_per_match_estimated': (round(effective / n_estimated, 2)
                                          if effective is not None and n_estimated else None),
            'month_tracked': month_tracked,
            'paid_breakdown': paid_breakdown,
        }
        if month_tracked:
            cost_rows.append(row)
        if pid and month_tracked and n_actual == 0:
            ghost = {'month': month, 'year': year, 'display_name': entry['display_name'],
                      'slots': sorted(entry['slots']),
                      'membership_ids': [mem4['record_id'] for mem4 in memberships
                                          if (mem4.get('player_id') or f"name:{mem4.get('display_name')}") == ident
                                          and str(mem4.get('month')) == month and int(_num(mem4.get('year'))) == year],
                      'attended_briefly': attended_briefly, 'attendance_note': attendance_note}
            ghosts.append(ghost)

    noted_attended = [g for g in ghosts if g['attended_briefly']]
    ghosts = [g for g in ghosts if not g['attended_briefly']]
    for lst in (ghosts, noted_attended):
        lst.sort(key=lambda r: (r['year'], MONTHS.index(r['month']), r['display_name']))
    cost_rows.sort(key=lambda r: (-r['year'], -MONTHS.index(r['month']),
                                    -(r['cost_per_match_actual'] or 10 ** 9), r['display_name']))

    # Walk-in -> member conversion. Linking rules: a LINKED walk-in matches
    # memberships by player_id; an UNLINKED walk-in may only name-match
    # UNLINKED memberships - if a guest was deliberately left unlinked
    # despite sharing a roster name (the two Mohits), they are different
    # people and must never be counted as converted.
    member_pids = {m.get('player_id') for m in memberships if m.get('status') == 'Yes' and m.get('player_id')}
    unlinked_member_names = {m.get('display_name') for m in memberships
                              if m.get('status') == 'Yes' and not m.get('player_id')}
    guests = {}
    for w in walkins:
        if _num(w.get('fee')) < 0:
            continue
        gkey = w.get('player_id') or f"name:{w.get('display_name')}"
        g = guests.setdefault(gkey, {'display_name': _resolve_name(cache, w.get('player_id')) or w.get('display_name'),
                                      'sessions': 0, 'fees_paid': 0.0, 'recruit_verdict': None,
                                      'became_member': False})
        g['sessions'] += 1
        g['fees_paid'] = round(g['fees_paid'] + _num(w.get('fee')), 2)
        if w.get('recruit_verdict'):
            g['recruit_verdict'] = w.get('recruit_verdict')
        if w.get('player_id'):
            g['became_member'] = w['player_id'] in member_pids
        else:
            g['became_member'] = w.get('display_name') in unlinked_member_names
    guest_rows = sorted(guests.values(), key=lambda g: (-g['became_member'], -g['sessions']))
    conversion = {
        'total_guests': len(guest_rows),
        'became_members': sum(1 for g in guest_rows if g['became_member']),
        'guests': guest_rows,
    }

    return _response(200, {'ghosts': ghosts, 'noted_attended': noted_attended,
                            'cost_rows': cost_rows, 'conversion': conversion,
                            'tracking_start': tracking_start})


def _response(status_code, body_dict):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body_dict, default=str)
    }
