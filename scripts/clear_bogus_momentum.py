"""
NetWorth - fix a match whose live-scoring point log is bogus (someone used
the live counter to batch-enter a final score - e.g. tapping 21 points for
one side and then 19 for the other - instead of scoring point-by-point).

Why this matters beyond cosmetics: a fabricated "comeback" isn't just a
wrong Hall of Fame row. compute_comeback_bonus() adds up to +8 rating
points to the winning side's Elo delta based on the deficit in that point
log, and because Elo is path-dependent, that error then propagates into
every subsequent match's math.

Usage:
    # 1. List every match that has momentum data, with the bonus it earned
    python clear_bogus_momentum.py

    # 2. Inspect one match (point log + a SIMULATED before/after ranking
    #    diff showing what clearing it would do - no writes happen)
    python clear_bogus_momentum.py --match-id <id>

    # 3. Actually clear its momentum + point_log and replay all ratings
    python clear_bogus_momentum.py --match-id <id> --apply

The replay is identical to the live recompute_all_ratings() in the matches
Lambda: reset to 1000, chronological replay, adaptive K per doubles
pairing, and comeback bonuses still applied for every OTHER match that has
genuine momentum data. Only the targeted match loses its bonus.

Safe to re-run - clearing an already-cleared match is a no-op, and the
replay is deterministic.
"""
import argparse

import boto3

K_FACTOR = 32
COMEBACK_BONUS_THRESHOLD = 5
COMEBACK_BONUS_PER_POINT = 0.3
COMEBACK_BONUS_CAP = 8


def compute_adaptive_k(pairing_count):
    if pairing_count == 0:
        return 40
    elif pairing_count < 5:
        return K_FACTOR
    else:
        return 20


def compute_comeback_bonus(momentum):
    if not momentum:
        return 0
    deficit = float(momentum.get('winner_overcame_deficit', 0))
    if deficit < COMEBACK_BONUS_THRESHOLD:
        return 0
    return min(deficit * COMEBACK_BONUS_PER_POINT, COMEBACK_BONUS_CAP)


def count_scoring_runs(point_log):
    """A 'run' is an unbroken sequence of points by the same side. A real
    ~40-point badminton game has many; a batch-entered final score has 2."""
    runs = 0
    prev = None
    for p in point_log or []:
        if p != prev:
            runs += 1
            prev = p
    return runs


def replay_ratings(matches, skip_momentum_match_id=None):
    """Full chronological replay, mirroring the live recompute_all_ratings.
    If skip_momentum_match_id is set, that match is treated as having no
    momentum (its comeback bonus is dropped); everything else is identical."""
    ratings = {}
    pairing_counts = {}

    for m in matches:
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

        if m.get('match_type') == 'doubles':
            k_a = compute_adaptive_k(pairing_counts.get(frozenset(team_a), 0)) if len(team_a) == 2 else K_FACTOR
            k_b = compute_adaptive_k(pairing_counts.get(frozenset(team_b), 0)) if len(team_b) == 2 else K_FACTOR
            if len(team_a) == 2:
                key = frozenset(team_a)
                pairing_counts[key] = pairing_counts.get(key, 0) + 1
            if len(team_b) == 2:
                key = frozenset(team_b)
                pairing_counts[key] = pairing_counts.get(key, 0) + 1
        else:
            k_a = k_b = K_FACTOR

        delta_a = k_a * (actual_a - expected_a)
        delta_b = k_b * (actual_b - expected_b)

        winner = m.get('winner')
        momentum = None if m.get('match_id') == skip_momentum_match_id else m.get('momentum')
        if momentum:
            bonus = compute_comeback_bonus(momentum)
            if winner == 'A':
                delta_a += bonus
            elif winner == 'B':
                delta_b += bonus

        for pid in team_a:
            ratings[pid] = ratings.get(pid, 1000.0) + delta_a
        for pid in team_b:
            ratings[pid] = ratings.get(pid, 1000.0) + delta_b

    return ratings


def rankings_diff(names, before, after):
    """Print old vs new rating and rank position for every affected player."""
    def ranked(r):
        order = sorted(r, key=lambda pid: (-r[pid], names.get(pid, pid)))
        return {pid: i + 1 for i, pid in enumerate(order)}

    rank_before = ranked(before)
    rank_after = ranked(after)
    rows = []
    for pid in before:
        b, a = int(round(before[pid])), int(round(after.get(pid, 1000.0)))
        rb, ra = rank_before[pid], rank_after.get(pid)
        rows.append((names.get(pid, pid), b, a, a - b, rb, ra))
    rows.sort(key=lambda r: r[5])

    print(f"\n{'player':<18}{'rating':>8}{'->':>4}{'new':>6}{'diff':>7}{'rank':>7}{'->':>4}{'new':>5}")
    any_change = False
    for name, b, a, diff, rb, ra in rows:
        marker = ''
        if diff != 0 or rb != ra:
            any_change = True
            marker = '  <- changed'
        print(f"{name:<18}{b:>8}{'':>4}{a:>6}{diff:>+7}{rb:>7}{'':>4}{ra:>5}{marker}")
    if not any_change:
        print("(no rating or rank changed)")
    return any_change


def main():
    parser = argparse.ArgumentParser(description='Inspect/clear bogus live-scoring momentum data')
    parser.add_argument('--match-id')
    parser.add_argument('--apply', action='store_true', help='actually clear + replay + write (default is simulate only)')
    parser.add_argument('--region', default='us-east-1')
    args = parser.parse_args()

    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    matches_table = dynamodb.Table('networth-matches')
    players_table = dynamodb.Table('networth-players')

    matches = matches_table.scan().get('Items', [])
    matches.sort(key=lambda m: m.get('date', ''))
    players = players_table.scan().get('Items', [])
    names = {p['player_id']: p.get('name', p['player_id']) for p in players}

    def teams_str(m):
        return f"{'/'.join(m.get('team_a_names', []))} {int(m.get('score_a', 0))}-{int(m.get('score_b', 0))} {'/'.join(m.get('team_b_names', []))}"

    if not args.match_id:
        print("Matches with live-scoring momentum data:\n")
        found = False
        for m in matches:
            momentum = m.get('momentum')
            if not momentum:
                continue
            found = True
            deficit = float(momentum.get('winner_overcame_deficit', 0))
            bonus = compute_comeback_bonus(momentum)
            runs = count_scoring_runs(m.get('point_log'))
            total_points = len(m.get('point_log') or [])
            flag = '  ⚠ SUSPECTED BATCH ENTRY' if total_points >= 20 and runs <= 3 else ''
            print(f"  {m.get('date', '')[:19]}  {teams_str(m)}")
            print(f"      deficit overcome: {deficit:g}, bonus applied: +{bonus:g}, "
                  f"point log: {total_points} points in {runs} run(s){flag}")
            print(f"      match_id: {m['match_id']}\n")
        if not found:
            print("  (none)")
        print("Inspect one with: python clear_bogus_momentum.py --match-id <id>")
        return

    target = next((m for m in matches if m['match_id'] == args.match_id), None)
    if not target:
        print(f"ERROR: no match found with id {args.match_id}")
        return

    print(f"Match: {teams_str(target)}  ({target.get('date', '')[:19]})")
    momentum = target.get('momentum')
    point_log = target.get('point_log')
    if not momentum and not point_log:
        print("This match has no momentum/point_log data - nothing to clear.")
        return
    print(f"momentum: {momentum}")
    if point_log:
        runs = count_scoring_runs(point_log)
        print(f"point_log ({len(point_log)} points, {runs} scoring run(s)): {''.join(point_log)}")
    print(f"comeback bonus this earned the winner: +{compute_comeback_bonus(momentum):g} rating\n")

    before = replay_ratings(matches)                                    # world as it is
    after = replay_ratings(matches, skip_momentum_match_id=args.match_id)  # world without this bonus
    print("Impact of clearing it (simulated full replay):")
    rankings_diff(names, before, after)

    if not args.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply to clear and recompute for real.")
        return

    # Preserve a console record of what we're deleting, then clear it.
    matches_table.update_item(
        Key={'match_id': args.match_id},
        UpdateExpression='REMOVE momentum, point_log'
    )
    print(f"\nCleared momentum + point_log on {args.match_id}.")

    # Full authoritative replay (the target now genuinely has no momentum),
    # rebuilding each match's ratings_after snapshot and every player's
    # current rating - same as the live Lambda does after an edit/deletion.
    matches = matches_table.scan().get('Items', [])
    matches.sort(key=lambda m: m.get('date', ''))

    ratings = {}
    pairing_counts = {}
    repaired = 0
    for m in matches:
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        score_a = m.get('score_a')
        score_b = m.get('score_b')
        if not team_a or not team_b or score_a is None or score_b is None:
            continue
        score_a, score_b = float(score_a), float(score_b)
        ra = sum(ratings.get(p, 1000.0) for p in team_a) / len(team_a)
        rb = sum(ratings.get(p, 1000.0) for p in team_b) / len(team_b)
        aa = 1.0 if score_a > score_b else (0.0 if score_a < score_b else 0.5)
        ea = 1 / (1 + 10 ** ((rb - ra) / 400))
        if m.get('match_type') == 'doubles':
            k_a = compute_adaptive_k(pairing_counts.get(frozenset(team_a), 0)) if len(team_a) == 2 else K_FACTOR
            k_b = compute_adaptive_k(pairing_counts.get(frozenset(team_b), 0)) if len(team_b) == 2 else K_FACTOR
            if len(team_a) == 2:
                pairing_counts[frozenset(team_a)] = pairing_counts.get(frozenset(team_a), 0) + 1
            if len(team_b) == 2:
                pairing_counts[frozenset(team_b)] = pairing_counts.get(frozenset(team_b), 0) + 1
        else:
            k_a = k_b = K_FACTOR
        da = k_a * (aa - ea)
        db = k_b * ((1.0 - aa) - (1 - ea))
        momentum = m.get('momentum')
        if momentum:
            bonus = compute_comeback_bonus(momentum)
            if m.get('winner') == 'A':
                da += bonus
            elif m.get('winner') == 'B':
                db += bonus
        for p in team_a:
            ratings[p] = ratings.get(p, 1000.0) + da
        for p in team_b:
            ratings[p] = ratings.get(p, 1000.0) + db
        new_after = {p: int(round(ratings[p])) for p in team_a + team_b}
        if m.get('ratings_after') != new_after:
            matches_table.update_item(
                Key={'match_id': m['match_id']},
                UpdateExpression='SET ratings_after = :r',
                ExpressionAttributeValues={':r': new_after}
            )
            repaired += 1

    for pid, rating in ratings.items():
        players_table.update_item(Key={'player_id': pid}, UpdateExpression='SET rating = :r',
                                   ExpressionAttributeValues={':r': int(round(rating))})

    print(f"Replayed all matches: repaired ratings_after on {repaired} record(s), refreshed all current ratings.")
    print("\nREMINDER: weekly deltas changed, so also re-run:  python backfill_progress_history.py")


if __name__ == '__main__':
    main()
