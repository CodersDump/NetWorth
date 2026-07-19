"""
NetWorth - one-time fix: add the missing 3rd place match to a tournament
whose semis were pre-loaded as history (via reconstruct_july19_tournament.py)
rather than scored live through the API - meaning the auto-generation logic
in record_knockout_score never got a chance to run for it.

Usage:
    pip install boto3 --break-system-packages   (if not already installed)
    python add_third_place_match.py --tournament-id 8003a10d-6bc6-4b4d-b8c6-fd1cdd2bf87c
"""
import argparse
import uuid

import boto3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tournament-id', required=True)
    parser.add_argument('--tournaments-table', default='networth-tournaments')
    parser.add_argument('--region', default='us-east-1')
    args = parser.parse_args()

    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    table = dynamodb.Table(args.tournaments_table)

    item = table.get_item(Key={'tournament_id': args.tournament_id}).get('Item')
    if not item:
        print(f"ERROR: no tournament found with id {args.tournament_id}")
        return

    rounds = item.get('knockout', {}).get('rounds', [])
    if len(rounds) < 1:
        print("ERROR: this tournament has no knockout rounds yet.")
        return

    semis = rounds[0]
    if len(semis) != 2:
        print(f"ERROR: expected exactly 2 semifinal matches, found {len(semis)}. Not auto-fixing - check manually.")
        return
    if not all(m['played'] for m in semis):
        print("ERROR: semis are not both marked as played yet.")
        return
    if 'third_place_match' in item.get('knockout', {}):
        print("Nothing to do - third_place_match already exists on this tournament.")
        return

    losers = []
    for m in semis:
        winner_id = m['winner_id']
        loser_entity = m['player_b'] if m['player_a']['player_id'] == winner_id else m['player_a']
        losers.append(loser_entity)

    item['knockout']['third_place_match'] = {
        'match_id': str(uuid.uuid4()),
        'player_a': losers[0],
        'player_b': losers[1],
        'games': [],
        'games_won_a': 0,
        'games_won_b': 0,
        'played': False,
        'winner_id': None
    }

    table.put_item(Item=item)
    print(f"Added 3rd place match: {losers[0]['name']} vs {losers[1]['name']}")


if __name__ == '__main__':
    main()
