#!/usr/bin/env python3
"""
One-time: reset paid memberships back to UNPAID for a given month (default
August 2026), because you're mid-collection and want to re-collect cleanly.

A membership is "paid" when its record carries a `payment_confirmed_amount`
attribute (that's exactly what the in-app "confirm payment" toggle sets). This
script REMOVES that attribute from matching membership records, which returns
them to unpaid - it does NOT delete the membership or change status/roster.

What it touches:
  - record_type == 'membership'
  - month == <month>  (case-insensitive, default 'August')
  - year  == <year>   (default 2026)
  - and only rows that currently HAVE payment_confirmed_amount.
Everything else is left untouched.

Safe by design:
  - DRY-RUN by default: prints exactly which records it would reset, changes
    nothing. Pass --apply to actually write.
  - IDEMPOTENT: a second run finds nothing left to reset (no-op).
  - Only REMOVES the payment attribute; no other field is modified.

Usage (from repo root, with AWS creds in the environment):
    python scripts/reset_august_unpaid.py                    # dry run (Aug 2026)
    python scripts/reset_august_unpaid.py --apply            # do it
    python scripts/reset_august_unpaid.py --month July --year 2026 --apply
    python scripts/reset_august_unpaid.py --group-id <GID> --apply   # limit to one group

Env overrides (default to the prod table name):
    FINANCE_TABLE (default networth-finance)
    AWS_REGION    (default us-east-1)
"""
import argparse
import os

import boto3


REGION = os.environ.get('AWS_REGION', 'us-east-1')
FINANCE_TABLE = os.environ.get('FINANCE_TABLE', 'networth-finance')


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
    ap.add_argument('--month', default='August', help="month name to reset (default 'August')")
    ap.add_argument('--year', default='2026', help="year to reset (default '2026')")
    ap.add_argument('--group-id', default=None, help='optional: limit to a single group_id')
    args = ap.parse_args()

    month = args.month.strip().lower()
    year = str(args.year).strip()

    finance = boto3.resource('dynamodb', region_name=REGION).Table(FINANCE_TABLE)
    records = scan_all(finance)

    matches = []
    for r in records:
        if r.get('record_type') != 'membership':
            continue
        if str(r.get('month', '')).strip().lower() != month:
            continue
        if str(r.get('year', '')).strip() != year:
            continue
        if args.group_id and r.get('group_id') != args.group_id:
            continue
        if r.get('payment_confirmed_amount') is None:
            continue  # already unpaid
        matches.append(r)

    scope = f"{args.month} {year}" + (f" (group {args.group_id})" if args.group_id else "")
    print(f"Membership records currently marked PAID for {scope}: {len(matches)}\n")
    for r in matches:
        who = r.get('display_name') or r.get('player_id') or '?'
        print(f"  {'WOULD RESET' if not args.apply else 'RESETTING '} {who} | {r.get('slot','?')} "
              f"| paid={r.get('payment_confirmed_amount')} | record_id={r.get('record_id')}")

    if not args.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply to reset these to unpaid.")
        return

    for r in matches:
        finance.update_item(
            Key={'record_id': r['record_id']},
            UpdateExpression='REMOVE payment_confirmed_amount',
        )
    print(f"\nDone. Reset {len(matches)} membership record(s) to unpaid for {scope}.")


if __name__ == '__main__':
    main()
