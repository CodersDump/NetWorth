"""
NetWorth - one-time repair: fix a display-name corruption left behind by
an earlier run of fix_misattributed_squad_player.py. That script's name
rebuild only had the OLD and NEW player's names loaded, not every OTHER
member of a doubles pair/rep - so a partner's name got silently replaced
by their raw player_id instead of their actual name (e.g. "Guddu &
b24d1dc0-1da3-4914-8d8b-55151eb8138e" instead of "Guddu & Mirgank").

This script re-derives every REP and MATCH-PLAYER entity's `name` field
from a live lookup of each of its `members`' current name in the players
table - never a restricted cache - so this class of bug can't recur here,
and fixes whatever's already wrong. It only ever touches `name` fields;
scores, ratings, ratings_after, pairs, and every other field are left
completely alone.

Squads are deliberately NOT touched: a squad's `name` is a custom display
name the organizer/leader chose ("Smashers", "Team Tanay") - never an
auto-derived list of its members, unlike a rep or a match player entity.
Rebuilding it here would silently overwrite a real, deliberately-set name
with a generic member list.

Usage:
    pip install boto3 --break-system-packages   (if not already installed)

    # dry run first - prints every name it would fix, writes nothing
    python fix_entity_names.py --tournament-id 656de879-ad8c-46ee-8aa0-65467d82692c

    # then apply
    python fix_entity_names.py --tournament-id 656de879-ad8c-46ee-8aa0-65467d82692c --apply

Safe to re-run - idempotent, only writes when a name actually differs
from what a fresh lookup produces.
"""
import argparse

import boto3


def correct_name(members, players_table, name_cache):
    names = []
    for pid in members:
        if pid not in name_cache:
            p = players_table.get_item(Key={'player_id': pid}).get('Item')
            name_cache[pid] = p['name'] if p else pid
        names.append(name_cache[pid])
    return ' & '.join(names) if len(names) > 1 else names[0]


def fix_entity(entity, players_table, name_cache, changes, where):
    members = entity.get('members')
    if not members:
        return
    correct = correct_name(members, players_table, name_cache)
    current = entity.get('name')
    if current != correct:
        changes.append((where, current, correct))
        entity['name'] = correct


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tournament-id', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--tournaments-table', default='networth-tournaments')
    parser.add_argument('--players-table', default='networth-players')
    parser.add_argument('--region', default='us-east-1')
    args = parser.parse_args()

    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    tournaments_table = dynamodb.Table(args.tournaments_table)
    players_table = dynamodb.Table(args.players_table)

    item = tournaments_table.get_item(Key={'tournament_id': args.tournament_id}).get('Item')
    if not item:
        print(f"ERROR: no tournament found with id {args.tournament_id}")
        return

    name_cache = {}
    changes = []

    # Squads are deliberately excluded: a squad's `name` is a custom
    # display name the organizer/leader set ("Smashers", "Team Tanay") -
    # never auto-derived from its members' names, unlike a rep or a match
    # player entity. The real substitute_squad_player backend code (see
    # backend/lambdas/tournaments/index.py) confirms this: it rebuilds a
    # rep's and a match entity's name on substitution, but never a
    # squad's. Renaming squads here would destroy real, deliberately-set
    # names - so this script never touches item['squads'][*]['name'].

    for rep_id, rep in (item.get('reps') or {}).items():
        fix_entity(rep, players_table, name_cache, changes, f"rep {rep_id}")

    def fix_matches(matches, where):
        for i, m in enumerate(matches):
            for side_key in ('player_a', 'player_b'):
                entity = m.get(side_key)
                if entity:
                    fix_entity(entity, players_table, name_cache, changes, f"{where} match #{i + 1} ({side_key})")

    for tie in (item.get('group_stage') or {}).get('ties', []):
        fix_matches(tie.get('matches', []), f"tie {tie.get('tie_id')}")
    for rnd_i, rnd in enumerate((item.get('knockout') or {}).get('rounds', [])):
        for tie in rnd:
            fix_matches(tie.get('matches', []), f"knockout round {rnd_i + 1} tie {tie.get('tie_id')}")
    tpm = (item.get('knockout') or {}).get('third_place_match')
    if tpm:
        fix_matches(tpm.get('matches', []), "third-place match")

    print(f"=== {'APPLY' if args.apply else 'DRY RUN (nothing will be written)'} ===\n")
    print(f"--- Names to fix: {len(changes)} ---")
    for where, old_name, new_name in changes:
        print(f"  {where}: '{old_name}' -> '{new_name}'")

    if args.apply and changes:
        tournaments_table.put_item(Item=item)
        print("\n(written)")
    elif not args.apply:
        print("\nThis was a DRY RUN - nothing was written. Re-run with --apply once this looks right.")
    else:
        print("\nNothing to fix.")


if __name__ == '__main__':
    main()
