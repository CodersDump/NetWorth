"""
NetWorth - force-set a group's owner, overwriting whatever role data already
exists for that group.

Unlike backfill_group_roles.py (which deliberately skips any group that
already has a `roles` map, so a one-time backfill can never clobber a role
you've since changed by hand), this script is for the opposite situation:
you backfilled the wrong owner and need to correct it.

Reads the same group_owner_overrides.csv format as the backfill script
(group_name,owner_player_name) - reuse the same file, or point at a
different one with --csv.

Usage:
    python set_group_owner.py                    (dry run - shows what would change)
    python set_group_owner.py --apply             (actually overwrites)
    python set_group_owner.py --csv other.csv --apply
"""
import argparse
import csv
import os

import boto3

DEFAULT_CSV = os.path.join(os.path.dirname(__file__), 'group_owner_overrides.csv')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--csv', default=DEFAULT_CSV)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--region', default='us-east-1')
    args = parser.parse_args()

    if not os.path.exists(args.csv):
        print(f"No CSV found at {args.csv}. Format: group_name,owner_player_name")
        return

    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    groups_table = dynamodb.Table('networth-groups')
    players_table = dynamodb.Table('networth-players')

    players = players_table.scan().get('Items', [])
    name_to_pid = {p['name'].strip().lower(): p['player_id'] for p in players}
    groups = {g['group_name'].strip().lower(): g for g in groups_table.scan().get('Items', [])}

    rows = []
    with open(args.csv, newline='') as f:
        for row in csv.DictReader(f):
            name, owner = row.get('group_name', '').strip(), row.get('owner_player_name', '').strip()
            if name and owner:
                rows.append((name, owner))

    to_write = []
    for group_name, owner_name in rows:
        group = groups.get(group_name.lower())
        if not group:
            print(f"  {group_name:<20} NOT FOUND - check spelling against the Groups tab")
            continue
        owner_id = name_to_pid.get(owner_name.lower())
        if not owner_id:
            print(f"  {group_name:<20} owner '{owner_name}' NOT FOUND in PlayersTable")
            continue
        current_roles = group.get('roles', {})
        current_owner_names = [p['name'] for p in players if p['player_id'] in current_roles
                                and current_roles[p['player_id']] == 'owner']
        print(f"  {group_name:<20} current owner(s): {current_owner_names or '(none)'} -> setting: {owner_name}")
        to_write.append((group['group_id'], group_name, owner_id, current_roles, group.get('member_ids', [])))

    if not to_write:
        print("\nNothing to write.")
        return

    if not args.apply:
        print(f"\nDRY RUN - {len(to_write)} group(s) would be overwritten. Re-run with --apply to actually write.")
        return

    for group_id, group_name, owner_id, current_roles, member_ids in to_write:
        # Preserve every OTHER member's existing role; only the owner slot changes.
        # Also demote any previous owner(s) to 'member' rather than leaving them
        # with a stale 'owner' entry alongside the new one.
        new_roles = {pid: ('member' if role == 'owner' else role) for pid, role in current_roles.items()}
        new_roles[owner_id] = 'owner'
        if owner_id not in member_ids:
            member_ids = member_ids + [owner_id]
        groups_table.update_item(
            Key={'group_id': group_id},
            UpdateExpression='SET #r = :r, member_ids = :m',
            ExpressionAttributeNames={'#r': 'roles'},
            ExpressionAttributeValues={':r': new_roles, ':m': member_ids}
        )
        print(f"  Wrote: {group_name} -> owner set")

    print(f"\nDone. {len(to_write)} group(s) corrected.")


if __name__ == '__main__':
    main()
