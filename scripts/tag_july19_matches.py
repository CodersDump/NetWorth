"""
NetWorth - one-time backfill: tag the July 19 tournament's 15 matches
(recorded as standalone matches, before the tournament UI was used) with
tournament_id and stage, so the "Exclude tournament matches" filter and
any other tournament-aware filtering actually applies to them.

Without this, these matches are indistinguishable from any other
standalone match in the Matches table, which is exactly the gap that
caused the "Exclude tournament matches" toggle to show no visible effect
for players who only played in this event.

Usage:
    pip install boto3 --break-system-packages   (if not already installed)
    python tag_july19_matches.py

Safe to re-run - it only sets tournament_id/stage if not already present.
"""
import boto3

TOURNAMENT_ID = "8003a10d-6bc6-4b4d-b8c6-fd1cdd2bf87c"  # the kept "July 19" tournament
GROUP_ID = "8805e3f4-b56e-410f-b2b0-478e4f8234c9"       # Matchpoint - same group the 12 group-stage matches carry

# match_id -> stage ('group' includes the tiebreaker; 'knockout' = semifinals)
MATCH_STAGES = {
    "3315f498-776b-4079-84fb-ecd60fc77681": "group",       # Sohan&Aditya vs Pradeep&Ram C
    "52bd1806-160d-4713-9d2e-11267c885f05": "group",       # Sourabh C&Suraj vs Mirgank&Mohit
    "2ef0e5a7-ac66-4ab9-b68d-52ccb7702200": "group",       # Bibhu&Suren vs Sourabh C&Suraj
    "e74ee3d6-a629-44ee-be64-d839fdcd5d2d": "group",       # Bibhu&Suren vs Mohit&Mirgank
    "6150950a-1412-4429-8760-84500624667b": "group",       # Sourabh C&Suraj vs Mayank&Adi
    "099a37bb-2b34-4092-aa30-e4ef6bb2b272": "group",       # Ram C&Pradeep vs Abhishek K&Gangaram Ghadi
    "0f9b7b12-708b-4eea-879d-e054aef2aef2": "group",       # Aditya&Sohan vs Abhishek K&Gangaram Ghadi
    "adee2fda-e4f3-4d6b-8e1c-02a841c3c0c6": "group",       # Mayank&Adi vs Mohit&Mirgank
    "e101bf48-ac0a-4464-b98d-ac78f7ac6b3b": "group",       # Sambhit&Sandeep vs Ram C&Pradeep
    "87762823-6674-4598-81f6-f9d0a0a8aef1": "group",       # Sambhit&Sandeep vs Aditya&Sohan
    "76807300-26f8-4aaa-bace-333cc9904f96": "group",       # Bibhu&Suren vs Adi&Mayank
    "1a52e317-cebb-4c39-8b94-d90fd1831997": "group",       # Abhishek K&Gangaram Ghadi vs Sambhit&Sandeep
    "39bdd14a-7b90-44f1-a28f-e479e31d2aad": "group",       # Mohit&Mirgank vs Bibhu&Suren (TIEBREAKER)
    "32d3b019-c363-4fe8-8bf8-d7df83323e57": "knockout",    # Abhishek K&Gangaram Ghadi vs Sourabh C&Suraj (SEMI)
    "2ec93d8d-8d3e-420f-a296-12583a2d1b4f": "knockout",    # Mirgank&Mohit vs Pradeep&Ram C (SEMI)
}


def main():
    dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
    table = dynamodb.Table('networth-matches')

    tagged = 0
    skipped = 0
    missing = 0

    for match_id, stage in MATCH_STAGES.items():
        item = table.get_item(Key={'match_id': match_id}).get('Item')
        if not item:
            print(f"NOT FOUND: {match_id}")
            missing += 1
            continue

        # The tiebreaker + both semis were recorded without a group_id
        # (unlike the 12 group-stage matches), so any group-filtered match
        # view silently drops them. Fill it in wherever it's missing, even
        # for matches whose tournament tag is already set.
        if not item.get('group_id'):
            table.update_item(
                Key={'match_id': match_id},
                UpdateExpression='SET group_id = :g',
                ExpressionAttributeValues={':g': GROUP_ID}
            )
            print(f"Set group_id on {match_id}")

        if item.get('tournament_id'):
            print(f"Already tagged, skipping: {match_id}")
            skipped += 1
            continue

        table.update_item(
            Key={'match_id': match_id},
            UpdateExpression='SET tournament_id = :t, stage = :s',
            ExpressionAttributeValues={':t': TOURNAMENT_ID, ':s': stage}
        )
        print(f"Tagged {match_id} as stage={stage}")
        tagged += 1

    print(f"\nDone. Tagged: {tagged}, already tagged: {skipped}, not found: {missing}")


if __name__ == '__main__':
    main()
