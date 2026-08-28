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
                               note, sessions_covered?}  (negative fee =
                               refund/adjustment; sessions_covered is how
                               many sessions this one fee entry pays for -
                               omitted/blank means 1, the normal case for a
                               guest who pays per session. A lump sum that
                               settles several sessions at once - paid
                               upfront for a stretch, or paid in one go at
                               month's end - should set this to the real
                               count so "sessions paid" in Insights reflects
                               reality instead of a raw count of fee
                               entries. Same idea covers 2 slots paid for in
                               one entry on the same day.)
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
import re
import uuid
from datetime import datetime, timedelta, timezone
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
GROUP_SLOT = '(whole group)'   # sentinel slot for slot-less, group-wide records
_default_group_id_cache = None


def _scan_all(table, **kw):
    """Full-table scan that follows LastEvaluatedKey - a bare .scan() returns
    only the first 1 MB page (KNOWN_ISSUES #15). This lambda's tables
    (finance records, groups, matches) all grow unbounded over the club's
    life, so silently truncating here would mean settlement math, group
    lookups, and match-log attendance quietly go wrong past the boundary
    with no error - worse than a crash. Copied from matches/players lambdas
    (KNOWN_ISSUES #6 - not shared, keep every copy in sync)."""
    items, last = [], None
    while True:
        if last:
            kw['ExclusiveStartKey'] = last
        resp = table.scan(**kw)
        items.extend(resp.get('Items', []))
        last = resp.get('LastEvaluatedKey')
        if not last:
            return items

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
    for g in _scan_all(groups_table):
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


def _slot_key(slot):
    """Normalize a record's slot for bucketing/comparison: a missing/blank
    slot (group-wide expense or walk-in) collapses to the GROUP_SLOT
    sentinel, same as everywhere else that buckets by (month, year, slot)."""
    return GROUP_SLOT if slot in (None, '', 'None') else str(slot)


def _member_assigned_slots(pid, group):
    """The set of slots (raw, already-normalized strings) a player is
    assigned to within a group, per that group's slot_members map."""
    if not pid:
        return set()
    slot_members = (group or {}).get('slot_members') or {}
    return {slot for slot, members in slot_members.items() if pid in (members or [])}


def _view_scope_slots(claims, group_id, level):
    """Stage 4c: a plain 'view'-level grant only sees their own assigned
    slot(s) in the main ledger (list_records/summary), not the whole
    group's finances - the group-wide (slot-less) bucket stays visible to
    everyone since it's a shared cost split across all distinct members
    regardless of slot. Returns None for unrestricted (sees everything):
    SuperAdmin, group owner/admin, or anyone with write/delete (those
    tiers are trusted to manage the whole ledger). Returns a set of
    allowed slot keys (always including GROUP_SLOT) when restricted.
    Own-dues-only access (Stage 4b's my-settlement) is unaffected by this -
    that's a separate, always-own-only route."""
    if level > FINANCE_LEVELS['view']:
        return None
    if _is_super_admin(claims):
        return None
    if not groups_table or not group_id:
        return None
    pid = claims.get('custom:player_id')
    group = groups_table.get_item(Key={'group_id': group_id}).get('Item') or {}
    if pid and group.get('roles', {}).get(pid) in ('owner', 'admin'):
        return None
    scope = _member_assigned_slots(pid, group)
    scope.add(GROUP_SLOT)
    return scope


def _has_any_group_finance(claims):
    """True if the caller has finance access in ANY group (owner/admin, or a
    per-group finance_roles entry). Lets group owners obtain the shared key
    during the transition, before it's retired."""
    pid = claims.get('custom:player_id')
    if not pid or not groups_table:
        return False
    for g in _scan_all(groups_table):
        if g.get('roles', {}).get(pid) in ('owner', 'admin'):
            return True
        if (g.get('finance_roles') or {}).get(pid) in FINANCE_LEVELS:
            return True
    return False


def _effective_finance_role(claims, group_id=None):
    """The role name to REPORT to the frontend for button visibility: the
    higher of the caller's legacy/global role and their per-group role for
    the group actually being viewed (if known). Real enforcement never
    trusts this - every finance call re-checks _group_finance_level itself
    - this only decides which buttons the UI shows.

    Bug this fixes (Owner-reported 2026-08-20: "a deletion is not enabled,
    i can only see edit option"): finance_key_for_caller used to report
    ONLY _finance_role (the legacy GLOBAL role / default-group transition
    floor), completely ignoring group ownership. A group owner with no
    legacy finance_role attribute on their player record got reported as
    whatever that global check returned (here: 'write', from an old
    finance_access grant) even though _group_finance_level already
    correctly grants owners/admins 'delete' on their OWN group - so Delete
    buttons stayed hidden for someone the backend would have let delete."""
    level = _finance_level(claims)
    if group_id:
        level = max(level, _group_finance_level(claims, group_id))
    for name, lvl in FINANCE_LEVELS.items():
        if lvl == level:
            return name
    return 'none'


def finance_key_for_caller(event):
    """Hands the shared view key to any caller with finance access - global
    (legacy) OR in any group (owner/admin/per-group role) - plus their
    EFFECTIVE role for the group being viewed (see _effective_finance_role)
    so the UI can hide write/delete controls correctly. Real per-group
    enforcement happens on each finance call regardless of what this
    reports."""
    claims = _caller_claims(event)
    if not claims:
        return _response(403, {'error': 'log in to access finance'})
    if not (_has_finance_access(claims) or _has_any_group_finance(claims)):
        return _response(403, {'error': 'you do not have finance access - ask an admin'})
    params = event.get('queryStringParameters') or {}
    return _response(200, {'view_key': VIEW_KEY,
                           'finance_role': _effective_finance_role(claims, params.get('group_id'))})


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
        scope_slots = None
        if claims_for_role:
            lvl = _group_finance_level(claims_for_role, target_group)
            if method == 'GET' and lvl < FINANCE_LEVELS['view']:
                return _response(403, {'error': 'you do not have finance access to this group'})
            if method in ('POST', 'PUT') and lvl < FINANCE_LEVELS['write']:
                return _response(403, {'error': 'you have view-only finance access here - ask an owner for write access'})
            if method == 'DELETE' and lvl < FINANCE_LEVELS['delete']:
                return _response(403, {'error': 'you need delete access for this - ask an owner'})
            # Stage 4c: a view-only grant is narrowed to their own slot(s) in
            # the main ledger (list_records/summary only - other routes are
            # unaffected).
            scope_slots = _view_scope_slots(claims_for_role, target_group, lvl)

        if parts == ['summary'] and method == 'GET':
            return summary(target_group, scope_slots)
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
                    return list_records(rtype, params, target_group, scope_slots)
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
    items = _scan_all(finance_table)
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
                    'attended_briefly', 'attendance_note', 'forfeit_residual'],
    'walkin': ['date', 'slot', 'display_name', 'player_id', 'fee', 'skill',
               'recruit_verdict', 'note', 'sessions_covered'],
}
NUMERIC_FIELDS = {'estimated_cost', 'actual_cost', 'estimated_qty', 'actual_qty', 'fee', 'year',
                   'sessions_covered'}
REQUIRED_FIELDS = {
    'expense': ['month', 'year', 'item'],          # slot optional -> group-wide
    'membership': ['month', 'year', 'slot', 'display_name', 'status'],
    'walkin': ['date', 'display_name'],             # slot optional -> group-wide
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

def _prev_period(month, year):
    i = MONTHS.index(month)
    return (MONTHS[i - 1], year - 1 if i == 0 else year)


def _next_period(month, year):
    i = MONTHS.index(month)
    return (MONTHS[(i + 1) % 12], year + 1 if i == 11 else year)


def _member_relief(settlement, memberships, ident, month, year, slot=None):
    """Relief a member gets in (month, year): the previous month's residual.
    If `slot` is given, ONLY that slot's relief (used for the per-slot member
    card / confirm / settled-check, so a member in two slots doesn't get both
    slots' relief subtracted on each). If `slot` is None, summed across all the
    member's slots - their total for the month (used by the aggregated Insights
    row). A slot the member forfeited contributes nothing."""
    p_month, p_year = _prev_period(month, year)
    relief = 0.0
    for m in memberships:
        if (m.get('status') == 'Yes' and str(m.get('month')) == p_month
                and int(_num(m.get('year'))) == p_year
                and (m.get('player_id') or f"name:{m.get('display_name')}") == ident):
            if m.get('forfeit_residual'):
                continue
            if slot is not None and str(m.get('slot')) != str(slot):
                continue  # per-slot: only this slot's own relief
            res = (settlement.get((p_month, p_year, str(m.get('slot')))) or {}).get('residual_per_head')
            if res:
                relief += res
    return round(relief, 2)


def list_records(record_type, params, group_id=None, scope_slots=None):
    items = _scan_type(record_type, group_id)
    if scope_slots is not None:
        # Stage 4c: narrow a view-only caller to their assigned slot(s) +
        # the group-wide bucket, across all three record types (expenses,
        # walk-ins, and membership rosters - a slot-scoped viewer shouldn't
        # see who's enrolled in a slot that isn't theirs either).
        items = [i for i in items if _slot_key(i.get('slot')) in scope_slots]
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
    if scope_slots is not None:
        resp['scoped_to'] = sorted(scope_slots)
    if record_type == 'membership' and params.get('month') and params.get('year') and params.get('slot'):
        settlement = _settlement_rows(group_id)
        row = settlement.get((str(params['month']), int(_num(params['year'])), str(params['slot'])))
        if row:
            resp['cost_per_head'] = row['cost_per_head']
            resp['estimated_total'] = row['estimated_total']
            cph = row['cost_per_head']
            # Per-member relief + effective (what they actually pay) so the
            # card and the confirm dialog don't need the Insights tab.
            all_mem = _scan_type('membership', group_id)
            for it in items:
                ident = it.get('player_id') or f"name:{it.get('display_name')}"
                relief = _member_relief(settlement, all_mem, ident,
                                        str(params['month']), int(_num(params['year'])),
                                        slot=str(params['slot']))
                it['relief'] = relief
                if cph is not None:
                    it['effective'] = round(max(cph - relief, 0), 2)
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
            settlement = _settlement_rows(existing.get('group_id'))
            row = settlement.get((str(existing.get('month')), int(_num(existing.get('year'))),
                                  str(existing.get('slot'))))
            cph = row['cost_per_head'] if row else None
            if cph is None:
                return _response(400, {'error': 'per-head amount is not computable yet (no expenses or no Yes members)'})
            # Store what they ACTUALLY pay = per-head minus their relief, so the
            # confirmed amount matches the collected amount (not the pre-relief
            # figure). Change-detection still works: if cost or relief shifts,
            # the effective amount shifts and the confirmation shows as stale.
            ident = existing.get('player_id') or f"name:{existing.get('display_name')}"
            all_mem = _scan_type('membership', existing.get('group_id'))
            relief = _member_relief(settlement, all_mem, ident,
                                    str(existing.get('month')), int(_num(existing.get('year'))),
                                    slot=str(existing.get('slot')))
            effective = round(max(cph - relief, 0), 2)
            existing['payment_confirmed_amount'] = Decimal(str(effective))
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
    # Forfeit last month's residual (leaver whose relief is redistributed to
    # the others). Stored only when true, so it never affects normal members.
    if record_type == 'membership' and 'forfeit_residual' in body:
        updates.pop('forfeit_residual', None)
        if body['forfeit_residual']:
            existing['forfeit_residual'] = True
        else:
            existing.pop('forfeit_residual', None)
    # Explicit unlink: player_id: null in the body clears the link.
    if 'player_id' in body and body['player_id'] in (None, ''):
        existing.pop('player_id', None)
    flag_only = ('forfeit_residual' in body) or ('attended_briefly' in body)
    if not updates and 'player_id' not in body and not flag_only:
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

DEFAULT_CLUB_UTC_OFFSET_MINUTES = 330  # IST (+05:30) - see club_utc_offset_minutes below


def get_settings():
    item = finance_table.get_item(Key={'record_id': 'settings'}).get('Item') or {}
    dwf = item.get('default_walkin_fee')
    return _response(200, {
        'walkins_public': bool(item.get('walkins_public', False)),
        'upi_id': item.get('upi_id', ''),
        'upi_name': item.get('upi_name', ''),
        # Club-wide expected per-visit fee for non-members - used only to
        # compute the "expected"/"pending" columns in insights()' guest
        # table (Owner-requested 2026-08-20). Optional and unset by default:
        # with no rate on file we show attendance/fees-collected only,
        # rather than guess at a number. Same single-record settings row as
        # walkins_public/upi_* (club-wide, not per-group - matches how those
        # already work; the add-walk-in form's own fee field already
        # defaults to 80 as a starting point, this makes that number a real
        # saved setting instead of just a form placeholder).
        'default_walkin_fee': float(dwf) if dwf is not None else None,
        # Minutes east of UTC, for converting a match's stored UTC timestamp
        # to local wall-clock time in the timing-check diagnostics below
        # (Owner-requested 2026-08-20). Matches are stored in UTC and slot
        # labels ("7AM-8AM") carry no timezone at all, so SOMETHING has to
        # be assumed - defaults to IST (+05:30) since that's the only
        # signal available (rupee currency throughout the app). Change this
        # if the club isn't actually in India.
        'club_utc_offset_minutes': int(item.get('club_utc_offset_minutes', DEFAULT_CLUB_UTC_OFFSET_MINUTES))
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
    if 'default_walkin_fee' in body:
        dwf = body.get('default_walkin_fee')
        if dwf not in (None, ''):
            item['default_walkin_fee'] = Decimal(str(dwf))
        # else: caller explicitly cleared it - leave unset (omitted, not
        # stored as a DynamoDB null) so get_settings reports None again.
    elif existing.get('default_walkin_fee') is not None:
        item['default_walkin_fee'] = existing['default_walkin_fee']
    item['club_utc_offset_minutes'] = Decimal(str(int(body.get('club_utc_offset_minutes'))) ) \
        if 'club_utc_offset_minutes' in body and body.get('club_utc_offset_minutes') not in (None, '') \
        else Decimal(str(existing.get('club_utc_offset_minutes', DEFAULT_CLUB_UTC_OFFSET_MINUTES)))
    finance_table.put_item(Item=item)
    resp_dwf = item.get('default_walkin_fee')
    return _response(200, {'walkins_public': item['walkins_public'],
                           'upi_id': item['upi_id'], 'upi_name': item['upi_name'],
                           'default_walkin_fee': float(resp_dwf) if resp_dwf is not None else None,
                           'club_utc_offset_minutes': int(item['club_utc_offset_minutes'])})


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
        # What confirm_payment actually stores is the EFFECTIVE amount
        # (cost_per_head minus that month's relief from the prior month's
        # residual - see update_record's confirm_payment branch), not the
        # raw per-head figure. Comparing against raw cph here made every
        # member who'd ever received relief (i.e. almost everyone, since
        # relief/residual carries over most months) show as unpaid even
        # right after confirming, because confirmed_amt (effective) would
        # never equal cph (pre-relief) whenever relief != 0. Match
        # _settlement_rows' own "settled" check: compare against the same
        # effective figure. (Owner-reported 2026-08-20: dues showed
        # pending despite already-confirmed payment.)
        ident = pid
        relief = _member_relief(rows, memberships, ident, b['month'], b['year'], slot=b['slot'])
        effective = round(max(cph - relief, 0), 2) if cph is not None else None
        paid = confirmed_amt is not None and effective is not None and abs(_num(confirmed_amt) - effective) < 0.01
        owe = 0.0 if (paid or effective is None) else effective
        # A member who forfeited this period's refund gets nothing back; their
        # share was redistributed to the others (reflected in rph for them).
        owed_back = 0.0 if m.get('forfeit_residual') else (rph or 0.0)
        # This residual isn't a standing cash balance sitting separately from
        # what's "you owe" above - it's auto-applied as relief AGAINST the
        # very next month's bill (see `effective` a few lines up, and
        # _member_relief). Once that's happened the money has already
        # changed hands (as a smaller bill, not a refund), so showing it
        # again here as "the club owes you X" double-counts it - the total
        # would claim credit for money that's already been spent. Net out
        # whatever the following month's Yes membership (same slot, not
        # itself forfeited) already consumed, capped at what that month
        # actually needed (relief beyond that month's cost isn't carried
        # further today - _member_relief only ever looks one month back).
        # (Owner-reported 2026-08-20: "already adjusted in this month's
        # amount, so it should be 0 as of now".)
        if owed_back > 0:
            nmonth, nyear = _next_period(b['month'], b['year'])
            has_next = any(mm.get('player_id') == pid and str(mm.get('slot')) == str(b['slot'])
                           and str(mm.get('month')) == nmonth and int(_num(mm.get('year'))) == nyear
                           and mm.get('status') == 'Yes'
                           for mm in memberships)
            if has_next:
                next_row = rows.get((nmonth, nyear, str(b['slot'])))
                next_cph = next_row.get('cost_per_head') if next_row else None
                consumed = min(owed_back, next_cph) if next_cph is not None else 0.0
                owed_back = round(owed_back - consumed, 2)
        owe_total += owe
        owed_back_total += owed_back
        lines.append({
            'month': b['month'], 'year': b['year'], 'slot': b['slot'],
            'expected_per_head': cph,
            'you_paid': paid,
            # The amount actually confirmed as paid (post-relief) - the
            # frontend showed a bare "paid" with no figure before; now it
            # can show what was paid, not just that it was (Owner-requested
            # 2026-08-20).
            'you_paid_amount': effective if paid else None,
            'you_owe': round(owe, 2),
            'owed_back_to_you': round(owed_back, 2),
            'collection_status': b.get('collection_status'),
        })
    # Group-wide (slot-less) share: for each month this member is a distinct
    # Yes member, add one "(whole group)" line with the shared cost + owed-back.
    member_months = {(str(m.get('month')), int(_num(m.get('year'))))
                     for m in memberships
                     if m.get('player_id') == pid and m.get('status') == 'Yes'}
    for (mth, yr) in member_months:
        gw = rows.get((mth, yr, GROUP_SLOT))
        if not gw or gw.get('cost_per_head') is None:
            continue
        # cost_per_head/residual_per_head on a group-wide bucket are now
        # PER-PORTION amounts (2026-08-24: portions = total slot-
        # enrollments that month, not distinct members - see
        # _settlement_rows). expense_shares/expense_residual_shares are
        # this member's own amount, already weighted by how many of the
        # month's slots they're enrolled in - falls back to 0 (not the raw
        # per-portion figure, which would misrepresent a multi-slot
        # member's share) in the unexpected case a member_months entry
        # has no matching share, which shouldn't happen in practice since
        # both are built from the same membership records.
        gw_cost = gw.get('expense_shares', {}).get(pid, 0.0)
        # walkin_shares is this member's slot-weighted cut of walk-in
        # earnings (see _settlement_rows) - summed for their total
        # group-wide refund.
        gw_back = (gw.get('expense_residual_shares', {}).get(pid, 0.0) or 0.0) \
            + (gw.get('walkin_shares', {}).get(pid, 0) or 0.0)
        owe_total += gw_cost
        owed_back_total += gw_back
        lines.append({
            'month': mth, 'year': yr, 'slot': GROUP_SLOT,
            'expected_per_head': gw_cost,
            'you_paid': False,
            'you_owe': round(gw_cost, 2),
            'owed_back_to_you': round(gw_back, 2),
            'collection_status': gw.get('collection_status'),
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
            'extra_collected': 0.0, 'player_count': 0, 'forfeit_count': 0, 'items': []
        })

    # A record with no slot is GROUP-WIDE. Its EXPENSE side (cost_per_head,
    # and the expense-driven half of residual) is split across the DISTINCT
    # "Yes" members across every slot that month, counted once even if a
    # member is in several slots - a group-wide expense (shuttle boxes, a
    # one-off court-wide purchase) is rare and doesn't scale with how many
    # slots you play, so everyone pays/gets refunded the same even share.
    # Its WALK-IN side (extra_collected) is different: walk-ins occupy court
    # time PER SLOT, so a member who plays more slots is more exposed to
    # that - their share of walk-in earnings is weighted by how many of the
    # month's slots they're enrolled in, not split evenly. (Owner-requested
    # 2026-08-20: expense/12 evenly, walk-in/18 by slot-count, in their
    # worked example of 12 unique members across 18 total slot-enrollments.)
    # It lives in its own (month, year, GROUP_SLOT) bucket whose player_count
    # is set to the distinct count below, so cost_per_head divides correctly;
    # residual_per_head after the main loop below is the EXPENSE-ONLY even
    # share, and walkin_shares (added in the post-pass further down) is a
    # per-member dict for the slot-weighted walk-in share - the two are
    # summed by the callers that need a member's total group-wide refund
    # (my_settlement, insights). (_slot_key is the module-level helper -
    # also used by list_records for Stage 4c slot-scoped view access.)
    distinct_members = {}  # (month, year) -> set of idents (Yes, any slot)
    member_slot_counts = {}  # (month, year) -> {ident: # distinct slots Yes this month}

    for e in expenses:
        b = bucket(e.get('month'), e.get('year'), _slot_key(e.get('slot')))
        est = _num(e.get('estimated_cost')) * _num(e.get('estimated_qty'), 1)
        act_cost = e.get('actual_cost')
        act = (_num(act_cost) if act_cost is not None else _num(e.get('estimated_cost'))) \
            * _num(e.get('actual_qty'), _num(e.get('estimated_qty'), 1))
        b['estimated_total'] += est
        b['actual_total'] += act
        b['items'].append({'item': e.get('item'), 'estimated': est, 'actual': act})

    for m in memberships:
        if m.get('status') == 'Yes':
            b = bucket(m.get('month'), m.get('year'), m.get('slot'))
            b['player_count'] += 1
            if m.get('forfeit_residual'):
                b['forfeit_count'] += 1
            ident = m.get('player_id') or f"name:{m.get('display_name')}"
            my = (str(m.get('month')), int(_num(m.get('year'))))
            distinct_members.setdefault(my, set()).add(ident)
            member_slot_counts.setdefault(my, {})
            member_slot_counts[my][ident] = member_slot_counts[my].get(ident, 0) + 1

    for w in walkins:
        date = w.get('date') or ''
        if len(date) >= 7:
            year, month_num = date[:4], date[5:7]
            try:
                month = MONTHS[int(month_num) - 1]
            except (ValueError, IndexError):
                continue
            bucket(month, year, _slot_key(w.get('slot')))['extra_collected'] += _num(w.get('fee'))

    # Give each group-wide bucket its denominator: the month's TOTAL
    # slot-enrollments (not distinct members) - changed 2026-08-24, owner
    # request: "can we switch it back to members in all the slots and then
    # if a group has 2 slots and 3 members share both the groups then they
    # should have 2 portions each ... 2 slots like 12 and 12 each, the
    # total amount for 5 boxes of shuttle amounts to 6000, 1200 per box,
    # then the amount 6000 should be divided into 24 parts ... those who
    # belong to one slot pay for the one slot while others who play in
    # both slots will pay for the 2 portions." This REVERSES the
    # 2026-08-20 decision recorded in the comment above (which deliberately
    # deduped to distinct members for the expense side, unlike walk-ins) -
    # the group-wide expense side now uses the exact same total-slots
    # denominator walk-ins already used, so `cost_per_head`/
    # `residual_per_head` below become PER-PORTION amounts, not per-member
    # ones; `expense_shares`/`expense_residual_shares` (added in the
    # post-pass below) are the actual per-member amounts, weighted by how
    # many of the month's slots that member is enrolled in - mirrors
    # `walkin_shares` exactly, now both driven by the same total_slots.
    for (month, year), slot_counts in member_slot_counts.items():
        gwkey = (str(month), int(year), GROUP_SLOT)
        if gwkey in periods:
            periods[gwkey]['player_count'] = sum(slot_counts.values())
            periods[gwkey]['is_group_wide'] = True
            # Exposed alongside the now-portion-based player_count so the
            # frontend can show "24 portions (15 members)" instead of a bare
            # number that reads like a headcount.
            periods[gwkey]['distinct_member_count'] = len(distinct_members.get((month, year), set()))

    for key, b in periods.items():
        count = b['player_count']
        # Residual (relief) is redistributed among the NON-forfeiters only:
        # a member who forfeits their refund gets 0, and the whole residual
        # pool is split across the remaining Yes members (so each gets more).
        # With no forfeiters this is identical to residual/player_count.
        active = count - b.get('forfeit_count', 0)
        b['active_count'] = active
        b['difference'] = round(b['estimated_total'] - b['actual_total'], 2)
        b['cost_per_head'] = round(b['estimated_total'] / count, 2) if count else None
        b['residual_per_head'] = round(
            (b['estimated_total'] - b['actual_total'] + b['extra_collected']) / active, 2) if active else None
        b['estimated_total'] = round(b['estimated_total'], 2)
        b['actual_total'] = round(b['actual_total'], 2)
        b['extra_collected'] = round(b['extra_collected'], 2)

    # Group-wide post-pass: split the expense-driven and walk-in-driven
    # halves of residual differently (see the big comment above). Overwrites
    # the combined residual_per_head computed above with the EXPENSE-ONLY
    # even share, and adds walkin_shares, a per-member dict, for the
    # slot-weighted walk-in share.
    for key, b in periods.items():
        if key[2] != GROUP_SLOT:
            continue
        month, year = key[0], key[1]
        active = b.get('active_count') or 0
        b['residual_per_head'] = round(
            (b['estimated_total'] - b['actual_total']) / active, 2) if active else None
        slot_counts = member_slot_counts.get((month, year), {})
        total_slots = sum(slot_counts.values())
        walkin_total = b['extra_collected']
        shares = {}
        if walkin_total:
            if total_slots > 0:
                for ident, n_slots in slot_counts.items():
                    shares[ident] = round(walkin_total * n_slots / total_slots, 2)
            elif active:
                # No real per-slot enrollment on record this month (shouldn't
                # normally happen alongside a group-wide walk-in fee, but
                # don't just drop the money) - fall back to an even split
                # across the distinct group-wide members instead.
                even = round(walkin_total / active, 2)
                for ident in distinct_members.get((month, year), set()):
                    shares[ident] = even
        b['walkin_shares'] = shares
        b['walkin_total'] = round(walkin_total, 2)
        b['walkin_denominator'] = total_slots

        # Per-member expense shares, weighted by slot count (2026-08-24) -
        # cost_per_head/residual_per_head above are now PER-PORTION (the
        # bucket's player_count is total_slots, see the denominator pass
        # above), so a member's actual amount is that portion price times
        # how many of the month's slots they're enrolled in. Computed from
        # the raw totals directly (estimated_total * n_slots / total_slots),
        # not from the already-rounded-to-cents cost_per_head times n_slots,
        # to avoid compounding rounding error - mirrors walkin_shares'
        # existing precision exactly (walkin_total * n_slots / total_slots).
        # Falls back to an even split across distinct members only in the
        # degenerate case where there's a group-wide expense but literally
        # no membership records this month (mirrors the walk-in fallback
        # above, for the same reason - don't just drop the money).
        cost_shares, residual_shares = {}, {}
        if total_slots > 0:
            for ident, n_slots in slot_counts.items():
                cost_shares[ident] = round(b['estimated_total'] * n_slots / total_slots, 2)
                residual_shares[ident] = round(
                    (b['estimated_total'] - b['actual_total']) * n_slots / total_slots, 2)
        elif active:
            even_cost = round(b['estimated_total'] / active, 2)
            even_residual = round((b['estimated_total'] - b['actual_total']) / active, 2)
            for ident in distinct_members.get((month, year), set()):
                cost_shares[ident] = even_cost
                residual_shares[ident] = even_residual
        b['expense_shares'] = cost_shares
        b['expense_residual_shares'] = residual_shares

    # Settled status (second pass - needs every period's residual finalised
    # first, because a member's EFFECTIVE amount = cost_per_head - their relief,
    # and relief comes from the previous month's residual). A Yes member counts
    # as confirmed only while their stored confirmed amount still equals their
    # current effective amount.
    matched = {k: 0 for k in periods}
    for m in memberships:
        if m.get('status') != 'Yes' or m.get('payment_confirmed_amount') is None:
            continue
        key = (str(m.get('month')), int(_num(m.get('year'))), str(m.get('slot')))
        b = periods.get(key)
        if not b or b.get('cost_per_head') is None:
            continue
        ident = m.get('player_id') or f"name:{m.get('display_name')}"
        relief = _member_relief(periods, memberships, ident, b['month'], b['year'], slot=b['slot'])
        effective = round(max(b['cost_per_head'] - relief, 0), 2)
        if abs(_num(m.get('payment_confirmed_amount')) - effective) < 0.01:
            matched[key] += 1
    for key, b in periods.items():
        count = b['player_count']
        if count and b.get('cost_per_head') is not None:
            b['confirmed_count'] = matched.get(key, 0)
            b['collection_status'] = 'settled' if b['confirmed_count'] >= count else 'collecting'
        else:
            b['confirmed_count'] = 0
            b['collection_status'] = None

    return periods


def summary(group_id=None, scope_slots=None):
    rows = list(_settlement_rows(group_id).values())
    if scope_slots is not None:
        rows = [r for r in rows if r['slot'] in scope_slots]
    rows.sort(key=lambda r: (r['year'], MONTHS.index(r['month']) if r['month'] in MONTHS else 99, r['slot']))
    resp = {'summary': rows}
    if scope_slots is not None:
        resp['scoped_to'] = sorted(scope_slots)
    return _response(200, resp)


# ---- insights: per-member monthly economics + ghosts + conversion ----

# The site started capturing matches mid-month (first match July 19), so a
# calendar month's real game count is undercounted for anyone who was
# playing before tracking began. Estimation model (from club reality:
# roughly 15 sessions in the 18 untracked days, 4-5 games per session):
AVG_GAMES_PER_SESSION = 4.5
SESSION_RATE = 15.0 / 18.0          # sessions actually held per untracked day
ACTIVE_DAYS_THRESHOLD = 10          # below this, offer the estimated count


# --- Slot-timing / match-density diagnostics (Owner-requested, parked
# 2026-08-19, built 2026-08-20: "also i think you parked something, please
# proceed with that as well"). Both checks are best-effort and NOT
# authoritative - slot labels are free text with no enforced format and
# match records carry no direct slot field, so a match's "slot" has to be
# inferred from which slot(s) its participants are commonly assigned to.
# Anything that can't be cleanly parsed/inferred is SKIPPED rather than
# guessed at, matching the owner's own hedge ("I know this would not be
# valid" in some cases) - silence here means "can't check", never "clean".
SLOT_GRACE_MINUTES = 15             # +/- tolerance around a parsed slot window
ASSUMED_MINUTES_PER_GAME = {21: 15, 11: 8}   # points_to_win -> assumed minutes/game
DEFAULT_ASSUMED_MINUTES_PER_GAME = 12
DENSITY_FLAG_RATIO = 1.3            # flag when assumed playtime exceeds the slot window by >30%

_SLOT_LABEL_RE = re.compile(
    r'^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$',
    re.IGNORECASE)


def _parse_slot_window(label):
    """Best-effort parse of a free-form slot label ('7AM-8AM', '19:00-20:00',
    '7-8AM') into a (start_minute, end_minute) local-minute-of-day window.
    Returns None when the label doesn't match a recognizable shape - slots
    are free text with no enforced format, so failing to parse is the normal,
    safe outcome; callers must treat None as "can't check this slot", never
    as "outside the slot"."""
    if not label:
        return None
    m = _SLOT_LABEL_RE.match(str(label))
    if not m:
        return None
    h1, m1, ap1, h2, m2, ap2 = m.groups()
    h1, h2 = int(h1), int(h2)
    m1, m2 = int(m1) if m1 else 0, int(m2) if m2 else 0
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        return None
    if ap1 is None and ap2 is not None:
        ap1 = ap2
    if ap2 is None and ap1 is not None:
        ap2 = ap1

    def to24(h, ap):
        if ap is None:
            return h if h <= 23 else None
        ap = ap.lower()
        if h == 12:
            h = 0
        return h + 12 if ap == 'pm' else h

    h1c, h2c = to24(h1, ap1), to24(h2, ap2)
    if h1c is None or h2c is None:
        return None
    return (h1c * 60 + m1, h2c * 60 + m2)


def _local_minutes_of_day(iso_ts, offset_minutes):
    """Convert a stored ISO-8601 UTC match timestamp to local minute-of-day
    (0-1439) using the club's configured UTC offset. Returns None on any
    unparseable input - callers must treat that as "can't check this match",
    not as a flag."""
    if not iso_ts:
        return None
    ts = str(iso_ts).strip()
    if ts.endswith('Z'):
        ts = ts[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(timezone.utc) + timedelta(minutes=offset_minutes)
    return local.hour * 60 + local.minute


def _minute_in_window(minute, window, grace_minutes):
    """Whether `minute` (local minute-of-day) falls inside `window` (start,
    end) +/- grace, handling windows/grace that cross midnight. None if
    either input is unavailable."""
    if minute is None or window is None:
        return None
    start, end = window
    lo, hi = (start - grace_minutes) % 1440, (end + grace_minutes) % 1440
    if lo <= hi:
        return lo <= minute <= hi
    return minute >= lo or minute <= hi


def _timing_checks(matches, group, offset_minutes, target_ym=None):
    """Best-effort, non-authoritative diagnostics only (see module note
    above) - a secondary signal for the owner to spot-check, not a source
    of truth.

    mismatches: matches whose local kickoff time falls outside every
        parseable slot window its participants are assigned to (+/-
        SLOT_GRACE_MINUTES). Skipped whenever the match's local time can't
        be derived, the group has no parseable slots, or any participant
        isn't assigned to a slot at all (nothing to compare against).

    density_flags: per (date, inferred slot), the assumed total playing
        time (sum of ASSUMED_MINUTES_PER_GAME per match) vs. the slot's
        parsed duration, flagged when it exceeds DENSITY_FLAG_RATIO. A match
        only contributes when ALL of its participants share exactly one
        common assigned, parseable slot; matches spanning mixed/unassigned
        players are excluded rather than guessed at.
    """
    slots = (group or {}).get('slots') or []
    windows = {s: _parse_slot_window(s) for s in slots}

    mismatches = []
    density_buckets = {}  # (day, slot) -> {'minutes': int, 'games': int}
    for m in matches:
        date = (m.get('date') or '')
        ym, day = date[:7], date[:10]
        if len(ym) != 7 or (target_ym and ym != target_ym):
            continue
        participants = (m.get('team_a') or []) + (m.get('team_b') or [])
        if not participants:
            continue
        per_player_slots = [_member_assigned_slots(pid, group) for pid in participants]

        local_minute = _local_minutes_of_day(m.get('date'), offset_minutes)
        if local_minute is not None and windows and all(per_player_slots):
            union_slots = set().union(*per_player_slots)
            parseable = [windows[s] for s in union_slots if windows.get(s)]
            if parseable and not any(_minute_in_window(local_minute, w, SLOT_GRACE_MINUTES)
                                      for w in parseable):
                mismatches.append({
                    'match_id': m.get('match_id'), 'date': date,
                    'local_time': f"{local_minute // 60:02d}:{local_minute % 60:02d}",
                    'participants': participants,
                    'assigned_slots': sorted(union_slots),
                })

        common = set.intersection(*per_player_slots) if per_player_slots and all(per_player_slots) else set()
        if len(common) == 1:
            slot = next(iter(common))
            if windows.get(slot):
                try:
                    pts = int(m.get('points_to_win'))
                except (TypeError, ValueError):
                    pts = None
                minutes = ASSUMED_MINUTES_PER_GAME.get(pts, DEFAULT_ASSUMED_MINUTES_PER_GAME)
                bucket = density_buckets.setdefault((day, slot), {'minutes': 0, 'games': 0})
                bucket['minutes'] += minutes
                bucket['games'] += 1

    density_flags = []
    for (day, slot), agg in density_buckets.items():
        window = windows.get(slot)
        if not window:
            continue
        start, end = window
        duration = (end - start) if end >= start else (1440 - start + end)
        if duration <= 0:
            continue
        if agg['minutes'] > duration * DENSITY_FLAG_RATIO:
            density_flags.append({
                'date': day, 'slot': slot, 'games': agg['games'],
                'assumed_minutes': agg['minutes'], 'slot_minutes': duration,
                'ratio': round(agg['minutes'] / duration, 2),
            })

    mismatches.sort(key=lambda r: r['date'])
    density_flags.sort(key=lambda r: (-r['ratio'], r['date']))
    return mismatches, density_flags


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
    matches = _scan_all(matches_table)
    settlement = _settlement_rows(group_id)
    group = {}
    if groups_table and group_id:
        group = groups_table.get_item(Key={'group_id': group_id}).get('Item') or {}

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

        # Group-wide (slot-less) cost + relief - weighted by how many of the
        # month's slots this member is enrolled in (2026-08-24; see
        # _settlement_rows). cost_per_head on the group-wide bucket is now a
        # PER-PORTION amount, not a per-member one - expense_shares is this
        # member's own (already-weighted) amount.
        gw = settlement.get((month, year, GROUP_SLOT))
        if gw and gw.get('cost_per_head') is not None:
            gw_share = gw.get('expense_shares', {}).get(ident, 0.0)
            paid += gw_share
            paid_breakdown.append({'slot': GROUP_SLOT, 'per_head': gw['cost_per_head'],
                                    'your_share': gw_share, 'your_slots': len(entry['slots']),
                                    'total': gw.get('estimated_total'), 'members': gw.get('player_count')})

        p_month, p_year = prev_period(month, year)
        relief = _member_relief(settlement, memberships, ident, month, year)
        # Group-wide relief from last month (only if they were a distinct Yes
        # member then, i.e. they have an entry for the previous period).
        # expense_residual_shares is the expense-driven share, already
        # weighted by that prior month's slot count; walkin_shares is this
        # member's slot-weighted cut of walk-in earnings (see
        # _settlement_rows) - both count toward relief.
        p_gw = settlement.get((p_month, p_year, GROUP_SLOT))
        if p_gw and (ident, p_month, p_year) in by_member_month:
            relief += (p_gw.get('expense_residual_shares', {}).get(ident, 0) or 0) \
                + (p_gw.get('walkin_shares', {}).get(ident, 0) or 0)

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
    #
    # Month-wise (Owner-requested 2026-08-20: "can we do it month wise for
    # the calculation of the joined and session paid and other things" -
    # someone who's said they'll settle up at the end of the month reads as
    # a standing debt in a lifetime total, when really it's just this
    # month's not-yet-due balance). When the same month/year filter used
    # for ghosts/cost_rows above is set, sessions/fees_paid/days_attended
    # all scope to just that month; with no filter (the "All" default) this
    # stays the lifetime view it always was, so nothing changes for anyone
    # not using the picker.
    target_ym = f"{f_year:04d}-{MONTHS.index(f_month) + 1:02d}" if (f_month in MONTHS and f_year) else None
    member_pids = {m.get('player_id') for m in memberships if m.get('status') == 'Yes' and m.get('player_id')}
    unlinked_member_names = {m.get('display_name') for m in memberships
                              if m.get('status') == 'Yes' and not m.get('player_id')}
    guests = {}
    scoped_walkins = [w for w in walkins if not target_ym or str(w.get('date') or '')[:7] == target_ym]
    for w in scoped_walkins:
        if _num(w.get('fee')) < 0:
            continue
        gkey = w.get('player_id') or f"name:{w.get('display_name')}"
        g = guests.setdefault(gkey, {'display_name': _resolve_name(cache, w.get('player_id')) or w.get('display_name'),
                                      'sessions': 0, 'fees_paid': 0.0, 'recruit_verdict': None,
                                      'became_member': False})
        # "Sessions paid" must reflect real sessions covered by this fee, not
        # a raw count of fee entries (Owner-reported 2026-08-28: a guest who
        # pays 80/slot every day should show N sessions for N entries, but a
        # guest who pays a lump sum for the whole month in one entry was
        # showing "1 session" no matter how much the lump sum actually
        # covered). sessions_covered defaults to 1 (any missing/blank/zero
        # value) so every existing entry - and every guest who already pays
        # per-session - is completely unaffected by this change.
        sc = _num(w.get('sessions_covered'), 0)
        g['sessions'] += sc if sc > 0 else 1
        g['fees_paid'] = round(g['fees_paid'] + _num(w.get('fee')), 2)
        if w.get('recruit_verdict'):
            g['recruit_verdict'] = w.get('recruit_verdict')
        if w.get('player_id'):
            g['became_member'] = w['player_id'] in member_pids
        else:
            g['became_member'] = w.get('display_name') in unlinked_member_names

    # A player who's played matches but has never had a walk-in fee entry
    # (in the scoped period) AND has never been a Yes member anywhere is a
    # non-member the club is missing money from - and until now they were
    # completely invisible here, since this table only ever looked at
    # `walkins` records (Owner-reported 2026-08-20: played with us, isn't a
    # slot member, doesn't show up in this list at all). Add them with
    # sessions=0/fees_paid=0 so they surface with real "days attended" and,
    # once a default walk-in fee is set, a real "pending" figure - instead
    # of quietly not existing in this view just because no one got around
    # to logging a fee for them.
    played_pids = {pid for (pid, ym) in active_days.keys() if not target_ym or ym == target_ym}
    for pid in played_pids:
        if pid in member_pids or pid in guests:
            continue
        guests[pid] = {'display_name': _resolve_name(cache, pid) or pid,
                        'sessions': 0, 'fees_paid': 0.0, 'recruit_verdict': None,
                        'became_member': False}

    # Attendance vs. fees collected, for non-members (Owner-requested
    # 2026-08-20: "how many days they came and how much they owe, and a
    # difference maintained for them"). `active_days` (built above from the
    # match log, same source cost_rows already uses for members) only ever
    # keys off player_id - a walk-in typed in as a free-text name with no
    # roster player never appears in a match, so their attendance can't be
    # cross-checked this way and days_attended stays None for them (still
    # get sessions/fees_paid from their walk-in records, same as before).
    settings_item = finance_table.get_item(Key={'record_id': 'settings'}).get('Item') or {}
    default_fee = settings_item.get('default_walkin_fee')
    default_fee = float(default_fee) if default_fee is not None else None
    offset_minutes = int(settings_item.get('club_utc_offset_minutes', DEFAULT_CLUB_UTC_OFFSET_MINUTES))
    timing_mismatches, density_flags = _timing_checks(matches, group, offset_minutes, target_ym=target_ym)
    for gkey, g in guests.items():
        # gkey IS the player_id whenever one exists (see how it's built,
        # both from a walk-in record and from the played-but-never-added
        # block above) - only a name-only guest falls back to "name:...".
        pid = None if str(gkey).startswith('name:') else gkey
        days_attended = None
        if pid:
            days_attended = sum(len(days) for (p, ym), days in active_days.items()
                                 if p == pid and (not target_ym or ym == target_ym))
        g['days_attended'] = days_attended
        if days_attended is not None and default_fee is not None:
            expected = round(days_attended * default_fee, 2)
            g['expected_amount'] = expected
            g['pending'] = round(expected - g['fees_paid'], 2)
        else:
            g['expected_amount'] = None
            g['pending'] = None

    guest_rows = sorted(guests.values(), key=lambda g: (-g['became_member'], -g['sessions']))
    conversion = {
        'total_guests': len(guest_rows),
        'became_members': sum(1 for g in guest_rows if g['became_member']),
        'guests': guest_rows,
        'default_walkin_fee': default_fee,
        'scoped_to_month': f"{f_month} {f_year}" if target_ym else None,
    }

    return _response(200, {'ghosts': ghosts, 'noted_attended': noted_attended,
                            'cost_rows': cost_rows, 'conversion': conversion,
                            'tracking_start': tracking_start,
                            'timing_mismatches': timing_mismatches,
                            'density_flags': density_flags})


def _response(status_code, body_dict):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body_dict, default=str)
    }
