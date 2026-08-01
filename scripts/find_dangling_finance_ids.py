#!/usr/bin/env python3
"""
READ-ONLY diagnostic. Finds finance records whose player_id points at a player
profile that NO LONGER EXISTS (deleted/orphaned) - the exact situation that
stranded Prasanna's history on an old id. Lists each dangling id with its
display_name(s) and how many records reference it, so you can merge them onto
the correct profile with merge_player_finance.py.

Writes NOTHING. Pure report.

For each dangling id it also SUGGESTS a likely correct profile by matching the
record's display_name against existing players (name/nickname, spaces/case
ignored). A suggestion is a hint only - always eyeball before merging.

Usage (from repo root, with AWS creds in the environment):
    python scripts/find_dangling_finance_ids.py

Env overrides (default to prod table names):
    FINANCE_TABLE (default networth-finance)
    PLAYERS_TABLE (default networth-players)
    AWS_REGION    (default us-east-1)
"""
import os
from collections import defaultdict

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
    dynamo = boto3.resource('dynamodb', region_name=REGION)
    finance = dynamo.Table(FINANCE_TABLE)
    players = dynamo.Table(PLAYERS_TABLE)

    profs = [p for p in scan_all(players) if not str(p.get('player_id', '')).startswith('__')]
    valid_ids = {p['player_id'] for p in profs}
    name_index = defaultdict(list)
    for p in profs:
        for key in {norm(p.get('name')), norm(p.get('nickname'))}:
            if key:
                name_index[key].append((p['player_id'], p.get('name')))

    records = scan_all(finance)
    # dangling id -> {names:set, count:int, types:Counter-ish}
    dangling = defaultdict(lambda: {'names': set(), 'count': 0, 'types': defaultdict(int)})
    for r in records:
        pid = r.get('player_id')
        if not pid or pid in valid_ids:
            continue
        d = dangling[pid]
        d['names'].add(r.get('display_name'))
        d['count'] += 1
        d['types'][r.get('record_type')] += 1

    if not dangling:
        print("No dangling finance identities. Every finance record points at a real profile.")
        return

    print(f"Found {len(dangling)} dangling player_id(s) on finance records "
          f"(profile deleted/missing):\n")
    for pid, d in sorted(dangling.items(), key=lambda kv: -kv[1]['count']):
        names = ', '.join(sorted(n for n in d['names'] if n))
        types = ', '.join(f"{k}:{v}" for k, v in d['types'].items())
        print(f"OLD id {pid}")
        print(f"   name(s): {names}")
        print(f"   records: {d['count']} ({types})")
        # suggest a current profile by name
        suggestions = set()
        for nm in d['names']:
            for (cand_id, cand_name) in name_index.get(norm(nm), []):
                suggestions.add((cand_id, cand_name))
        if len(suggestions) == 1:
            cid, cname = next(iter(suggestions))
            print(f"   -> likely correct profile: {cname} ({cid})")
            print(f"      merge: python scripts/merge_player_finance.py --old {pid} --new {cid}")
        elif len(suggestions) > 1:
            print(f"   -> multiple name matches, pick one:")
            for cid, cname in sorted(suggestions, key=lambda x: x[1] or ''):
                print(f"        {cname} ({cid})")
        else:
            print(f"   -> no name match found; find the correct player_id manually.")
        print()


if __name__ == '__main__':
    main()