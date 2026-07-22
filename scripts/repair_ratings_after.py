"""
NetWorth - one-time repair: fix stale ratings_after snapshots.

Before a recent bug fix, correcting a match's score (or deleting a
tournament) only updated each player's *current* rating in the Players
table - it never went back and fixed the ratings_after snapshot stored on
each individual match, which is exactly what the Rating History graph
reads. So a correction fixed today's number but left the historical
graph permanently showing the old, wrong trajectory.

This script re-runs the (now-fixed) full chronological replay and writes
the corrected ratings_after back onto every match record, repairing any
data written before the fix was deployed.

Usage:
    pip install boto3 --break-system-packages   (if not already installed)
    python repair_ratings_after.py

Safe to re-run - it's idempotent (only writes when a value actually
changes) and does not touch scores, winners, or anything else about the
match records.
"""
import boto3

K_FACTOR = 32
COMEBACK_BONUS_THRESHOLD = 5
COMEBACK_BONUS_PER_POINT = 0.3
COMEBACK_BONUS_CAP = 8


def compute_comeback_bonus(momentum):
    """Kept identical to the live Lambdas: extra rating for the winner when
    the point-by-point log shows they overcame a genuine mid-game deficit.
    Without replaying this here, running the repair would silently strip
    every legitimately-earned comeback bonus out of the ratings."""
    if not momentum:
        return 0
    deficit = float(momentum.get('winner_overcame_deficit', 0))
    if deficit < COMEBACK_BONUS_THRESHOLD:
        return 0
    return min(deficit * COMEBACK_BONUS_PER_POINT, COMEBACK_BONUS_CAP)


def compute_adaptive_k(pairing_count):
    if pairing_count == 0:
        return 40
    elif pairing_count < 5:
        return K_FACTOR
    else:
        return 20


def main():
    dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
    players_table = dynamodb.Table('networth-players')
    matches_table = dynamodb.Table('networth-matches')

    players = players_table.scan().get('Items', [])
    current_ratings = {p['player_id']: 1000.0 for p in players}
    pairing_counts = {}

    matches = matches_table.scan().get('Items', [])
    matches.sort(key=lambda m: m.get('date', ''))

    updated_count = 0

    for m in matches:
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        score_a = m.get('score_a')
        score_b = m.get('score_b')
        if not team_a or not team_b or score_a is None or score_b is None:
            continue

        score_a, score_b = float(score_a), float(score_b)
        rating_a_avg = sum(current_ratings.get(pid, 1000.0) for pid in team_a) / len(team_a)
        rating_b_avg = sum(current_ratings.get(pid, 1000.0) for pid in team_b) / len(team_b)

        actual_a = 1.0 if score_a > score_b else (0.0 if score_a < score_b else 0.5)
        actual_b = 1.0 - actual_a
        expected_a = 1 / (1 + 10 ** ((rating_b_avg - rating_a_avg) / 400))
        expected_b = 1 - expected_a

        if m.get('match_type') == 'doubles':
            k_a = compute_adaptive_k(pairing_counts.get(frozenset(team_a), 0)) if len(team_a) == 2 else K_FACTOR
            k_b = compute_adaptive_k(pairing_counts.get(frozenset(team_b), 0)) if len(team_b) == 2 else K_FACTOR
            if len(team_a) == 2:
                key_a = frozenset(team_a)
                pairing_counts[key_a] = pairing_counts.get(key_a, 0) + 1
            if len(team_b) == 2:
                key_b = frozenset(team_b)
                pairing_counts[key_b] = pairing_counts.get(key_b, 0) + 1
        else:
            k_a = k_b = K_FACTOR

        delta_a = k_a * (actual_a - expected_a)
        delta_b = k_b * (actual_b - expected_b)

        winner = m.get('winner')
        momentum = m.get('momentum')
        if momentum:
            bonus = compute_comeback_bonus(momentum)
            if winner == 'A':
                delta_a += bonus
            elif winner == 'B':
                delta_b += bonus

        for pid in team_a:
            current_ratings[pid] = current_ratings.get(pid, 1000.0) + delta_a
        for pid in team_b:
            current_ratings[pid] = current_ratings.get(pid, 1000.0) + delta_b

        new_ratings_after = {pid: int(round(current_ratings[pid])) for pid in team_a + team_b}
        if m.get('ratings_after') != new_ratings_after:
            matches_table.update_item(
                Key={'match_id': m['match_id']},
                UpdateExpression='SET ratings_after = :r',
                ExpressionAttributeValues={':r': new_ratings_after}
            )
            updated_count += 1
            print(f"Repaired: {m['match_id']} ({m.get('date', '')[:10]}) -> {new_ratings_after}")

    for pid, rating in current_ratings.items():
        players_table.update_item(Key={'player_id': pid}, UpdateExpression='SET rating = :r',
                                   ExpressionAttributeValues={':r': int(round(rating))})

    print(f"\nDone. Repaired {updated_count} match record(s). All current ratings also refreshed.")


if __name__ == '__main__':
    main()
