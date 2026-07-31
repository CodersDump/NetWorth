#!/usr/bin/env python3
"""
Stage 1 of group-scoped finance: migrate the existing club-wide finance
ledger into a dedicated "Club (default)" group.

What it does (ADDITIVE ONLY - never deletes or overwrites data):
  1. Ensures a group named "Club (default)" exists in the groups table,
     creating it if absent (owner = the SuperAdmin player you pass, or none).
  2. Stamps `group_id = <that group's id>` onto every finance record that
     doesn't already have a group_id.

Safe by design:
  - DRY-RUN by default: prints exactly what it would do and changes nothing.
    Pass --apply to actually write.
  - IDEMPOTENT: re-running finds the existing default group and skips records
    already stamped. Running it twice is a no-op.
  - Only ADDS an attribute to finance records; existing fields are untouched.

Usage (from repo root, with AWS creds in the environment):
    python scripts/backfill_finance_groups.py                 # dry run
    python scripts/backfill_finance_groups.py --apply         # do it
    python scripts/backfill_finance_groups.py --apply \
        --owner-player-id <PLAYER_ID>   # make that player the group owner

Env overrides (default to the prod table names):
    FINANCE_TABLE (default networth-finance)
    GROUPS_TABLE  (default networth-groups)
    AWS_REGION    (default us-east-1)
"""
import argparse
import os
import sys
import uuid

import boto3

REGION = os.environ.get('AWS_REGION', 'us-east-1')
FINANCE_TABLE = os.environ.get('FINANCE_TABLE', 'networth-finance')
GROUPS_TABLE = os.environ.get('GROUPS_TABLE', 'networth-groups')
DEFAULT_GROUP_NAME = 'Club (default)'


def find_default_group(groups_table):
    """Return the existing 'Club (default)' group row, or None."""
    for g in scan_all(groups_table):
        if g.get('group_name') == DEFAULT_GROUP_NAME:
            return g
    return None


def scan_all(table):
    """Full paginated scan - DynamoDB caps a single scan page."""
    items = []
    kwargs = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get('Items', []))
        lek = resp.get('LastEvaluatedKey')
        if not lek:
            break
        kwargs['ExclusiveStartKey'] = lek
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='actually write changes (default is a dry run)')
    ap.add_argument('--owner-player-id', default=None,
                    help='player_id to record as owner of the default group')
    args = ap.parse_args()

    mode = 'APPLY' if args.apply else 'DRY-RUN'
    print(f"=== backfill_finance_groups [{mode}] ===")
    print(f"region={REGION} finance_table={FINANCE_TABLE} groups_table={GROUPS_TABLE}")

    ddb = boto3.resource('dynamodb', region_name=REGION)
    finance_table = ddb.Table(FINANCE_TABLE)
    groups_table = ddb.Table(GROUPS_TABLE)

    # 1) Ensure the default group exists ------------------------------------
    group = find_default_group(groups_table)
    if group:
        group_id = group['group_id']
        print(f"[group] found existing '{DEFAULT_GROUP_NAME}' -> {group_id}")
    else:
        group_id = str(uuid.uuid4())
        roles = {}
        member_ids = []
        if args.owner_player_id:
            roles[args.owner_player_id] = 'owner'
            member_ids.append(args.owner_player_id)
        new_group = {
            'group_id': group_id,
            'group_name': DEFAULT_GROUP_NAME,
            'member_ids': member_ids,
            'roles': roles,
        }
        print(f"[group] will CREATE '{DEFAULT_GROUP_NAME}' -> {group_id} "
              f"(owner={args.owner_player_id or 'none'})")
        if args.apply:
            groups_table.put_item(Item=new_group)
            print("[group] created.")

    # 2) Stamp group_id on finance records ----------------------------------
    records = scan_all(finance_table)
    to_stamp = [r for r in records if not r.get('group_id')]
    already = len(records) - len(to_stamp)
    print(f"[finance] {len(records)} records total; {already} already grouped; "
          f"{len(to_stamp)} to stamp with group_id={group_id}")

    stamped = 0
    for r in to_stamp:
        rid = r.get('record_id')
        rtype = r.get('record_type', '?')
        if not rid:
            print(f"  ! skipping a record with no record_id (type={rtype})")
            continue
        if args.apply:
            finance_table.update_item(
                Key={'record_id': rid},
                UpdateExpression='SET group_id = :g',
                ConditionExpression='attribute_not_exists(group_id)',
                ExpressionAttributeValues={':g': group_id},
            )
        stamped += 1

    verb = 'stamped' if args.apply else 'would stamp'
    print(f"[finance] {verb} {stamped} records.")
    if not args.apply:
        print("\nDRY RUN - nothing was written. Re-run with --apply to commit.")
    else:
        print("\nDone. Existing ledger now lives under the 'Club (default)' group.")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001 - surface any AWS/permission error plainly
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
