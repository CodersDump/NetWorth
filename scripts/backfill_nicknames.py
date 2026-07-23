"""
NetWorth - one-time backfill: nickname becomes the unique player
identifier going forward (registration/rename now enforce this), but
every player registered BEFORE that change has no nickname at all.

Rule (exact spec): nickname = name with all whitespace removed.
"Aditya Nair" -> "AdityaNair". If that collides with an existing
nickname, a numeric suffix is appended (AdityaNair2, AdityaNair3, ...)
until it's unique.

Usage:
    python backfill_nicknames.py            (dry run - shows every proposed nickname)
    python backfill_nicknames.py --apply     (writes them)

Idempotent: a player who already has a nickname is left alone and
reported as "already has one, skipping" - safe to re-run any time.
"""
import argparse
import re

import boto3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--region', default='us-east-1')
    args = parser.parse_args()

    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    table = dynamodb.Table('networth-players')

    players = table.scan().get('Items', [])
    existing_nicknames = {p.get('nickname', '').strip().lower() for p in players if p.get('nickname')}

    to_write = []
    for p in players:
        if p.get('nickname'):
            print(f"  {p['name']:<20} already has nickname '{p['nickname']}', skipping")
            continue
        base = re.sub(r'\s+', '', p['name'])
        nickname = base
        n = 2
        while nickname.lower() in existing_nicknames:
            nickname = f"{base}{n}"
            n += 1
        existing_nicknames.add(nickname.lower())  # reserve it against the next player in this same run
        to_write.append((p['player_id'], p['name'], nickname))
        print(f"  {p['name']:<20} -> nickname '{nickname}'")

    if not to_write:
        print("\nNothing to backfill - everyone already has a nickname.")
        return

    if not args.apply:
        print(f"\nDRY RUN - {len(to_write)} player(s) would be updated. Re-run with --apply to write.")
        return

    for player_id, name, nickname in to_write:
        table.update_item(
            Key={'player_id': player_id},
            UpdateExpression='SET nickname = :nk',
            ExpressionAttributeValues={':nk': nickname}
        )
    print(f"\nDone. {len(to_write)} player(s) backfilled.")


if __name__ == '__main__':
    main()
