#!/usr/bin/env python3
"""
One-time: move ALL of a month's finance records - expenses, memberships AND
walk-ins - under a named group (default "Matchpoint", July 2026). This is what
makes the residual/settlement math work for that group: the per-head relief
needs the expenses (cost), the Yes-memberships (the denominator/count) and the
walk-in fees (extra collected) to ALL live under the same group.

Matches the right date field per record type:
  - expense / membership : `month` (name, e.g. "July") + `year`
  - walkin               : `date` (YYYY-MM-DD)

Only rewrites group_id; slots, amounts, statuses, names - everything else is
left exactly as-is.

Safe by design:
  - DRY-RUN by default: prints exactly what it would move, changes nothing.
    Pass --apply to actually write.
  - IDEMPOTENT: skips records already on the target group. Safe to re-run
    after the earlier walk-ins-only move (those 33 are skipped).

Usage (from repo root, with AWS creds in the environment):
    python scripts/map_month_finance_to_group.py                    # dry run
    python scripts/map_month_finance_to_group.py --apply            # do it
    python scripts/map_month_finance_to_group.py --types membership,expense --apply
    python scripts/map_month_finance_to_group.py --group Matchpoint --month 7 --year 2026 --apply
    python scripts/map_month_finance_to_group.py --group-id <GID> --apply

Env overrides (default to the prod table names):
    FINANCE_TABLE (default networth-finance)
    GROUPS_TABLE  (default networth-groups)
    AWS_REGION    (default us-east-1)
"""
import argparse
import os
import sys

import boto3

REGION = os.environ.get('AWS_REGION', 'us-east-1')
FINANCE_TABLE = os.environ.get('FINANCE_TABLE', 'networth-finance')
GROUPS_TABLE = os.environ.get('GROUPS_TABLE', 'networth-groups')

MONTH_NAMES = {
    1: 'january', 2: 'february', 3: 'march', 4: 'april', 5: 'may', 6: 'june',
    7: 'july', 8: 'august', 9: 'september', 10: 'october', 11: 'november', 12: 'december',
}


def scan_all(table):
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get('Items', []))
        if 'LastEvaluatedKey' not in resp:
            return items
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']


def resolve_group_id(groups_table, name, gid):
    if gid:
        return gid
    for g in scan_all(groups_table):
        if str(g.get('group_name', '')).strip().lower() == name.strip().lower():
            return g['group_id']
    return None


def in_month(record, month, year):
    """True if this record falls in the target month/year, reading whichever
    date fields its record type uses."""
    rtype = record.get('record_type')
    if rtype == 'walkin':
        d = str(record.get('date', '')).strip()
        if len(d) >= 7 and d[4] == '-':
            try:
                return int(d[0:4]) == year and int(d[5:7]) == month
            except ValueError:
                return False
        return False
    # expense / membership: named month + year
    rm = str(record.get('month', '')).strip().lower()
    ry = str(record.get('year', '')).strip()
    return rm == MONTH_NAMES[month] and ry == str(year)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='actually write (default is dry-run)')
    ap.add_argument('--group', default='Matchpoint', help="target group NAME (default 'Matchpoint')")
    ap.add_argument('--group-id', default=None, help='target group by id (overrides --group)')
    ap.add_argument('--month', type=int, default=7, help='month number 1-12 (default 7 = July)')
    ap.add_argument('--year', type=int, default=2026, help='year (default 2026)')
    ap.add_argument('--types', default='expense,membership,walkin',
                    help='comma-separated record types to move (default all three)')
    args = ap.parse_args()

    types = {t.strip() for t in args.types.split(',') if t.strip()}

    dynamo = boto3.resource('dynamodb', region_name=REGION)
    finance = dynamo.Table(FINANCE_TABLE)
    groups = dynamo.Table(GROUPS_TABLE)

    target_gid = resolve_group_id(groups, args.group, args.group_id)
    if not target_gid:
        print(f"Could not find a group named '{args.group}'. Pass --group-id, or check the name.")
        sys.exit(1)

    records = scan_all(finance)
    to_move, already = [], 0
    by_type = {t: 0 for t in types}
    for r in records:
        if r.get('record_type') not in types:
            continue
        if not in_month(r, args.month, args.year):
            continue
        if r.get('group_id') == target_gid:
            already += 1
            continue
        to_move.append(r)
        by_type[r['record_type']] = by_type.get(r['record_type'], 0) + 1

    label = f"{MONTH_NAMES[args.month].title()} {args.year}"
    print(f"Target group: {args.group} ({target_gid})")
    print(f"{label} records to move = {len(to_move)} "
          f"({', '.join(f'{k}: {v}' for k, v in by_type.items())}) | already on target = {already}\n")
    for r in to_move:
        who = r.get('display_name') or r.get('item') or r.get('player_id') or '?'
        when = r.get('date') or f"{r.get('month','?')} {r.get('year','')}"
        print(f"  {'WOULD MOVE' if not args.apply else 'MOVING '} [{r.get('record_type')}] {who} "
              f"| {when} | {r.get('slot','?')} | from group={r.get('group_id')}")

    if not args.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply to move these.")
        return

    for r in to_move:
        finance.update_item(
            Key={'record_id': r['record_id']},
            UpdateExpression='SET group_id = :g',
            ExpressionAttributeValues={':g': target_gid},
        )
    print(f"\nDone. Moved {len(to_move)} {label} record(s) to {args.group}.")


if __name__ == '__main__':
    main()