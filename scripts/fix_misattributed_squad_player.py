"""
NetWorth - one-time repair: correct a manual-draft squad player who was
substituted (via the pre-v1.57.0 buggy `substitute-squad-player` action,
which silently no-op'd for cross-squad pairs/reps - fixed going forward in
v1.57.0) but whose OLD identity is still baked into the squad's fixed
pairs, its rep entity, and every match snapshot that rep played under -
including matches already scored, because the old player never actually
played them; the new player did.

This is deliberately more aggressive than the normal (fixed) substitution
flow, which intentionally leaves ALREADY-PLAYED matches untouched (because
in the general case, a mid-tournament substitution is legitimate - the
outgoing player really did play their earlier matches). This script is for
the specific case where that assumption is wrong: the old player never
played ANY match attributed to them in this tournament, so relabeling
already-played matches is a correction, not a rewrite of real history.

What it does, for --old-player-id -> --new-player-id within one tournament:
  1. Tournament item (networth-tournaments): fixes the squad's `pairs`
     entry, the matching rep in `reps` (or, for squads-mode tournaments
     with no `reps`, the squad's own `members`/`member_ratings`), and
     every match entity (group_stage + knockout + third_place_match,
     played or not) whose player_a/player_b snapshot still names the old
     player - rebuilding `members`, `member_ratings` (from each member's
     CURRENT rating), and `name` (from each member's CURRENT name).
  2. Match log (networth-matches): every entry tagged with this
     tournament_id whose team_a/team_b contains the old player id gets
     relabeled to the new player id, and the matching name in
     team_a_names/team_b_names is rebuilt from the new player's current
     name. Scores, winner, dates, stage - all untouched.
  3. Elo - ISOLATED to just the real effect of the substitution. A full
     chronological replay (K-factor, doubles adaptive-K, comeback bonus -
     same formula as scripts/repair_ratings_after.py) is run TWICE: once
     against the match history exactly as currently stored ("baseline"),
     and once against the same history with the substitution applied
     ("fixed"). Both replays share the identical formula and ordering, so
     any pre-existing drift between what's currently stored and a from-
     scratch replay (the kind scripts/repair_ratings_after.py already
     exists to periodically correct) shows up IDENTICALLY in both and
     cancels out in the diff. Only where the two replays genuinely
     disagree - the 3 relabeled matches themselves, plus any downstream
     ripple through opponents' later matches - does this script write
     anything, and it writes a DELTA on top of the currently-stored value,
     never a from-scratch replay's absolute number. This was verified
     directly: a true no-op dry run (old-player-id == new-player-id, so
     nothing actually changes) on this tournament's real production data
     still showed several unrelated players' ratings "changing" purely
     from full-replay drift - proof that writing raw replay output would
     have silently corrected unrelated pre-existing drift as a side
     effect. This script does not do that; it only ever touches what the
     substitution itself caused.

Nothing about scores, winner_id, wins/point_diff, or tie decisions is
touched anywhere - only WHO is recorded as having played, never what
happened in the match.

Usage:
    pip install boto3 --break-system-packages   (if not already installed)

    # 1. DRY RUN FIRST - always. Prints every change it would make and
    #    every player's rating (old -> new) without writing anything.
    python fix_misattributed_squad_player.py \
        --tournament-id 656de879-ad8c-46ee-8aa0-65467d82692c \
        --old-player-id ef3d3e0c-2e65-4f36-a4e1-10b1a935c627 \
        --new-player-id e7f2040b-b9af-4c9b-a7d0-7df006f770e3

    # 2. Once the dry-run output looks right, actually write:
    python fix_misattributed_squad_player.py \
        --tournament-id 656de879-ad8c-46ee-8aa0-65467d82692c \
        --old-player-id ef3d3e0c-2e65-4f36-a4e1-10b1a935c627 \
        --new-player-id e7f2040b-b9af-4c9b-a7d0-7df006f770e3 \
        --apply

Safe to re-run - once applied, the old player id no longer appears
anywhere in this tournament, so a second run finds nothing left to do
(match_writes/player_deltas will both come back empty).
"""
import argparse
import copy

import boto3

K_FACTOR = 32
COMEBACK_BONUS_THRESHOLD = 5
COMEBACK_BONUS_PER_POINT = 0.3
COMEBACK_BONUS_CAP = 8
DEFAULT_RATING = 1000


def compute_comeback_bonus(momentum):
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


def rebuild_entity(entity, old_id, new_id, players_by_id, changes, players_table):
    """In-place: swap old_id -> new_id in entity['members'], rebuild
    member_ratings (from each member's CURRENT rating) and name (from
    each member's CURRENT name). Returns True if this entity was touched.

    Looks up EVERY member's current name via a live players_table lookup
    (cached in players_by_id as it goes) rather than assuming players_by_id
    already has them - an entity can have OTHER members beyond old_id/
    new_id (e.g. a doubles partner), and relying on a restricted cache for
    those silently produces their raw player_id as the "name" instead."""
    members = entity.get('members')
    if not members or old_id not in members:
        return False
    idx = members.index(old_id)
    members[idx] = new_id
    ratings = entity.get('member_ratings')
    if ratings is not None and idx < len(ratings):
        ratings[idx] = int(players_by_id.get(new_id, {}).get('rating', DEFAULT_RATING))
    names = []
    for pid in members:
        if pid not in players_by_id:
            p = players_table.get_item(Key={'player_id': pid}).get('Item')
            players_by_id[pid] = p or {'name': pid}
        names.append(players_by_id[pid].get('name', pid))
    old_name = entity.get('name')
    entity['name'] = ' & '.join(names) if len(names) > 1 else names[0]
    changes.append((entity.get('entity_id') or entity.get('player_id'), old_name, entity['name']))
    return True


def fix_tournament_item(item, old_id, new_id, players_by_id, players_table):
    """Returns (changed: bool, report: list[str])."""
    report = []
    entity_changes = []

    # squads-mode: old_id may sit directly in a squad's own members list.
    for squad_id, squad in (item.get('squads') or {}).items():
        if rebuild_entity(squad, old_id, new_id, players_by_id, entity_changes, players_table):
            report.append(f"squad {squad_id} ('{squad.get('name')}'): members updated")
        for pair in squad.get('pairs') or []:
            if old_id in pair:
                pair[pair.index(old_id)] = new_id
                report.append(f"squad {squad_id} ('{squad.get('name')}'): fixed pair {pair}")

    # cross-squad mode: the rep entity is the one actually referenced by matches.
    for rep_id, rep in (item.get('reps') or {}).items():
        if rebuild_entity(rep, old_id, new_id, players_by_id, entity_changes, players_table):
            report.append(f"rep {rep_id}: now '{rep['name']}'")

    match_touch_count = 0

    def fix_matches(matches, where):
        nonlocal match_touch_count
        for i, m in enumerate(matches):
            for side_key in ('player_a', 'player_b'):
                entity = m.get(side_key)
                if entity and rebuild_entity(entity, old_id, new_id, players_by_id, entity_changes, players_table):
                    match_touch_count += 1
                    report.append(f"  {where} match #{i + 1} ({side_key}): now '{entity['name']}' "
                                   f"(played={m.get('played')})")

    for tie in (item.get('group_stage') or {}).get('ties', []):
        fix_matches(tie.get('matches', []), f"tie {tie.get('tie_id')}")
    for rnd_i, rnd in enumerate((item.get('knockout') or {}).get('rounds', [])):
        for tie in rnd:
            fix_matches(tie.get('matches', []), f"knockout round {rnd_i + 1} tie {tie.get('tie_id')}")
    tpm = (item.get('knockout') or {}).get('third_place_match')
    if tpm:
        fix_matches(tpm.get('matches', []), "third-place match")

    changed = bool(report)
    if changed:
        report.append(f"({match_touch_count} match entity snapshot(s) relabeled)")
    return changed, report


def relabel_matches_list(all_matches, tournament_id, old_id, new_id, players_by_id):
    """Mutates matching entries of `all_matches` IN PLACE (team_a/team_b +
    team_a_names/team_b_names). Returns the list of touched match dicts."""
    touched = []
    for m in all_matches:
        if m.get('tournament_id') != tournament_id:
            continue
        changed_here = False
        for side, names_key in (('team_a', 'team_a_names'), ('team_b', 'team_b_names')):
            team = m.get(side) or []
            if old_id in team:
                idx = team.index(old_id)
                team[idx] = new_id
                names = m.get(names_key) or []
                if idx < len(names):
                    names[idx] = players_by_id.get(new_id, {}).get('name', new_id)
                changed_here = True
        if changed_here:
            touched.append(m)
    return touched


def compute_pure_replay(matches):
    """No writes, no side effects. Full chronological Elo replay - same
    formula as scripts/repair_ratings_after.py. Returns:
      ratings_after_by_match: {match_id: {pid: rating}}
      final_ratings: {pid: rating}
    """
    matches_sorted = sorted(matches, key=lambda m: m.get('date', ''))
    current_ratings = {}
    pairing_counts = {}
    ratings_after_by_match = {}

    for m in matches_sorted:
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        score_a = m.get('score_a')
        score_b = m.get('score_b')
        if not team_a or not team_b or score_a is None or score_b is None:
            continue

        score_a, score_b = float(score_a), float(score_b)
        rating_a_avg = sum(current_ratings.get(pid, float(DEFAULT_RATING)) for pid in team_a) / len(team_a)
        rating_b_avg = sum(current_ratings.get(pid, float(DEFAULT_RATING)) for pid in team_b) / len(team_b)

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
            current_ratings[pid] = current_ratings.get(pid, float(DEFAULT_RATING)) + delta_a
        for pid in team_b:
            current_ratings[pid] = current_ratings.get(pid, float(DEFAULT_RATING)) + delta_b

        ratings_after_by_match[m['match_id']] = {pid: int(round(current_ratings[pid])) for pid in team_a + team_b}

    final_ratings = {pid: int(round(r)) for pid, r in current_ratings.items()}
    return ratings_after_by_match, final_ratings


def isolate_incremental_effect(all_matches, tournament_id, old_id, new_id):
    """The core of this script's safety story. Returns:
      match_writes: {match_id: new_ratings_after_dict}  - only matches with
        a genuine, isolated incremental effect from the substitution.
      player_deltas: {pid: delta}                        - only players
        whose final rating genuinely moves because of the substitution.

    Method: run the SAME full replay twice - once on the untouched
    "baseline" history, once on a "fixed" copy with the substitution
    applied - and diff them. Any pre-existing drift between what's stored
    today and a from-scratch replay appears identically in both runs (same
    formula, same order) and cancels out. Only genuine differences survive:
    the 3 relabeled matches themselves, and any downstream ripple through
    shared opponents. Every write is expressed as a delta layered on top of
    the CURRENTLY STORED value, never as a raw replay number - so unrelated
    pre-existing drift for an affected player's OTHER matches is left
    completely alone."""
    baseline_matches = copy.deepcopy(all_matches)
    fixed_matches = copy.deepcopy(all_matches)
    relabel_matches_list(fixed_matches, tournament_id, old_id, new_id, {})  # names don't matter here, only ids

    baseline_ra, baseline_final = compute_pure_replay(baseline_matches)
    fixed_ra, fixed_final = compute_pure_replay(fixed_matches)

    stored_by_id = {m['match_id']: m for m in all_matches}
    fixed_by_id = {m['match_id']: m for m in fixed_matches}

    match_writes = {}
    for mid, fixed_vals in fixed_ra.items():
        stored_m = stored_by_id[mid]
        fixed_m = fixed_by_id[mid]
        stored_ra = stored_m.get('ratings_after') or {}
        baseline_vals = baseline_ra.get(mid, {})

        b_team_a, b_team_b = stored_m.get('team_a') or [], stored_m.get('team_b') or []
        f_team_a, f_team_b = fixed_m.get('team_a') or [], fixed_m.get('team_b') or []

        new_ra = dict(stored_ra)
        touched = False
        for f_list, b_list in ((f_team_a, b_team_a), (f_team_b, b_team_b)):
            for i in range(len(f_list)):
                f_pid = f_list[i]
                b_pid = b_list[i] if i < len(b_list) else f_pid
                f_val = fixed_vals.get(f_pid)
                b_val = baseline_vals.get(b_pid)
                if f_val is None or b_val is None:
                    continue
                s_val = stored_ra.get(b_pid, b_val)  # assume no drift if genuinely missing
                delta = f_val - b_val
                if f_pid != b_pid:
                    # this slot's identity actually changed (the relabel itself)
                    new_ra.pop(b_pid, None)
                    new_ra[f_pid] = s_val + delta
                    touched = True
                elif delta != 0:
                    new_ra[f_pid] = s_val + delta
                    touched = True
        if touched:
            match_writes[mid] = new_ra

    player_deltas = {}
    for pid in set(baseline_final) | set(fixed_final):
        b = baseline_final.get(pid, DEFAULT_RATING)
        f = fixed_final.get(pid, DEFAULT_RATING)
        if f != b:
            player_deltas[pid] = f - b

    return match_writes, player_deltas


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tournament-id', required=True)
    parser.add_argument('--old-player-id', required=True)
    parser.add_argument('--new-player-id', required=True)
    parser.add_argument('--apply', action='store_true', help='actually write changes (default: dry-run only)')
    parser.add_argument('--tournaments-table', default='networth-tournaments')
    parser.add_argument('--matches-table', default='networth-matches')
    parser.add_argument('--players-table', default='networth-players')
    parser.add_argument('--region', default='us-east-1')
    args = parser.parse_args()

    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    tournaments_table = dynamodb.Table(args.tournaments_table)
    matches_table = dynamodb.Table(args.matches_table)
    players_table = dynamodb.Table(args.players_table)

    mode = 'APPLY' if args.apply else 'DRY RUN (nothing will be written)'
    print(f"=== {mode} ===\n")

    item = tournaments_table.get_item(Key={'tournament_id': args.tournament_id}).get('Item')
    if not item:
        print(f"ERROR: no tournament found with id {args.tournament_id}")
        return

    old_p = players_table.get_item(Key={'player_id': args.old_player_id}).get('Item')
    new_p = players_table.get_item(Key={'player_id': args.new_player_id}).get('Item')
    if not old_p or not new_p:
        print("ERROR: old-player-id or new-player-id not found in the players table.")
        return
    players_by_id = {args.old_player_id: old_p, args.new_player_id: new_p}
    print(f"Replacing '{old_p.get('name')}' ({args.old_player_id}) with "
          f"'{new_p.get('name')}' ({args.new_player_id}) in tournament '{item.get('name')}'\n")

    # 1. tournament item
    changed, report = fix_tournament_item(item, args.old_player_id, args.new_player_id, players_by_id, players_table)
    if not changed:
        print("Nothing to fix in the tournament item - old player id not found anywhere in it.")
    else:
        print("--- Tournament item changes ---")
        for line in report:
            print(line)
        if args.apply:
            tournaments_table.put_item(Item=item)
            print("(written)")
        print()

    # 2. Elo isolation - one scan, diffed against a baseline replay so only
    # the substitution's genuine effect (never pre-existing drift) survives.
    all_matches = matches_table.scan().get('Items', [])
    match_writes, player_deltas = isolate_incremental_effect(
        all_matches, args.tournament_id, args.old_player_id, args.new_player_id)

    # 3. match log relabeling (team_a/team_b/names) - separate from the Elo
    # writes above, but applied to the same live table.
    touched_matches = relabel_matches_list(all_matches, args.tournament_id, args.old_player_id,
                                            args.new_player_id, players_by_id)
    print(f"--- Match log entries relabeled: {len(touched_matches)} ---")
    for m in touched_matches:
        print(f"  {m['match_id']} ({m.get('date', '')[:10]}): {m.get('team_a_names')} vs {m.get('team_b_names')}"
              f" -> {m.get('score_a')}-{m.get('score_b')}")
    if args.apply:
        for m in touched_matches:
            matches_table.update_item(
                Key={'match_id': m['match_id']},
                UpdateExpression='SET team_a = :ta, team_b = :tb, team_a_names = :tan, team_b_names = :tbn',
                ExpressionAttributeValues={
                    ':ta': m['team_a'], ':tb': m['team_b'],
                    ':tan': m['team_a_names'], ':tbn': m['team_b_names'],
                }
            )
    print()

    print(f"--- ratings_after snapshots with a genuine isolated effect: {len(match_writes)} ---")
    print("(pre-existing drift unrelated to this substitution - if any exists elsewhere in your")
    print(" data - is deliberately excluded here; only the substitution's own effect is shown)")
    stored_by_id = {m['match_id']: m for m in all_matches}
    for mid, new_ra in match_writes.items():
        old_ra = stored_by_id[mid].get('ratings_after') or {}
        print(f"  {mid} ({stored_by_id[mid].get('date', '')[:10]}): {old_ra} -> {new_ra}")
        if args.apply:
            matches_table.update_item(
                Key={'match_id': mid},
                UpdateExpression='SET ratings_after = :r',
                ExpressionAttributeValues={':r': new_ra}
            )
    print()

    print(f"--- Player ratings with a genuine isolated effect: {len(player_deltas)} ---")
    for pid, delta in player_deltas.items():
        name = players_by_id.get(pid, {}).get('name') or (players_table.get_item(Key={'player_id': pid}).get('Item') or {}).get('name', pid)
        stored_rating = players_by_id.get(pid, {}).get('rating')
        if stored_rating is None:
            p = players_table.get_item(Key={'player_id': pid}).get('Item') or {}
            stored_rating = p.get('rating', DEFAULT_RATING)
        new_rating = int(stored_rating) + delta
        flag = '  <-- old player (reverts, since these matches are no longer theirs)' if pid == args.old_player_id \
            else ('  <-- new player (picks up the correct rating from these matches)' if pid == args.new_player_id else '')
        print(f"  {name} ({pid}): {stored_rating} -> {new_rating}{flag}")
        if args.apply:
            players_table.update_item(Key={'player_id': pid}, UpdateExpression='SET rating = :r',
                                       ExpressionAttributeValues={':r': new_rating})

    if not args.apply:
        print("\nThis was a DRY RUN - nothing was written. Re-run with --apply once this looks right.")
    else:
        print("\nDone - all changes written.")


if __name__ == '__main__':
    main()
