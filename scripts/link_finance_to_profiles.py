#!/usr/bin/env python3
"""
Link finance records (memberships + walk-ins) to player PROFILES and normalize
their display names to the profile's current name. This matters because
my_settlement matches a member's dues by `player_id` - a record with only a
`display_name` (e.g. old Excel imports) never shows up in that member's
"My dues". It also fixes stale names like "prasanna" -> "Prasanna Varade".

What it does, per record:
  1. If the record ALREADY has a player_id: refresh its display_name to that
     profile's current name if they differ (SAFE - player_id is authoritative).
  2. If the record has NO player_id: try to match its display_name to exactly
     one player (by name or nickname, case-insensitive, spaces ignored).
       - exactly one match  -> set player_id + normalize display_name
       - zero / multiple     -> LEFT ALONE and listed under "needs manual fix"
  Fuzzy guessing is deliberately avoided - money records are only auto-linked
  on a confident single match.

Manual overrides for the ambiguous ones:
    --map "prasanna=<player_id>,sohan=<player_id>"
  forces those display_names (lowercased) to the given player_id.

Safe by design:
  - DRY-RUN by default: prints every proposed change, writes nothing.
    Pass --apply to write.
  - IDEMPOTENT: re-running is a no-op once names/links are correct.
  - Only ever sets player_id and/or display_name; no amounts/status/slots.

Usage (from repo root, with AWS creds in the environment):
    python scripts/link_finance_to_profiles.py                       # dry run
    python scripts/link_finance_to_profiles.py --apply               # do it
    python scripts/link_finance_to_profiles.py --map "prasanna=<PID>" --apply
    python scripts/link_finance_to_profiles.py --types membership --apply

Env overrides (default to prod table names):
    FINANCE_TABLE (default networth-finance)
    PLAYERS_TABLE (default networth-players)
    AWS_REGION    (default us-east-1)
"""
import argparse
import os

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


def norm(s):
    return ''.join(str(s or '').lower().split())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='actually write (default dry-run)')
    ap.add_argument('--types', default='membership,walkin',
                    help='record types to process (default membership,walkin)')
    ap.add_argument('--map', default='', help='manual overrides: "name=player_id,name2=player_id2"')
    args = ap.parse_args()

    types = {t.strip() for t in args.types.split(',') if t.strip()}
    overrides = {}
    for pair in args.map.split(','):
        if '=' in pair:
            k, v = pair.split('=', 1)
            overrides[norm(k)] = v.strip()

    dynamo = boto3.resource('dynamodb', region_name=REGION)
    finance = dynamo.Table(FINANCE_TABLE)
    players_tbl = dynamo.Table(PLAYERS_TABLE)

    players = [p for p in scan_all(players_tbl) if not str(p.get('player_id', '')).startswith('__')]
    by_id = {p['player_id']: p for p in players}
    # normalized name/nickname -> list of player_ids (list, to detect ambiguity)
    name_index = {}
    for p in players:
        for key in {norm(p.get('name')), norm(p.get('nickname'))}:
            if key:
                name_index.setdefault(key, []).append(p['player_id'])

    records = scan_all(finance)
    relink, rename, manual, ok = [], [], [], 0
    for r in records:
        if r.get('record_type') not in types:
            continue
        pid = r.get('player_id')
        dname = r.get('display_name')

        if pid:
            prof = by_id.get(pid)
            if prof and prof.get('name') and prof['name'] != dname:
                rename.append((r, prof['name']))
            else:
                ok += 1
            continue

        # No player_id - try to resolve by name.
        key = norm(dname)
        forced = overrides.get(key)
        if forced and forced in by_id:
            relink.append((r, forced, by_id[forced].get('name')))
            continue
        candidates = name_index.get(key, [])
        if len(candidates) == 1:
            pid2 = candidates[0]
            relink.append((r, pid2, by_id[pid2].get('name')))
        else:
            manual.append((r, len(candidates)))

    print(f"Records scanned (types={sorted(types)}): "
          f"link={len(relink)} | rename-only={len(rename)} | already-fine={ok} | needs-manual={len(manual)}\n")

    if relink:
        print("== will LINK to a profile (+ normalize name) ==")
        for r, pid2, nm in relink:
            print(f"  [{r.get('record_type')}] '{r.get('display_name')}' -> {nm} ({pid2})  record_id={r.get('record_id')}")
    if rename:
        print("\n== already linked, will NORMALIZE name ==")
        for r, nm in rename:
            print(f"  [{r.get('record_type')}] '{r.get('display_name')}' -> '{nm}'  record_id={r.get('record_id')}")
    if manual:
        print("\n== NEEDS MANUAL (no match or ambiguous - not touched) ==")
        seen = {}
        for r, n in manual:
            seen[r.get('display_name')] = seen.get(r.get('display_name'), 0) + 1
        for nm, cnt in sorted(seen.items()):
            print(f"  '{nm}'  ({cnt} record(s))  -> resolve with --map \"{norm(nm)}=<player_id>\"")

    if not args.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply.")
        return

    for r, pid2, nm in relink:
        finance.update_item(
            Key={'record_id': r['record_id']},
            UpdateExpression='SET player_id = :p, display_name = :n',
            ExpressionAttributeValues={':p': pid2, ':n': nm},
        )
    for r, nm in rename:
        finance.update_item(
            Key={'record_id': r['record_id']},
            UpdateExpression='SET display_name = :n',
            ExpressionAttributeValues={':n': nm},
        )
    print(f"\nDone. Linked {len(relink)}, renamed {len(rename)}. "
          f"{len(manual)} still need a --map override.")


if __name__ == '__main__':
    main()