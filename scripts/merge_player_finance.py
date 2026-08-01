#!/usr/bin/env python3
"""
Merge a DUPLICATE / old player profile's finance history onto the correct
profile. Excel imports created some profiles (e.g. "Prasanna", id 6481aef2)
that people later re-registered under a fresh profile (e.g. "Prasanna Varade",
id fb796edf). Because the settlement keys on player_id, the old id's residual
(relief) never reaches the new id - the new row shows relief 0. This repoints
the old id's records onto the new id so all their history is one identity.

What it does:
  - Finds every finance record (membership / walk-in / expense) with
    player_id == OLD.
  - For each, if the NEW id has NO record for the same period+slot, repoints it
    (sets player_id = NEW, and display_name = the NEW profile's name).
  - If the NEW id ALREADY has a record for that same period+slot (a genuine
    duplicate, e.g. old August=NA vs new August=Yes), it does NOT repoint -
    it lists it under "collisions" for you to delete manually (usually the
    stale old row).

Period key: membership/expense = (month, year, slot); walk-in = (date, slot).

Safe by design:
  - DRY-RUN by default: prints repoints + collisions, writes nothing.
    Pass --apply to write.
  - IDEMPOTENT: re-running finds nothing left on OLD (no-op).
  - Only ever sets player_id / display_name. Deletes NOTHING (collisions are
    left for you to remove via the app's Delete button).

Usage (from repo root, with AWS creds in the environment):
    python scripts/merge_player_finance.py --old <OLD_ID> --new <NEW_ID>            # dry run
    python scripts/merge_player_finance.py --old <OLD_ID> --new <NEW_ID> --apply    # do it

Example (Prasanna):
    python scripts/merge_player_finance.py \
        --old 6481aef2-74c9-4221-9e1d-943eb1331f9a \
        --new fb796edf-6ffa-470b-a7ea-8d0adcafc61f --apply

Env overrides (default to prod table names):
    FINANCE_TABLE (default networth-finance)
    PLAYERS_TABLE (default networth-players)
    AWS_REGION    (default us-east-1)
"""
import argparse
import os
import sys

import boto3

REGION = os.environ.get('AWS_REGION', 'us-east-1')
FINANCE_TABLE = os.environ.get('FINANCE_TABLE', 'networth-finance')
PLAYERS_TABLE = os.environ.get('PLAYERS_TABLE', 'networth-players')


def scan_all(table):
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get('Items', []))
        if 'LastEvaluatedKey' not in resp:
            return items
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']


def period_key(r):
    if r.get('record_type') == 'walkin':
        return ('walkin', str(r.get('date', '')), str(r.get('slot', '')))
    return (r.get('record_type'), str(r.get('month', '')), str(r.get('year', '')), str(r.get('slot', '')))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--old', required=True, help='old/duplicate player_id to merge FROM')
    ap.add_argument('--new', required=True, help='correct player_id to merge INTO')
    ap.add_argument('--apply', action='store_true', help='actually write (default dry-run)')
    args = ap.parse_args()

    if args.old == args.new:
        print('old and new are the same id.'); sys.exit(1)

    dynamo = boto3.resource('dynamodb', region_name=REGION)
    finance = dynamo.Table(FINANCE_TABLE)
    players = dynamo.Table(PLAYERS_TABLE)

    new_prof = players.get_item(Key={'player_id': args.new}).get('Item')
    if not new_prof:
        print(f"New player {args.new} not found in players table."); sys.exit(1)
    new_name = new_prof.get('name')

    records = scan_all(finance)
    # index existing records already on NEW, by period, to detect collisions
    new_periods = {period_key(r) for r in records if r.get('player_id') == args.new}

    repoint, collide = [], []
    for r in records:
        if r.get('player_id') != args.old:
            continue
        if period_key(r) in new_periods:
            collide.append(r)
        else:
            repoint.append(r)

    print(f"Merging {args.old}  ->  {args.new} ({new_name})")
    print(f"Records on OLD: repoint = {len(repoint)} | collisions (skipped) = {len(collide)}\n")
    for r in repoint:
        when = r.get('date') or f"{r.get('month','?')} {r.get('year','')}"
        print(f"  {'WOULD MOVE' if not args.apply else 'MOVING '} [{r.get('record_type')}] "
              f"{r.get('display_name')} | {when} | {r.get('slot','?')} | status={r.get('status','-')}")
    if collide:
        print("\n== COLLISIONS - NOT moved (NEW already has this period+slot). Delete the stale OLD row via the app: ==")
        for r in collide:
            when = r.get('date') or f"{r.get('month','?')} {r.get('year','')}"
            print(f"  [{r.get('record_type')}] {r.get('display_name')} | {when} | {r.get('slot','?')} "
                  f"| status={r.get('status','-')} | record_id={r.get('record_id')}")

    if not args.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply.")
        return

    for r in repoint:
        finance.update_item(
            Key={'record_id': r['record_id']},
            UpdateExpression='SET player_id = :p, display_name = :n',
            ExpressionAttributeValues={':p': args.new, ':n': new_name},
        )
    print(f"\nDone. Repointed {len(repoint)} record(s) onto {new_name}. "
          f"{len(collide)} collision(s) left for you to delete manually.")


if __name__ == '__main__':
    main()