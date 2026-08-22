"""
NetWorth - read-only diagnostic dump for a single tournament.

Prints the full tournament item (pools/squads/reps/group_stage/knockout,
everything) plus every matches-table log entry tagged with that
tournament_id, as one JSON file. Purely read-only - makes no writes of
any kind. Use this to hand a Claude session (or anyone debugging) the raw
data behind a reported bug without needing to screen-share DynamoDB.

Usage:
    pip install boto3 --break-system-packages   (if not already installed)

    # if you don't know the tournament_id, list all tournaments first:
    python dump_tournament_for_review.py --list

    # then dump the one you need:
    python dump_tournament_for_review.py --tournament-id <id> --out dump.json
"""
import argparse
import json
import decimal

import boto3


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, decimal.Decimal):
            return int(o) if o == o.to_integral_value() else float(o)
        return super().default(o)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tournament-id')
    parser.add_argument('--list', action='store_true', help='list every tournament_id + name, then exit')
    parser.add_argument('--tournaments-table', default='networth-tournaments')
    parser.add_argument('--matches-table', default='networth-matches')
    parser.add_argument('--players-table', default='networth-players')
    parser.add_argument('--out', help='write JSON to this file instead of stdout')
    parser.add_argument('--region', default='us-east-1')
    args = parser.parse_args()

    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    tournaments_table = dynamodb.Table(args.tournaments_table)
    matches_table = dynamodb.Table(args.matches_table)
    players_table = dynamodb.Table(args.players_table)

    if args.list:
        items = tournaments_table.scan().get('Items', [])
        for t in sorted(items, key=lambda x: x.get('created_at', '')):
            print(f"{t.get('tournament_id')}  {t.get('created_at', '')[:10]}  {t.get('name')}  (format={t.get('format')}, status={t.get('status')})")
        return

    if not args.tournament_id:
        parser.error('--tournament-id is required unless --list is passed')

    item = tournaments_table.get_item(Key={'tournament_id': args.tournament_id}).get('Item')
    if not item:
        print(f"ERROR: no tournament found with id {args.tournament_id}")
        return

    # every player_id referenced anywhere in the tournament, so their
    # current name/rating can be included for context (names alone are
    # ambiguous - two players can share a display name).
    player_ids = set()

    def collect_ids(node):
        if isinstance(node, dict):
            if 'members' in node and isinstance(node['members'], list):
                player_ids.update(x for x in node['members'] if isinstance(x, str))
            if 'player_id' in node and isinstance(node['player_id'], str):
                player_ids.add(node['player_id'])
            for v in node.values():
                collect_ids(v)
        elif isinstance(node, list):
            for v in node:
                collect_ids(v)

    collect_ids(item)
    players = {}
    for pid in player_ids:
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        if p:
            players[pid] = {'name': p.get('name'), 'nickname': p.get('nickname'), 'rating': p.get('rating')}

    all_matches = matches_table.scan().get('Items', [])
    tournament_matches = [m for m in all_matches if m.get('tournament_id') == args.tournament_id]
    tournament_matches.sort(key=lambda m: m.get('date', ''))

    output = {
        'tournament': item,
        'players_referenced': players,
        'match_log_entries': tournament_matches,
    }

    text = json.dumps(output, indent=2, cls=DecimalEncoder, default=str)
    if args.out:
        with open(args.out, 'w') as f:
            f.write(text)
        print(f"Wrote {len(text)} bytes to {args.out}")
    else:
        print(text)


if __name__ == '__main__':
    main()
