"""
NetWorth - Epic 3: backfill a `roles` map onto every existing group.

Your groups predate the role system, so there's no reliably-stored
"who created this group" field - member_ids is just a flat list. This
script's default guess is the FIRST entry in that list (which, for groups
that haven't had removals, usually reflects original add order in
DynamoDB) - but it never writes that guess without you reviewing it first,
and you can override any group's owner via a small CSV.

Usage:
    # 1. See the proposed owner for every group (writes nothing)
    python backfill_group_roles.py

    # 2. If any guess is wrong, write scripts/group_owner_overrides.csv:
    #      group_name,owner_player_name
    #      Matchpoint,Sourabh C
    #    and re-run the dry run - it'll show the override applied instead

    # 3. Once every proposed owner looks right:
    python backfill_group_roles.py --apply

Idempotent: a group that already has a non-empty `roles` map is left
alone (printed as "already has roles, skipping") - safe to re-run any time
without clobbering roles you've since changed by hand via the API.
"""
import argparse
import csv
import os

import boto3

OVERRIDES_PATH = os.path.join(os.path.dirname(__file__), 'group_owner_overrides.csv')


def load_overrides():
    if not os.path.exists(OVERRIDES_PATH):
        return {}
    overrides = {}
    with open(OVERRIDES_PATH, newline='') as f:
        for row in csv.DictReader(f):
            name, owner = row.get('group_name', '').strip(), row.get('owner_player_name', '').strip()
            if name and owner:
                overrides[name.lower()] = owner
    return overrides


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='actually write roles (default: dry run)')
    parser.add_argument('--region', default='us-east-1')
    args = parser.parse_args()

    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    groups_table = dynamodb.Table('networth-groups')
    players_table = dynamodb.Table('networth-players')

    players = players_table.scan().get('Items', [])
    name_to_pid = {p['name'].strip().lower(): p['player_id'] for p in players}
    pid_to_name = {p['player_id']: p['name'] for p in players}

    overrides = load_overrides()
    groups = groups_table.scan().get('Items', [])

    to_write = []
    for g in groups:
        group_id, group_name = g['group_id'], g.get('group_name', '(unnamed)')
        if g.get('roles'):
            print(f"  {group_name:<20} already has roles, skipping")
            continue

        member_ids = g.get('member_ids', [])
        override_name = overrides.get(group_name.strip().lower())
        owner_id = None

        if override_name:
            owner_id = name_to_pid.get(override_name.strip().lower())
            if not owner_id:
                print(f"  {group_name:<20} override '{override_name}' not found in PlayersTable - skipping this group")
                continue
            source = f"override -> {override_name}"
        elif member_ids:
            owner_id = member_ids[0]
            source = f"guessed (first member) -> {pid_to_name.get(owner_id, owner_id)}"
        else:
            print(f"  {group_name:<20} has no members yet - no owner to assign, skipping")
            continue

        to_write.append((group_id, group_name, owner_id, source))
        print(f"  {group_name:<20} {source}")

    if not to_write:
        print("\nNothing to backfill.")
        return

    if not args.apply:
        print(f"\nDRY RUN - {len(to_write)} group(s) would be updated. "
              f"Wrong guess? Add it to {os.path.basename(OVERRIDES_PATH)} (group_name,owner_player_name) and re-run.")
        print("Re-run with --apply once every proposed owner above looks right.")
        return

    for group_id, group_name, owner_id, _ in to_write:
        groups_table.update_item(
            Key={'group_id': group_id},
            UpdateExpression='SET #r = :r',
            ExpressionAttributeNames={'#r': 'roles'},
            ExpressionAttributeValues={':r': {owner_id: 'owner'}}
        )
        print(f"  Wrote: {group_name} -> owner {pid_to_name.get(owner_id, owner_id)}")

    print(f"\nDone. {len(to_write)} group(s) backfilled.")
    print("Everyone else in each group defaults to 'member' (the API already treats an absent role that way).")


if __name__ == '__main__':
    main()
