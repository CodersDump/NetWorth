"""
NetWorth - one-time reconstruction of the "July 19" tournament.

All 15 real matches (12 group-stage, 1 tiebreaker, 2 semifinals) were
recorded as standalone matches rather than through the tournament UI, so
player ratings are already correct - this script does NOT touch Elo or
the Matches table. It only builds a Tournaments table item whose bracket
matches what actually happened, with the final left unplayed, so the final
can be scored through the app's normal tournament scoring UI.

Usage:
    pip install boto3 --break-system-packages   (if not already installed)
    python reconstruct_july19_tournament.py --group-name Matchpoint --region us-east-1

Edit GROUP_A / GROUP_B / TIEBREAKER / SEMIS below if names or scores need
correcting before running.
"""
import argparse
import uuid
from datetime import datetime, timezone

import boto3

# ---- Real match data (edit here if anything needs correcting) ----

# Each fixture: (team_a_names_tuple, team_b_names_tuple, score_a, score_b)
GROUP_A_TEAMS = [
    ("Sourabh C", "Suraj"),
    ("Bibhu", "Suren"),
    ("Mirgank", "Mohit"),
    ("Mayank", "Adi"),
]
GROUP_A_FIXTURES = [
    (("Sourabh C", "Suraj"), ("Mirgank", "Mohit"), 21, 12),
    (("Bibhu", "Suren"), ("Sourabh C", "Suraj"), 7, 21),
    (("Bibhu", "Suren"), ("Mirgank", "Mohit"), 16, 21),
    (("Sourabh C", "Suraj"), ("Mayank", "Adi"), 21, 14),
    (("Mayank", "Adi"), ("Mirgank", "Mohit"), 21, 19),
    (("Bibhu", "Suren"), ("Mayank", "Adi"), 21, 16),
]
GROUP_A_TIEBREAKER = (("Mirgank", "Mohit"), ("Bibhu", "Suren"), 23, 21)

GROUP_B_TEAMS = [
    ("Sohan", "Aditya"),
    ("Pradeep", "Ram C"),
    ("Abhishek K", "Gangaram Ghadi"),
    ("Sambvit", "Sandeep"),
]
GROUP_B_FIXTURES = [
    (("Sohan", "Aditya"), ("Pradeep", "Ram C"), 17, 21),
    (("Pradeep", "Ram C"), ("Abhishek K", "Gangaram Ghadi"), 21, 15),
    (("Sohan", "Aditya"), ("Abhishek K", "Gangaram Ghadi"), 12, 21),
    (("Sambvit", "Sandeep"), ("Pradeep", "Ram C"), 11, 21),
    (("Sambvit", "Sandeep"), ("Sohan", "Aditya"), 21, 7),
    (("Abhishek K", "Gangaram Ghadi"), ("Sambvit", "Sandeep"), 21, 17),
]

# Semifinals (knockout round 1) - already played
SEMIS = [
    (("Sourabh C", "Suraj"), ("Abhishek K", "Gangaram Ghadi"), 21, 19),
    (("Mirgank", "Mohit"), ("Pradeep", "Ram C"), 21, 13),
]

TOURNAMENT_NAME = "July 19"
POINTS_TO_WIN = 21
CREATED_AT = "2026-07-19T02:15:00+00:00"  # just before the first match


def build_entity(players_by_name, name_a, name_b):
    p1 = players_by_name[name_a]
    p2 = players_by_name[name_b]
    return {
        'player_id': str(uuid.uuid4()),
        'name': f"{p1['name']} & {p2['name']}",
        'members': [p1['player_id'], p2['player_id']]
    }


def build_fixture(entity_by_names, fixture_tuple):
    (a_names, b_names, score_a, score_b) = fixture_tuple
    entity_a = entity_by_names[a_names]
    entity_b = entity_by_names[b_names]
    winner_id = entity_a['player_id'] if score_a > score_b else entity_b['player_id']
    return {
        'fixture_id': str(uuid.uuid4()),
        'player_a': entity_a,
        'player_b': entity_b,
        'games': [{'score_a': score_a, 'score_b': score_b}],
        'games_won_a': 1 if score_a > score_b else 0,
        'games_won_b': 1 if score_b > score_a else 0,
        'played': True,
        'winner_id': winner_id
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--group-name', default='Matchpoint')
    parser.add_argument('--groups-table', default='networth-groups')
    parser.add_argument('--players-table', default='networth-players')
    parser.add_argument('--tournaments-table', default='networth-tournaments')
    parser.add_argument('--region', default='us-east-1')
    args = parser.parse_args()

    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    groups_table = dynamodb.Table(args.groups_table)
    players_table = dynamodb.Table(args.players_table)
    tournaments_table = dynamodb.Table(args.tournaments_table)

    # Look up the real group_id by name
    groups = groups_table.scan().get('Items', [])
    matching_group = next((g for g in groups if g['group_name'] == args.group_name), None)
    if not matching_group:
        print(f"ERROR: no group named '{args.group_name}' found.")
        return
    group_id = matching_group['group_id']

    # Look up all players by name
    players = players_table.scan().get('Items', [])
    players_by_name = {p['name']: p for p in players}

    all_names = set()
    for a, b in GROUP_A_TEAMS + GROUP_B_TEAMS:
        all_names.add(a)
        all_names.add(b)
    missing = [n for n in all_names if n not in players_by_name]
    if missing:
        print(f"ERROR: these player names were not found in the Players table: {missing}")
        print("Fix the name spelling in this script or in the app, then re-run.")
        return

    # Build team entities
    def make_entities(team_list):
        entity_by_names = {}
        for a, b in team_list:
            entity_by_names[(a, b)] = build_entity(players_by_name, a, b)
        return entity_by_names

    entity_a_map = make_entities(GROUP_A_TEAMS)
    entity_b_map = make_entities(GROUP_B_TEAMS)

    group_a_fixtures = [build_fixture(entity_a_map, f) for f in GROUP_A_FIXTURES]
    group_a_fixtures.append(build_fixture(entity_a_map, GROUP_A_TIEBREAKER))
    group_b_fixtures = [build_fixture(entity_b_map, f) for f in GROUP_B_FIXTURES]

    subgroups = {
        'A': {'members': list(entity_a_map.values()), 'fixtures': group_a_fixtures},
        'B': {'members': list(entity_b_map.values()), 'fixtures': group_b_fixtures}
    }

    # Build semifinal round using the SAME team entities already created above,
    # so player_ids stay consistent across group stage and knockout.
    combined_entities = {**entity_a_map, **entity_b_map}
    semi_matches = []
    for (a_names, b_names, score_a, score_b) in SEMIS:
        entity_a = combined_entities[a_names]
        entity_b = combined_entities[b_names]
        winner_id = entity_a['player_id'] if score_a > score_b else entity_b['player_id']
        semi_matches.append({
            'match_id': str(uuid.uuid4()),
            'player_a': entity_a,
            'player_b': entity_b,
            'games': [{'score_a': score_a, 'score_b': score_b}],
            'games_won_a': 1 if score_a > score_b else 0,
            'games_won_b': 1 if score_b > score_a else 0,
            'played': True,
            'winner_id': winner_id
        })

    # Build the final (round 2) - NOT played yet, winners of the semis feed in
    final_entities = []
    for m in semi_matches:
        winner_entity = m['player_a'] if m['player_a']['player_id'] == m['winner_id'] else m['player_b']
        final_entities.append(winner_entity)

    final_match = {
        'match_id': str(uuid.uuid4()),
        'player_a': final_entities[0],
        'player_b': final_entities[1],
        'games': [],
        'games_won_a': 0,
        'games_won_b': 0,
        'played': False,
        'winner_id': None
    }

    tournament_id = str(uuid.uuid4())
    item = {
        'tournament_id': tournament_id,
        'group_id': group_id,
        'name': TOURNAMENT_NAME,
        'format': 'groups_then_knockout',
        'match_type': 'doubles',
        'points_to_win': POINTS_TO_WIN,
        'best_of': 1,
        'advance_per_group': 2,
        'created_at': CREATED_AT,
        'subgroups': subgroups,
        'knockout': {'rounds': [semi_matches, [final_match]]},
        'status': 'knockout'
    }

    tournaments_table.put_item(Item=item)
    print(f"Created tournament '{TOURNAMENT_NAME}' with tournament_id: {tournament_id}")
    print(f"Final: {final_entities[0]['name']} vs {final_entities[1]['name']}")
    print("Go to the Tournaments section in the app, select this tournament, and submit the final's score when it's played.")


if __name__ == '__main__':
    main()
