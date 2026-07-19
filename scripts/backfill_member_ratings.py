"""
NetWorth - one-time backfill: for tournaments created before the
member_ratings snapshot feature existed (like "July 19"), reconstruct what
each player's rating actually was AT THE TOURNAMENT'S CREATION TIME, by
replaying the full match history chronologically up to (but not including)
that timestamp - same methodology as the live Elo formula, just stopped at
a cutoff instead of running to the present.

This writes the result permanently into each entity's `member_ratings`
field, so afterward these tournaments behave identically to ones created
after the snapshot feature shipped - no special-casing needed anywhere else.

Usage:
    pip install boto3 --break-system-packages   (if not already installed)
    python backfill_member_ratings.py --region us-east-1

Safe to re-run - it skips any tournament/entity that already has
member_ratings set, so it only fills in what's missing.
"""
import argparse
import boto3

K_FACTOR = 32


def compute_ratings_as_of(cutoff_date, matches):
    """Replay matches chronologically, stopping strictly before cutoff_date.
    Returns {player_id: rating} for everyone who appeared before then."""
    ratings = {}
    for m in matches:
        if m.get('date', '') >= cutoff_date:
            break
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        score_a = m.get('score_a')
        score_b = m.get('score_b')
        if not team_a or not team_b or score_a is None or score_b is None:
            continue

        score_a, score_b = float(score_a), float(score_b)
        rating_a_avg = sum(ratings.get(pid, 1000.0) for pid in team_a) / len(team_a)
        rating_b_avg = sum(ratings.get(pid, 1000.0) for pid in team_b) / len(team_b)

        actual_a = 1.0 if score_a > score_b else (0.0 if score_a < score_b else 0.5)
        actual_b = 1.0 - actual_a
        expected_a = 1 / (1 + 10 ** ((rating_b_avg - rating_a_avg) / 400))
        expected_b = 1 - expected_a
        delta_a = K_FACTOR * (actual_a - expected_a)
        delta_b = K_FACTOR * (actual_b - expected_b)

        for pid in team_a:
            ratings[pid] = ratings.get(pid, 1000.0) + delta_a
        for pid in team_b:
            ratings[pid] = ratings.get(pid, 1000.0) + delta_b
    return ratings


def inject_ratings(entity, ratings):
    """Fill in member_ratings on one entity occurrence, if missing."""
    if not entity or 'members' not in entity:
        return False
    if 'member_ratings' in entity:
        return False
    entity['member_ratings'] = [int(round(ratings.get(pid, 1000.0))) for pid in entity['members']]
    return True


def backfill_tournament(item, matches):
    created_at = item.get('created_at', '')
    ratings = compute_ratings_as_of(created_at, matches)
    changed = False

    for sg in item.get('subgroups', {}).values():
        for e in sg.get('members', []):
            changed |= inject_ratings(e, ratings)
        for f in sg.get('fixtures', []):
            changed |= inject_ratings(f.get('player_a'), ratings)
            changed |= inject_ratings(f.get('player_b'), ratings)

    if 'knockout' in item:
        for round_ in item['knockout'].get('rounds', []):
            for m in round_:
                changed |= inject_ratings(m.get('player_a'), ratings)
                changed |= inject_ratings(m.get('player_b'), ratings)
        tp = item['knockout'].get('third_place_match')
        if tp:
            changed |= inject_ratings(tp.get('player_a'), ratings)
            changed |= inject_ratings(tp.get('player_b'), ratings)

    return changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tournaments-table', default='networth-tournaments')
    parser.add_argument('--matches-table', default='networth-matches')
    parser.add_argument('--region', default='us-east-1')
    args = parser.parse_args()

    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    tournaments_table = dynamodb.Table(args.tournaments_table)
    matches_table = dynamodb.Table(args.matches_table)

    matches = matches_table.scan().get('Items', [])
    matches.sort(key=lambda m: m.get('date', ''))

    tournaments = tournaments_table.scan().get('Items', [])
    updated_count = 0

    for t in tournaments:
        if backfill_tournament(t, matches):
            tournaments_table.put_item(Item=t)
            updated_count += 1
            print(f"Backfilled: {t.get('name')} ({t['tournament_id']})")

    print(f"\nDone. Updated {updated_count} tournament(s).")


if __name__ == '__main__':
    main()
