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

# Private - never rendered in the UI. Change both if ever exposed.
VIEW_KEY = 'Matchpoint-Ledger-11'          # read/write access to finance data
CONFIRMATION_CODE = 'Matchpoint-Falcon-77'  # required on top of VIEW_KEY for deletes

MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
          'August', 'September', 'October', 'November', 'December']


def handler(event, context):
    try:
        method = event.get('httpMethod')
        proxy = (event.get('pathParameters') or {}).get('proxy', '')
        parts = [p for p in proxy.split('/') if p] if proxy else []
        params = event.get('queryStringParameters') or {}
        body = json.loads(event.get('body') or '{}')

        # The one public route: names/dates only, and only when enabled.
        if parts == ['walkins', 'public'] and method == 'GET':
            return public_walkins()

        supplied_key = params.get('view_key') or body.get('view_key')
        if supplied_key != VIEW_KEY:
            return _response(403, {'error': 'view key is missing or incorrect'})

        if parts == ['summary'] and method == 'GET':
            return summary()
        if parts == ['settings']:
            if method == 'GET':
                return get_settings()
            if method == 'PUT':
                return put_settings(body)

        for kind in ('expenses', 'memberships', 'walkins'):
            rtype = kind[:-1] if kind != 'memberships' else 'membership'
            if parts == [kind]:
                if method == 'GET':
                    return list_records(rtype, params)
                if method == 'POST':
                    return create_records(rtype, body)
            if len(parts) == 2 and parts[0] == kind:
                if method == 'PUT':
                    return update_record(rtype, parts[1], body)
                if method == 'DELETE':
                    return delete_record(rtype, parts[1], body)

        return _response(404, {'error': 'not found'})
    except Exception as e:
        return _response(500, {'error': str(e)})


# ---------- helpers ----------

def _scan_type(record_type):
    items = finance_table.scan().get('Items', [])
    return [i for i in items if i.get('record_type') == record_type]


def _num(v, default=0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


ALLOWED_FIELDS = {
    'expense': ['month', 'year', 'slot', 'item', 'estimated_cost', 'actual_cost',
                'estimated_qty', 'actual_qty'],
    'membership': ['month', 'year', 'slot', 'display_name', 'player_id', 'status', 'remark'],
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

def list_records(record_type, params):
    items = _scan_type(record_type)
    for f in ('month', 'year', 'slot'):
        if params.get(f):
            items = [i for i in items if str(i.get(f)) == str(params[f])]
    # Live player names for linked records - a rename shows up immediately.
    cache = {}
    for i in items:
        live = _resolve_name(cache, i.get('player_id'))
        if live:
            i['display_name'] = live
    key = (lambda i: (i.get('date', ''), i.get('display_name', ''))) if record_type == 'walkin' \
        else (lambda i: (int(_num(i.get('year'))), MONTHS.index(i['month']) if i.get('month') in MONTHS else 99,
                          i.get('slot', ''), i.get('display_name', i.get('item', ''))))
    items.sort(key=key)
    return _response(200, {record_type + 's': items})


def create_records(record_type, body):
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
        finance_table.put_item(Item=item)
        created.append(item['record_id'])
    result = {'created': created}
    if errors:
        result['errors'] = errors
    return _response(200, result)


def update_record(record_type, record_id, body):
    existing = finance_table.get_item(Key={'record_id': record_id}).get('Item')
    if not existing or existing.get('record_type') != record_type:
        return _response(404, {'error': f'{record_type} not found'})
    updates = _clean(record_type, body)
    # Explicit unlink: player_id: null in the body clears the link.
    if 'player_id' in body and body['player_id'] in (None, ''):
        existing.pop('player_id', None)
    if not updates and 'player_id' not in body:
        return _response(400, {'error': 'no updatable fields supplied'})
    existing.update(updates)
    finance_table.put_item(Item=existing)
    return _response(200, {'updated': record_id})


def delete_record(record_type, record_id, body):
    if body.get('confirm') != CONFIRMATION_CODE:
        return _response(400, {'error': 'confirmation code is missing or incorrect'})
    existing = finance_table.get_item(Key={'record_id': record_id}).get('Item')
    if not existing or existing.get('record_type') != record_type:
        return _response(404, {'error': f'{record_type} not found'})
    finance_table.delete_item(Key={'record_id': record_id})
    return _response(200, {'deleted': record_id})


# ---------- settings + public walk-ins ----------

def get_settings():
    item = finance_table.get_item(Key={'record_id': 'settings'}).get('Item') or {}
    return _response(200, {'walkins_public': bool(item.get('walkins_public', False))})


def put_settings(body):
    item = {'record_id': 'settings', 'record_type': 'settings',
            'walkins_public': bool(body.get('walkins_public', False))}
    finance_table.put_item(Item=item)
    return _response(200, {'walkins_public': item['walkins_public']})


def public_walkins():
    settings = finance_table.get_item(Key={'record_id': 'settings'}).get('Item') or {}
    if not settings.get('walkins_public'):
        return _response(404, {'error': 'not available'})
    items = _scan_type('walkin')
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

def summary():
    """Per (month, year, slot): the exact math from the Calculations sheet.
        estimated_total = SUM(estimated_cost * estimated_qty)
        actual_total    = SUM(actual_cost * actual_qty)   [falls back to estimated]
        extra_collected = SUM(walk-in fees for that month+slot)
        player_count    = COUNT(memberships with status Yes)
        cost_per_head   = estimated_total / player_count   [what members paid]
        residual_per_head = (estimated_total - actual_total + extra_collected)
                             / player_count                [refund owed each]
    """
    expenses = _scan_type('expense')
    memberships = _scan_type('membership')
    walkins = _scan_type('walkin')

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

    rows = []
    for b in periods.values():
        count = b['player_count']
        b['difference'] = round(b['estimated_total'] - b['actual_total'], 2)
        b['cost_per_head'] = round(b['estimated_total'] / count, 2) if count else None
        b['residual_per_head'] = round(
            (b['estimated_total'] - b['actual_total'] + b['extra_collected']) / count, 2) if count else None
        b['estimated_total'] = round(b['estimated_total'], 2)
        b['actual_total'] = round(b['actual_total'], 2)
        b['extra_collected'] = round(b['extra_collected'], 2)
        rows.append(b)

    rows.sort(key=lambda r: (r['year'], MONTHS.index(r['month']) if r['month'] in MONTHS else 99, r['slot']))
    return _response(200, {'summary': rows})


def _response(status_code, body_dict):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body_dict, default=str)
    }
