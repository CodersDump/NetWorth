"""
NetWorth - one-time backfill: correct a player's name in historical
match and tournament records after renaming them in the Players table.

Why this is needed: match/tournament records store the player's name as a
frozen snapshot at the time they were saved (team_a_names, team_b_names,
and nested entity "name" fields in tournament brackets). Renaming a player
via PUT /players/{id} only updates the live Players table - it doesn't
retroactively rewrite that already-saved text. Run this script once after
a rename to fix up existing history.

Usage:
    pip install boto3 --break-system-packages   (if not already installed)
    python rename_player_history.py \
        --player-id 572c87b5-bdae-4ee6-91ca-0b1568579fd2 \
        --old-name Prasanna \
        --new-name Sambhit \
        --matches-table networth-matches \
        --tournaments-table networth-tournaments \
        --region us-east-1

Safe to re-run - it only changes entries that still contain the old name
for the given player_id, so running it twice is a harmless no-op the
second time.
"""
import argparse
import boto3


def fix_matches(table, player_id, old_name, new_name):
    items = table.scan().get('Items', [])
    updated = 0
    for item in items:
        changed = False
        team_a = item.get('team_a', [])
        team_b = item.get('team_b', [])
        team_a_names = list(item.get('team_a_names', []))
        team_b_names = list(item.get('team_b_names', []))

        if player_id in team_a:
            idx = team_a.index(player_id)
            if idx < len(team_a_names) and team_a_names[idx] == old_name:
                team_a_names[idx] = new_name
                changed = True
        if player_id in team_b:
            idx = team_b.index(player_id)
            if idx < len(team_b_names) and team_b_names[idx] == old_name:
                team_b_names[idx] = new_name
                changed = True

        if changed:
            table.update_item(
                Key={'match_id': item['match_id']},
                UpdateExpression='SET team_a_names = :a, team_b_names = :b',
                ExpressionAttributeValues={':a': team_a_names, ':b': team_b_names}
            )
            updated += 1
    print(f'Matches table: updated {updated} record(s)')


def _fix_entity(entity, player_id, old_name, new_name):
    """Fix a bracket 'entity' dict (either a lone player or a doubles team)."""
    if not entity:
        return False
    changed = False
    if entity.get('player_id') == player_id and entity.get('name') == old_name:
        entity['name'] = new_name
        changed = True
    elif player_id in (entity.get('members') or []) and old_name in (entity.get('name') or ''):
        entity['name'] = entity['name'].replace(old_name, new_name)
        changed = True
    return changed


def fix_tournaments(table, player_id, old_name, new_name):
    items = table.scan().get('Items', [])
    updated = 0
    for item in items:
        changed = False

        for sg in item.get('subgroups', {}).values():
            for member in sg.get('members', []):
                if _fix_entity(member, player_id, old_name, new_name):
                    changed = True
            for fixture in sg.get('fixtures', []):
                for side in ('player_a', 'player_b'):
                    if _fix_entity(fixture.get(side), player_id, old_name, new_name):
                        changed = True

        for round_ in item.get('knockout', {}).get('rounds', []):
            for match in round_:
                for side in ('player_a', 'player_b'):
                    if _fix_entity(match.get(side), player_id, old_name, new_name):
                        changed = True

        if changed:
            table.put_item(Item=item)
            updated += 1
    print(f'Tournaments table: updated {updated} record(s)')


def main():
    parser = argparse.ArgumentParser(description='Backfill a player name change into match/tournament history')
    parser.add_argument('--player-id', required=True)
    parser.add_argument('--old-name', required=True)
    parser.add_argument('--new-name', required=True)
    parser.add_argument('--matches-table', default='networth-matches')
    parser.add_argument('--tournaments-table', default='networth-tournaments')
    parser.add_argument('--region', default='us-east-1')
    args = parser.parse_args()

    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    matches_table = dynamodb.Table(args.matches_table)
    tournaments_table = dynamodb.Table(args.tournaments_table)

    fix_matches(matches_table, args.player_id, args.old_name, args.new_name)
    fix_tournaments(tournaments_table, args.player_id, args.old_name, args.new_name)
    print('Done.')


if __name__ == '__main__':
    main()
