#!/usr/bin/env python3
"""
One-time: re-home all July walk-in finance records under a named group
(default "Matchpoint"). Slots and every other field are left exactly as they
are - this ONLY changes group_id.

What it touches:
  - record_type == 'walkin'
  - the record's date falls in the given month/year (default July 2026),
    read from the `date` field (YYYY-MM-DD) or, as a fallback, month/year
    fields if the walk-in has them.
  - and only records whose group_id isn't already the target group.
Everything else (dates, slots, fees, names) is untouched.

Safe by design:
  - DRY-RUN by default: prints exactly which records it would move and
    changes nothing. Pass --apply to actually write.
  - IDEMPOTENT: a second run skips records already on the target group.
  - Only rewrites group_id; no other attribute is modified.

Usage (from repo root, with AWS creds in the environment):
    python scripts/map_july_walkins_to_group.py                    # dry run
    python scripts/map_july_walkins_to_group.py --apply            # do it
    python scripts/map_july_walkins_to_group.py --group Matchpoint --month 7 --year 2026 --apply
    python scripts/map_july_walkins_to_group.py --group-id <GID> --apply   # target by id

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
    """True if this walk-in falls in the target month/year. Prefers the
    `date` field (YYYY-MM-DD); falls back to month/year attributes."""
    d = str(record.get('date', '')).strip()
    if len(d) >= 7 and d[4] == '-':
        try:
            y, m = int(d[0:4]), int(d[5:7])
            return y == year and m == month
        except ValueError:
            pass
    # fallback: named/numeric month + year attributes
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
    args = ap.parse_args()

    dynamo = boto3.resource('dynamodb', region_name=REGION)
    finance = dynamo.Table(FINANCE_TABLE)
    groups = dynamo.Table(GROUPS_TABLE)

    target_gid = resolve_group_id(groups, args.group, args.group_id)
    if not target_gid:
        print(f"Could not find a group named '{args.group}'. "
              f"Pass --group-id, or check the name.")
        sys.exit(1)

    records = scan_all(finance)
    matches, already = [], 0
    for r in records:
        if r.get('record_type') != 'walkin':
            continue
        if not in_month(r, args.month, args.year):
            continue
        if r.get('group_id') == target_gid:
            already += 1
            continue
        matches.append(r)

    label = f"{MONTH_NAMES[args.month].title()} {args.year}"
    print(f"Target group: {args.group} ({target_gid})")
    print(f"Walk-ins in {label}: to move = {len(matches)} | already on target = {already}\n")
    for r in matches:
        who = r.get('display_name') or r.get('player_id') or '?'
        print(f"  {'WOULD MOVE' if not args.apply else 'MOVING '} {who} | {r.get('date','?')} "
              f"| {r.get('slot','?')} | fee={r.get('fee','?')} | from group={r.get('group_id')}")

    if not args.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply to move these.")
        return

    for r in matches:
        finance.update_item(
            Key={'record_id': r['record_id']},
            UpdateExpression='SET group_id = :g',
            ExpressionAttributeValues={':g': target_gid},
        )
    print(f"\nDone. Moved {len(matches)} {label} walk-in(s) to {args.group}.")


if __name__ == '__main__':
    main()
