#!/usr/bin/env python3
"""
One-time backfill: give every group the default time slots so the Finance
tab's slot dropdowns (which read from the group) have something to show.

What it does (ADDITIVE ONLY - never deletes or overwrites):
  - For every group WITHOUT a non-empty `slots` list, sets
    slots = ["7AM-8AM", "8AM-9AM"] (the historical club defaults).
  - Groups that already have slots (including any you added by hand) are
    left completely untouched.

Safe by design:
  - DRY-RUN by default: prints what it would do and changes nothing.
    Pass --apply to actually write.
  - IDEMPOTENT: re-running skips groups that already have slots. Running it
    twice is a no-op.

Usage (from repo root, with AWS creds in the environment):
    python scripts/backfill_group_slots.py                 # dry run
    python scripts/backfill_group_slots.py --apply         # do it
    python scripts/backfill_group_slots.py --apply \
        --slots "7AM-8AM,8AM-9AM"    # custom default list

Env overrides (default to the prod table name):
    GROUPS_TABLE  (default networth-groups)
    AWS_REGION    (default us-east-1)
"""
import argparse
import os
import sys

import boto3

REGION = os.environ.get('AWS_REGION', 'us-east-1')
GROUPS_TABLE = os.environ.get('GROUPS_TABLE', 'networth-groups')
DEFAULT_SLOTS = ['7AM-8AM', '8AM-9AM']


def scan_all(table):
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get('Items', []))
        if 'LastEvaluatedKey' not in resp:
            return items
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='actually write (default is dry-run)')
    ap.add_argument('--slots', default=','.join(DEFAULT_SLOTS),
                    help='comma-separated default slot list to set on groups that have none')
    args = ap.parse_args()

    slots = [s.strip() for s in args.slots.split(',') if s.strip()]
    if not slots:
        print('No slots given.'); sys.exit(1)

    groups_table = boto3.resource('dynamodb', region_name=REGION).Table(GROUPS_TABLE)
    groups = scan_all(groups_table)

    to_set, skipped = [], []
    for g in groups:
        if g.get('slots'):
            skipped.append(g)
        else:
            to_set.append(g)

    print(f"Groups: {len(groups)} | already have slots: {len(skipped)} | to set: {len(to_set)}")
    print(f"Default slots to apply: {slots}\n")
    for g in to_set:
        print(f"  {'WOULD SET' if not args.apply else 'SETTING '} {g.get('group_name','?')} ({g['group_id']}) -> {slots}")

    if not args.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply to make these changes.")
        return

    for g in to_set:
        groups_table.update_item(
            Key={'group_id': g['group_id']},
            UpdateExpression='SET slots = :s',
            ExpressionAttributeValues={':s': slots},
        )
    print(f"\nDone. Set default slots on {len(to_set)} group(s).")


if __name__ == '__main__':
    main()
