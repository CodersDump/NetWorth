"""
NetWorth - matches Lambda (singles + doubles)

Routes:
    POST /matches  -> record a match, updates Elo ratings
    GET  /matches?group_id=X&player_id=Y  -> game log, optionally filtered

Body for POST /matches:
    {
      "match_type": "singles" | "doubles",
      "team_a": ["player_id1"] or ["player_id1", "player_id2"],
      "team_b": ["player_id1"] or ["player_id1", "player_id2"],
      "score_a": 21, "score_b": 15,
      "group_id": "optional",
      "point_log": ["A", "A", "B", "A", ...]   (optional, from live scoring)
    }

point_log is an ordered list of which team won each point, if the match was
recorded live via the point-by-point counter. When present, it's validated
against the final score and used to compute simple momentum stats (longest
scoring streak per team, biggest deficit the eventual winner overcame).

Elo approach for doubles: team rating = average of teammates' current
ratings. Expected score computed from team ratings. The resulting rating
delta is applied in full to each teammate individually (based on their own
current rating), so two players on a winning team both move, but a much
higher-rated player carried by a lower-rated partner still updates off
their own baseline.

Env vars:
    MATCHES_TABLE - DynamoDB table name for matches
    PLAYERS_TABLE - DynamoDB table name for players
"""
import json
import os
import uuid
import boto3
from datetime import datetime, timezone, date, timedelta

dynamodb = boto3.resource('dynamodb')
matches_table = dynamodb.Table(os.environ['MATCHES_TABLE'])
players_table = dynamodb.Table(os.environ['PLAYERS_TABLE'])
tournaments_table = dynamodb.Table(os.environ['TOURNAMENTS_TABLE'])
groups_table = dynamodb.Table(os.environ['GROUPS_TABLE'])
history_table = dynamodb.Table(os.environ['PROGRESS_HISTORY_TABLE'])

K_FACTOR = 32


def display_name(player_item, fallback=None):
    """Single source of truth for name formatting: 'Nickname (Real Name)'
    when a nickname is set, plain name otherwise. Used everywhere a player
    record needs to become display text, so nickname support can't drift
    out of sync in one function while another still shows a bare name."""
    if not player_item:
        return fallback
    name = player_item.get('name', fallback)
    nickname = player_item.get('nickname')
    return f"{nickname} ({name})" if nickname else name


COMEBACK_BONUS_THRESHOLD = 5   # minimum deficit overcome to count as a genuine comeback
COMEBACK_BONUS_PER_POINT = 0.3
COMEBACK_BONUS_CAP = 8


def compute_comeback_bonus(momentum):
    """Extra rating-point bonus for the winning side, on top of the
    standard Elo delta, when they overcame a genuine mid-game deficit.
    Only ever non-zero for matches with a point-by-point log, since only
    that data can detect a comeback trajectory at all - a manually-entered
    final score has no way to know if a match was ever close."""
    if not momentum:
        return 0
    deficit = float(momentum.get('winner_overcame_deficit', 0))
    if deficit < COMEBACK_BONUS_THRESHOLD:
        return 0
    return min(deficit * COMEBACK_BONUS_PER_POINT, COMEBACK_BONUS_CAP)
CONFIRMATION_CODE = os.environ['CONFIRMATION_CODE']  # supplied at deploy time via GitHub Secrets -> CFN parameter, never stored in the repo


def _is_valid_completed_game(score_a, score_b, target):
    """
    BWF-style badminton scoring: first to `target` points wins, but must lead
    by 2 (deuce continues past target); hard cap at target+9 (e.g. 21 -> 30),
    where reaching the cap wins outright regardless of margin.
    """
    cap = target + 9
    hi, lo = max(score_a, score_b), min(score_a, score_b)
    if hi > cap or lo > cap:
        return False
    if hi == cap:
        return True
    if hi >= target and (hi - lo) >= 2:
        return True
    return False


def _caller_claims(event):
    """Claims API Gateway's Cognito Authorizer attaches to the request.
    Only present on the isolated /record-match and /profile-secure routes."""
    return (event.get('requestContext') or {}).get('authorizer', {}).get('claims') or {}


def _is_super_admin(claims):
    groups = (claims.get('cognito:groups') or '').split(',')
    return 'SuperAdmin' in groups


def _can_view_profile(claims, target_player_id):
    """SuperAdmin sees everyone. Anyone can view their own profile. A
    logged-in member can view another player's profile only if they share
    at least one group - matches the spec: 'if I'm part of 3 groups, I
    can see all members across those 3 groups'."""
    if _is_super_admin(claims):
        return True
    caller_player_id = claims.get('custom:player_id')
    if not caller_player_id:
        return False
    if caller_player_id == target_player_id:
        return True
    groups = groups_table.scan().get('Items', [])
    for g in groups:
        members = g.get('member_ids', [])
        if caller_player_id in members and target_player_id in members:
            return True
    return False


def _requires_linked_member(claims):
    """Signing up is not the same as being a member. Cognito self-signup is
    open to anyone with a working inbox - including disposable ones - so a
    bare session proves only that someone controls an email address. It
    proves nothing about belonging to this club.

    The real membership signal is a custom:player_id that resolves to a
    LIVE player row, because that is only ever set by an approved claim or
    by an admin. Anything that creates or mutates shared data checks this,
    not merely "is logged in".

    Returns an error response, or None when the caller is a real member.
    """
    if _is_super_admin(claims):
        return None
    player_id = claims.get('custom:player_id')
    if not player_id:
        return _response(403, {'error': 'your account is not linked to a player yet - claim your profile first'})
    if not players_table.get_item(Key={'player_id': player_id}).get('Item'):
        return _response(403, {'error': 'the player linked to your account no longer exists - claim your profile again'})
    return None


def record_match_enforced(event):
    claims = _caller_claims(event)
    if not claims:
        return _response(403, {'error': 'log in to record a match'})
    not_member = _requires_linked_member(claims)
    if not_member:
        return not_member
    return record_match(event)


def profile_view_enforced(event):
    """Entry point for the isolated /profile-secure/{proxy+} catch-all.
    Determines which player's profile is being requested from whichever
    query param is present, checks _can_view_profile, then delegates to
    the existing list_matches() unchanged - same computation logic either
    way, just gated entry."""
    claims = _caller_claims(event)
    if not claims:
        return _response(403, {'error': 'log in to view profiles'})

    params = event.get('queryStringParameters') or {}
    target = (params.get('profile_bundle_for') or params.get('player_id')
              or params.get('partnerships_for') or params.get('radar_for')
              or params.get('head_to_head'))
    if not target:
        return _response(400, {'error': 'no player specified'})
    if not _can_view_profile(claims, target):
        return _response(403, {'error': 'you can only view profiles of players who share a group with you'})

    return list_matches(event)


def handler(event, context):
    try:
        method = event.get('httpMethod')
        match_id = (event.get('pathParameters') or {}).get('match_id')

        # Epic 7: the only way to record a match now requires a real
        # Cognito login, via this isolated top-level route (same platform
        # reason as every other isolated route this session - a specific
        # path can't sit alongside {proxy+}/ANY at the same parent).
        if event.get('resource') == '/record-match' and method == 'POST':
            return record_match_enforced(event)

        # Reorder a single day's matches by swapping their timestamps, then
        # replay ratings. SuperAdmin only - it rewrites history.
        if event.get('resource') == '/reorder-matches' and method == 'POST':
            return reorder_matches(event)

        # Epic 7 extension: profile viewing is now genuinely restricted -
        # guests can't view any profile at all; logged-in members can only
        # view profiles of players sharing at least one group with them
        # (or their own); SuperAdmin sees everyone. Reached via the
        # isolated /profile-secure/{proxy+} catch-all (same reasoning as
        # finance-secure - one route covers every profile-related query
        # param without needing a separate resource tree per param).
        if event.get('resource', '').startswith('/profile-secure'):
            return profile_view_enforced(event)

        if method == 'POST':
            # The original anonymous path - genuinely closed now, not left
            # as a guest fallback, since Epic 7 asked for real restriction
            # here rather than an additive stronger option.
            return _response(403, {'error': 'log in to record a match - use /record-match'})
        elif method == 'GET':
            return list_matches(event)
        elif method == 'PUT' and match_id:
            return update_match(match_id, event)
        elif method == 'DELETE' and match_id:
            return delete_match(match_id, event)
        return _response(404, {'error': 'not found'})
    except Exception as e:
        return _response(500, {'error': str(e)})


def reorder_matches(event):
    """Reorders a set of matches by reassigning their timestamps.

    The client sends match_ids in the desired order. We take the set of
    timestamps those matches currently hold, sort them, and hand them back
    out in the new order - so match now-first gets the earliest of the
    day's times, and so on. This keeps every timestamp within the same day
    (they're just permuted among that day's matches), then replays every
    rating from scratch in the corrected order.

    Only a SuperAdmin may do this: reordering silently rewrites every
    player's rating from the earliest changed match onward.
    """
    claims = _caller_claims(event)
    if not _is_super_admin(claims):
        return _response(403, {'error': 'only a SuperAdmin can reorder matches'})

    body = json.loads(event.get('body') or '{}')
    ordered_ids = body.get('match_ids') or []
    if len(ordered_ids) < 2:
        return _response(400, {'error': 'need at least two matches to reorder'})

    # Fetch exactly these matches.
    found = {}
    for mid in ordered_ids:
        item = matches_table.get_item(Key={'match_id': mid}).get('Item')
        if not item:
            return _response(404, {'error': f'match {mid} not found'})
        found[mid] = item

    # Guard: they must all be the same calendar day. Reordering across days
    # is almost never intended and is where months of ratings get nuked.
    days = {(found[mid].get('date') or '')[:10] for mid in ordered_ids}
    if len(days) > 1:
        return _response(400, {'error': 'all matches in a reorder must be from the same day'})

    # The pool of timestamps to redistribute, earliest first.
    timestamps = sorted(found[mid].get('date') for mid in ordered_ids)

    # Assign the earliest time to the match the admin put first, etc.
    for new_time, mid in zip(timestamps, ordered_ids):
        if found[mid].get('date') != new_time:
            matches_table.update_item(
                Key={'match_id': mid},
                UpdateExpression='SET #d = :d',
                ExpressionAttributeNames={'#d': 'date'},
                ExpressionAttributeValues={':d': new_time}
            )

    recompute_all_ratings()
    return _response(200, {'reordered': len(ordered_ids)})


def record_match(event):
    body = json.loads(event.get('body') or '{}')
    match_type = body.get('match_type', 'singles')
    team_a = body.get('team_a') or []
    team_b = body.get('team_b') or []
    score_a = body.get('score_a')
    score_b = body.get('score_b')
    group_id = body.get('group_id')
    point_log = body.get('point_log')
    points_to_win = body.get('points_to_win', 21)

    if match_type not in ('singles', 'doubles'):
        return _response(400, {'error': 'match_type must be singles or doubles'})
    expected_size = 1 if match_type == 'singles' else 2
    if len(team_a) != expected_size or len(team_b) != expected_size:
        return _response(400, {'error': f'{match_type} requires {expected_size} player(s) per team'})
    if score_a is None or score_b is None:
        return _response(400, {'error': 'score_a and score_b are required'})
    if set(team_a) & set(team_b):
        return _response(400, {'error': 'a player cannot be on both teams'})
    if not _is_valid_completed_game(int(score_a), int(score_b), int(points_to_win)):
        cap = int(points_to_win) + 9
        return _response(400, {
            'error': f'invalid final score: game must be won by 2 at {points_to_win}+ points, or reach the hard cap of {cap}'
        })

    if point_log is not None:
        if not isinstance(point_log, list) or any(p not in ('A', 'B') for p in point_log):
            return _response(400, {'error': 'point_log must be a list of "A"/"B" entries'})
        log_a = sum(1 for p in point_log if p == 'A')
        log_b = sum(1 for p in point_log if p == 'B')
        if log_a != int(score_a) or log_b != int(score_b):
            return _response(400, {'error': 'point_log does not match score_a/score_b totals'})

    item = _play_and_log(match_type, team_a, team_b, int(score_a), int(score_b), group_id, None, None,
                          point_log, int(points_to_win))
    if item is None:
        return _response(404, {'error': 'one or more players not found'})
    return _response(200, item)


def update_match(match_id, event):
    """Fix a mis-entered score on an already-recorded standalone match.
    Requires the confirmation code, same as deletions and renames. Since
    Elo is path-dependent, changing a score doesn't just affect this one
    match's own rating delta - it can shift everyone who played after it
    too - so every edit triggers a full recompute of every player's rating
    from scratch, replaying the corrected history in order."""
    body = json.loads(event.get('body') or '{}')
    if body.get('confirm') != CONFIRMATION_CODE:
        return _response(400, {'error': 'confirmation code is missing or incorrect'})

    existing = matches_table.get_item(Key={'match_id': match_id}).get('Item')
    if not existing:
        return _response(404, {'error': 'match not found'})

    new_score_a = body.get('score_a')
    new_score_b = body.get('score_b')
    if new_score_a is None or new_score_b is None:
        return _response(400, {'error': 'score_a and score_b are required'})
    new_score_a, new_score_b = int(new_score_a), int(new_score_b)
    if new_score_a == new_score_b:
        return _response(400, {'error': 'scores cannot be tied'})

    new_winner = 'A' if new_score_a > new_score_b else 'B'

    matches_table.update_item(
        Key={'match_id': match_id},
        UpdateExpression='SET score_a = :sa, score_b = :sb, winner = :w',
        ExpressionAttributeValues={':sa': new_score_a, ':sb': new_score_b, ':w': new_winner}
    )

    recompute_all_ratings()

    updated = matches_table.get_item(Key={'match_id': match_id}).get('Item')
    return _response(200, {'match': updated, 'note': 'All player ratings were recomputed from the corrected match history.'})


def delete_match(match_id, event):
    """Permanently delete a mis-recorded match - e.g. the wrong player was
    selected entirely and a corrected match was recorded separately.
    Requires the confirmation code, same as score corrections. Since Elo
    is path-dependent, deleting a match doesn't just undo its own rating
    delta - it can shift every match that happened after it too - so
    deletion triggers the same full recompute as a score correction."""
    body = json.loads(event.get('body') or '{}')
    if body.get('confirm') != CONFIRMATION_CODE:
        return _response(400, {'error': 'confirmation code is missing or incorrect'})

    existing = matches_table.get_item(Key={'match_id': match_id}).get('Item')
    if not existing:
        return _response(404, {'error': 'match not found'})

    matches_table.delete_item(Key={'match_id': match_id})
    recompute_all_ratings()
    return _response(200, {'deleted': True, 'match_id': match_id,
                            'note': 'All player ratings were recomputed from the remaining match history.'})


def recompute_all_ratings():
    """Elo is path-dependent - each match's rating change depends on the
    ratings at that exact moment, which depend on everything before it.
    After correcting a match, the only fully correct fix is to reset
    everyone to 1000 and replay every match in chronological order,
    recomputing from scratch - including replaying each pairing's K-factor
    exactly as it would have been at that point in time."""
    players = players_table.scan().get('Items', [])
    current_ratings = {p['player_id']: 1000.0 for p in players}
    pairing_counts = {}  # frozenset({p1,p2}) -> matches played together so far

    matches = matches_table.scan().get('Items', [])
    matches.sort(key=lambda m: m.get('date', ''))

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

        # The rating history graph reads ratings_after directly off each
        # match record - if we don't write the corrected values back here,
        # a correction fixes everyone's current rating but leaves the
        # historical trail permanently showing the old, wrong numbers.
        new_ratings_after = {pid: int(round(current_ratings[pid])) for pid in team_a + team_b}
        if m.get('ratings_after') != new_ratings_after:
            matches_table.update_item(
                Key={'match_id': m['match_id']},
                UpdateExpression='SET ratings_after = :r',
                ExpressionAttributeValues={':r': new_ratings_after}
            )

    for pid, rating in current_ratings.items():
        players_table.update_item(Key={'player_id': pid}, UpdateExpression='SET rating = :r',
                                   ExpressionAttributeValues={':r': int(round(rating))})


def compute_momentum_stats(point_log, winner):
    """Longest scoring streak per team, and how big a deficit the winner overcame."""
    if not point_log:
        return {}

    longest_streak = {'A': 0, 'B': 0}
    current_streak = {'A': 0, 'B': 0}
    running = {'A': 0, 'B': 0}
    worst_deficit_for_winner = 0

    scoring_runs = 0
    prev_point = None
    for point in point_log:
        other = 'B' if point == 'A' else 'A'
        if point != prev_point:
            scoring_runs += 1
            prev_point = point
        current_streak[point] += 1
        current_streak[other] = 0
        longest_streak[point] = max(longest_streak[point], current_streak[point])
        running[point] += 1

        if winner in ('A', 'B'):
            deficit = running[other] - running[winner]
            if deficit > worst_deficit_for_winner:
                worst_deficit_for_winner = deficit

    # Guard against the live counter being misused to batch-enter a final
    # score (e.g. tapping 21 points for one side, then 19 for the other).
    # A real ~40-point badminton game changes scorer dozens of times; a
    # batch entry produces 1-3 unbroken runs. Such a log is not a genuine
    # point-by-point record, so it must not fabricate a "comeback" (which
    # would both pollute the Hall of Fame and grant an undeserved Elo
    # bonus to the winner).
    suspected_batch_entry = len(point_log) >= 20 and scoring_runs <= 3

    result = {
        'longest_streak_a': longest_streak['A'],
        'longest_streak_b': longest_streak['B'],
        'winner_overcame_deficit': (worst_deficit_for_winner
                                     if winner in ('A', 'B') and not suspected_batch_entry else 0)
    }
    if suspected_batch_entry:
        result['suspected_batch_entry'] = True
    return result


def compute_adaptive_k(pairing_count):
    """Higher K for a fresh/novel doubles pairing (each match together is
    high-information, since we don't yet know how these two specific
    players perform as a unit). Lower K once a pairing is well-established
    (each additional match together adds little new information, and this
    is what keeps a fixed partnership's ratings from swinging wildly in
    lockstep every single time they play). Singles has no pairing concept,
    so it always uses the flat K_FACTOR."""
    if pairing_count == 0:
        return 40
    elif pairing_count < 5:
        return K_FACTOR
    else:
        return 20


def get_pairing_count(team_ids, exclude_match_id=None):
    """How many prior doubles matches has this exact 2-player team played
    together, based on matches already recorded (regardless of opponent)."""
    if len(team_ids) != 2:
        return 0
    pair_key = frozenset(team_ids)
    count = 0
    items = matches_table.scan().get('Items', [])
    for m in items:
        if exclude_match_id and m.get('match_id') == exclude_match_id:
            continue
        if m.get('match_type') != 'doubles':
            continue
        for team in (m.get('team_a') or [], m.get('team_b') or []):
            if len(team) == 2 and frozenset(team) == pair_key:
                count += 1
                break
    return count


def _play_and_log(match_type, team_a_ids, team_b_ids, score_a, score_b, group_id, tournament_id, stage,
                   point_log=None, points_to_win=21):
    team_a_players = [players_table.get_item(Key={'player_id': pid}).get('Item') for pid in team_a_ids]
    team_b_players = [players_table.get_item(Key={'player_id': pid}).get('Item') for pid in team_b_ids]
    if any(p is None for p in team_a_players) or any(p is None for p in team_b_players):
        return None

    rating_a_avg = sum(float(p.get('rating', 1000)) for p in team_a_players) / len(team_a_players)
    rating_b_avg = sum(float(p.get('rating', 1000)) for p in team_b_players) / len(team_b_players)

    actual_a = 1.0 if score_a > score_b else (0.0 if score_a < score_b else 0.5)
    actual_b = 1.0 - actual_a

    expected_a = 1 / (1 + 10 ** ((rating_b_avg - rating_a_avg) / 400))
    expected_b = 1 - expected_a

    if match_type == 'doubles':
        k_a = compute_adaptive_k(get_pairing_count(team_a_ids))
        k_b = compute_adaptive_k(get_pairing_count(team_b_ids))
    else:
        k_a = k_b = K_FACTOR

    delta_a = k_a * (actual_a - expected_a)
    delta_b = k_b * (actual_b - expected_b)

    winner = 'A' if score_a > score_b else ('B' if score_b > score_a else 'tie')

    momentum = None
    if point_log:
        momentum = compute_momentum_stats(point_log, winner)
        bonus = compute_comeback_bonus(momentum)
        if winner == 'A':
            delta_a += bonus
        elif winner == 'B':
            delta_b += bonus

    new_ratings = {}
    for p in team_a_players:
        new_ratings[p['player_id']] = int(round(float(p.get('rating', 1000)) + delta_a))
    for p in team_b_players:
        new_ratings[p['player_id']] = int(round(float(p.get('rating', 1000)) + delta_b))

    for pid, new_rating in new_ratings.items():
        players_table.update_item(Key={'player_id': pid}, UpdateExpression='SET rating = :r',
                                   ExpressionAttributeValues={':r': new_rating})

    item = {
        'match_id': str(uuid.uuid4()),
        'date': datetime.now(timezone.utc).isoformat(),
        'match_type': match_type,
        'team_a': team_a_ids,
        'team_b': team_b_ids,
        'team_a_names': [p['name'] for p in team_a_players],
        'team_b_names': [p['name'] for p in team_b_players],
        'score_a': score_a,
        'score_b': score_b,
        'points_to_win': points_to_win,
        'winner': winner,
        'ratings_after': new_ratings,
    }
    if group_id:
        item['group_id'] = group_id
    if tournament_id:
        item['tournament_id'] = tournament_id
        item['stage'] = stage
    if point_log:
        item['point_log'] = point_log
        item['momentum'] = momentum

    matches_table.put_item(Item=item)
    return item


def list_matches(event):
    params = event.get('queryStringParameters') or {}
    group_id = params.get('group_id')
    player_id = params.get('player_id')
    date_from = params.get('date_from')  # 'YYYY-MM-DD'
    date_to = params.get('date_to')      # 'YYYY-MM-DD'
    partnerships_for = params.get('partnerships_for')
    attendance = params.get('attendance')
    hall_of_fame = params.get('hall_of_fame')
    radar_for = params.get('radar_for')
    top_n = int(params.get('top_n', 10))
    tournament_filter = params.get('tournament_filter', 'include')  # 'include' | 'exclude'

    items = matches_table.scan().get('Items', [])

    if partnerships_for or radar_for:
        scoped_items = items
        if group_id:
            scoped_items = [i for i in scoped_items if i.get('group_id') == group_id]
        if tournament_filter == 'exclude':
            scoped_items = [i for i in scoped_items if not i.get('tournament_id')]
        if partnerships_for:
            return _response(200, compute_partnerships(partnerships_for, scoped_items))
        return _response(200, compute_partner_distribution(radar_for, scoped_items, top_n))
    if attendance:
        return _response(200, compute_attendance(items, group_id))
    if hall_of_fame:
        return _response(200, compute_hall_of_fame(items, group_id))
    if params.get('diversity'):
        return _response(200, compute_diversity(items, group_id))
    if params.get('progress_badges'):
        return _response(200, compute_progress_badges(items, group_id))
    if params.get('achievements_for'):
        all_tournaments = tournaments_table.scan().get('Items', [])
        return _response(200, compute_achievements(params.get('achievements_for'), items, all_tournaments))
    if params.get('profile_bundle_for'):
        player_id = params.get('profile_bundle_for')
        all_tournaments = tournaments_table.scan().get('Items', [])
        return _response(200, {
            'hall_of_fame': compute_hall_of_fame(items),
            'progress_badges': compute_progress_badges(items),
            'achievements': compute_achievements(player_id, items, all_tournaments),
            'recent_form': compute_recent_form(player_id, items, 10),
            'overall_record': compute_overall_record(player_id, items),
            'top_opponents': compute_top_opponents(player_id, items, 15),
            'attendance': compute_attendance(items)['attendance'],
        })
    if params.get('progress_history'):
        scope = params.get('scope', 'global')
        period = params.get('period', 'week')
        return _response(200, compute_progress_history_summary(scope, period))
    if params.get('head_to_head') and params.get('opponent'):
        return _response(200, compute_head_to_head(params.get('head_to_head'), params.get('opponent'), items))
    if params.get('recent_form'):
        limit = int(params.get('limit', 10))
        return _response(200, compute_recent_form(params.get('recent_form'), items, limit))
    if params.get('top_opponents_for'):
        top_n = int(params.get('top_n', 15))
        return _response(200, compute_top_opponents(params.get('top_opponents_for'), items, top_n))
    if params.get('overall_record_for'):
        return _response(200, compute_overall_record(params.get('overall_record_for'), items))

    if group_id:
        items = [i for i in items if i.get('group_id') == group_id]
    if player_id:
        items = [i for i in items if player_id in (i.get('team_a') or []) or player_id in (i.get('team_b') or [])]
    if date_from:
        items = [i for i in items if i.get('date', '') >= date_from]
    if date_to:
        # date_to is a calendar day - include the whole day (matches are stored in UTC)
        items = [i for i in items if i.get('date', '') <= date_to + 'T23:59:59.999999+00:00']

    items.sort(key=lambda i: i.get('date', ''), reverse=True)

    return _response(200, {'matches': items})


def compute_partnerships(player_id, items):
    """For a given player, tally win/loss record with each doubles partner
    they've played alongside. This is a straightforward performance
    breakdown (not a full statistical synergy score against individual
    rating expectations, which would need historical pre-match ratings we
    don't currently snapshot)."""
    partner_stats = {}
    for m in items:
        if m.get('match_type') != 'doubles':
            continue
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        if player_id in team_a and len(team_a) == 2:
            team, won = team_a, (m.get('winner') == 'A')
        elif player_id in team_b and len(team_b) == 2:
            team, won = team_b, (m.get('winner') == 'B')
        else:
            continue

        partner_id = next((pid for pid in team if pid != player_id), None)
        if not partner_id:
            continue
        if partner_id not in partner_stats:
            partner_stats[partner_id] = {'partner_id': partner_id, 'matches': 0, 'wins': 0, 'losses': 0}
        stats = partner_stats[partner_id]
        stats['matches'] += 1
        if won:
            stats['wins'] += 1
        elif m.get('winner') in ('A', 'B'):
            stats['losses'] += 1

    result = []
    for pid, stats in partner_stats.items():
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        stats['partner_name'] = display_name(p, pid)
        stats['win_rate'] = round(stats['wins'] / stats['matches'] * 100, 1) if stats['matches'] else 0
        result.append(stats)

    result.sort(key=lambda s: -s['matches'])
    return {'player_id': player_id, 'partnerships': result}


def get_group_member_ids(group_id):
    """The set of player_ids belonging to a group, used to filter WHO shows
    up in a stat's results - not to restrict WHICH matches count. A group
    filter means 'show me these people's numbers', computed from their
    full match history regardless of whether any individual match happened
    to be tagged with this group at recording time (tagging is optional
    and many matches are never tagged at all)."""
    group = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    return set(group.get('member_ids', [])) if group else set()


def compute_attendance(items, group_id_filter=None):
    """Per-player attendance/consistency: total matches, distinct calendar
    dates played (a proxy for 'sessions attended'), recent activity
    windows, and their longest run of consecutive weeks with at least one
    match. Optionally scoped to a group - this shows only that group's
    members, but each member's own numbers are computed from their FULL
    match history (including standalone matches never tagged with any
    group), not just matches tagged with this specific group."""
    member_ids = get_group_member_ids(group_id_filter) if group_id_filter else None

    now = datetime.now(timezone.utc)
    player_stats = {}
    for m in items:
        date_str = m.get('date')
        if not date_str:
            continue
        try:
            match_date = datetime.fromisoformat(date_str)
        except ValueError:
            continue
        days_ago = (now - match_date).days
        day_key = date_str[:10]
        # A stable, sortable week index where consecutive calendar weeks
        # differ by exactly 1, avoiding manual year-boundary handling.
        iso_year, iso_week, _ = match_date.isocalendar()
        week_index = date.fromisocalendar(iso_year, iso_week, 1).toordinal() // 7

        for pid in (m.get('team_a') or []) + (m.get('team_b') or []):
            if pid not in player_stats:
                player_stats[pid] = {'player_id': pid, 'total_matches': 0, 'session_dates': set(),
                                      'last_30_days': 0, 'last_90_days': 0, 'week_indices': set()}
            s = player_stats[pid]
            s['total_matches'] += 1
            s['session_dates'].add(day_key)
            s['week_indices'].add(week_index)
            if days_ago <= 30:
                s['last_30_days'] += 1
            if days_ago <= 90:
                s['last_90_days'] += 1

    result = []
    for pid, s in player_stats.items():
        if member_ids is not None and pid not in member_ids:
            continue
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        weeks_sorted = sorted(s['week_indices'])
        best_streak = 1 if weeks_sorted else 0
        current_run = 1
        for i in range(1, len(weeks_sorted)):
            if weeks_sorted[i] == weeks_sorted[i - 1] + 1:
                current_run += 1
                best_streak = max(best_streak, current_run)
            else:
                current_run = 1
        result.append({
            'player_id': pid,
            'name': display_name(p, pid),
            'total_matches': s['total_matches'],
            'sessions_attended': len(s['session_dates']),
            'matches_last_30_days': s['last_30_days'],
            'matches_last_90_days': s['last_90_days'],
            'longest_week_streak': best_streak,
        })

    result.sort(key=lambda r: -r['sessions_attended'])
    return {'attendance': result}


def compute_hall_of_fame(items, group_id_filter=None):
    """Highlight stats computed from full chronological match history:
    longest win streak, biggest blowout, peak rating ever per player,
    biggest upsets (giant-killer), best comebacks (only available for
    matches recorded via the live point-by-point counter), rating
    consistency/volatility, singles-vs-doubles specialization, and
    group-stage-vs-knockout performance (deep-run rate).

    Names are resolved from the current Players table at the very end,
    not from the names frozen onto each match record at the time it was
    played - a rename should show up everywhere immediately, rather than
    only affecting matches recorded after the rename happened.

    Optionally scoped to a group: this filters WHO shows up in the
    results to that group's members, but every computation still uses
    each player's FULL match history (including standalone matches never
    tagged with any group) - a group filter means 'show me these
    people's numbers', not 'only count matches tagged with this group'."""
    member_ids = get_group_member_ids(group_id_filter) if group_id_filter else None

    matches = sorted(items, key=lambda m: m.get('date', ''))

    name_cache = {}

    def resolve_name(pid, fallback=None):
        if pid not in name_cache:
            p = players_table.get_item(Key={'player_id': pid}).get('Item')
            name_cache[pid] = display_name(p, fallback or pid)
        return name_cache[pid]

    rolling_ratings = {}
    current_streak = {}
    personal_best_streaks = {}  # pid -> their own best-ever streak
    peak_rating = {}          # pid -> rating
    blowout_candidates = []   # every match's blowout info, filtered at output time
    giant_killer_candidates = []  # will hold winner_ids/loser_ids
    comeback_candidates = []      # will hold winner_ids
    player_deltas = {}       # pid -> [deltas]
    format_stats = {}        # pid -> {'singles_w','singles_l','doubles_w','doubles_l'}
    stage_stats = {}         # pid -> {'group_w','group_l','knockout_w','knockout_l'}
    tournament_stage_sets = {}  # pid -> {'group_tournaments': set(), 'knockout_tournaments': set()}
    pair_stats = {}          # frozenset(pair) -> {'wins','losses','members'}
    deuce_wins = {}          # pid -> wins by exactly 2 points past the target
    session_record = {}      # (pid, yyyy-mm-dd) -> {'wins','losses'}
    session_deltas = {}      # yyyy-mm-dd -> {pid: total rating delta that day}

    def team_avg(team_ids):
        return sum(rolling_ratings.get(pid, 1000.0) for pid in team_ids) / len(team_ids) if team_ids else 1000.0

    for m in matches:
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        team_a_names = m.get('team_a_names') or []
        team_b_names = m.get('team_b_names') or []
        score_a = m.get('score_a')
        score_b = m.get('score_b')
        winner = m.get('winner')
        match_type = m.get('match_type')
        stage = m.get('stage')
        tournament_id = m.get('tournament_id')
        if not team_a or not team_b or score_a is None or score_b is None:
            continue
        score_a, score_b = float(score_a), float(score_b)

        pre_a = team_avg(team_a)
        pre_b = team_avg(team_b)
        pre_individual = {pid: rolling_ratings.get(pid, 1000.0) for pid in team_a + team_b}

        if winner in ('A', 'B'):
            winners = team_a if winner == 'A' else team_b
            losers = team_b if winner == 'A' else team_a
            pre_winner = pre_a if winner == 'A' else pre_b
            pre_loser = pre_b if winner == 'A' else pre_a

            for pid in winners:
                current_streak[pid] = current_streak.get(pid, 0) + 1
                if current_streak[pid] > personal_best_streaks.get(pid, 0):
                    personal_best_streaks[pid] = current_streak[pid]
            for pid in losers:
                current_streak[pid] = 0

            day = (m.get('date') or '')[:10]
            for pid in winners + losers:
                rec = session_record.setdefault((pid, day), {'wins': 0, 'losses': 0})
                rec['wins' if pid in winners else 'losses'] += 1

            # Doubles chemistry: per fixed pair, wins/losses together.
            if match_type == 'doubles':
                for side, side_won in ((team_a, winner == 'A'), (team_b, winner == 'B')):
                    if len(side) == 2:
                        key = frozenset(side)
                        ps = pair_stats.setdefault(key, {'wins': 0, 'losses': 0, 'members': sorted(side)})
                        ps['wins' if side_won else 'losses'] += 1

            # Deuce specialist: won by exactly 2, past the normal target
            # (i.e. the game went to deuce and they closed it out).
            win_score, lose_score = max(score_a, score_b), min(score_a, score_b)
            if win_score - lose_score == 2 and win_score >= 22:
                for pid in winners:
                    deuce_wins[pid] = deuce_wins.get(pid, 0) + 1

            upset_gap = pre_loser - pre_winner
            if upset_gap > 0:
                giant_killer_candidates.append({
                    'winner_ids': winners, 'loser_ids': losers,
                    'upset_gap': round(upset_gap, 1), 'date': m.get('date'),
                    'score': f"{int(score_a)}-{int(score_b)}" if winner == 'A' else f"{int(score_b)}-{int(score_a)}"
                })

            for pid in team_a + team_b:
                won = pid in winners
                if match_type in ('singles', 'doubles'):
                    fs = format_stats.setdefault(pid, {'singles_w': 0, 'singles_l': 0, 'doubles_w': 0, 'doubles_l': 0})
                    key = 'singles' if match_type == 'singles' else 'doubles'
                    fs[f'{key}_w' if won else f'{key}_l'] += 1
                if stage in ('group', 'knockout'):
                    ss = stage_stats.setdefault(pid, {'group_w': 0, 'group_l': 0, 'knockout_w': 0, 'knockout_l': 0})
                    ss[f'{stage}_w' if won else f'{stage}_l'] += 1

        if stage in ('group', 'knockout') and tournament_id:
            for pid in team_a + team_b:
                sets = tournament_stage_sets.setdefault(pid, {'group_tournaments': set(), 'knockout_tournaments': set()})
                sets[f'{stage}_tournaments'].add(tournament_id)

        margin = abs(score_a - score_b)
        winning_side_ids = team_a if score_a > score_b else team_b
        blowout_candidates.append({
            'team_a_ids': team_a, 'team_b_ids': team_b,
            'winning_side_ids': winning_side_ids,
            'score_a': int(score_a), 'score_b': int(score_b),
            'margin': int(margin), 'date': m.get('date')
        })

        momentum = m.get('momentum')
        if momentum and momentum.get('winner_overcame_deficit', 0) > 0:
            winners = team_a if winner == 'A' else team_b
            comeback_candidates.append({
                'winner_ids': winners, 'deficit_overcome': int(momentum['winner_overcame_deficit']),
                'date': m.get('date')
            })

        ratings_after = m.get('ratings_after') or {}
        day = (m.get('date') or '')[:10]
        for pid, rating in ratings_after.items():
            rating = float(rating)
            delta = rating - pre_individual.get(pid, 1000.0)
            player_deltas.setdefault(pid, []).append(delta)
            session_deltas.setdefault(day, {})[pid] = session_deltas.setdefault(day, {}).get(pid, 0) + delta

            rolling_ratings[pid] = rating
            if pid not in peak_rating or rating > peak_rating[pid]:
                peak_rating[pid] = rating

    giant_killer_candidates.sort(key=lambda g: -g['upset_gap'])
    comeback_candidates.sort(key=lambda c: -c['deficit_overcome'])

    # Group filtering happens here, at output time, not by restricting
    # which matches got processed above - every calculation above already
    # used each player's FULL history. This just decides which rows are
    # relevant to show for this group.
    def in_group(pid):
        return member_ids is None or pid in member_ids

    def side_in_group(ids):
        return member_ids is None or all(pid in member_ids for pid in ids)

    giant_killer_candidates = [g for g in giant_killer_candidates if side_in_group(g['winner_ids'])]
    comeback_candidates = [c for c in comeback_candidates if side_in_group(c['winner_ids'])]
    blowout_candidates = [b for b in blowout_candidates if side_in_group(b['winning_side_ids'])]
    biggest_blowout = max(blowout_candidates, key=lambda b: b['margin']) if blowout_candidates else None

    eligible_streaks = {pid: s for pid, s in personal_best_streaks.items() if in_group(pid)}
    best_streak = None
    if eligible_streaks:
        top_pid = max(eligible_streaks.items(), key=lambda kv: kv[1])[0]
        best_streak = {'player_id': top_pid, 'streak': eligible_streaks[top_pid]}

    # Consistency / volatility - population standard deviation of rating deltas, min 3 matches
    consistency_rows = []
    for pid, deltas in player_deltas.items():
        if len(deltas) < 3 or not in_group(pid):
            continue
        mean_delta = sum(deltas) / len(deltas)
        variance = sum((d - mean_delta) ** 2 for d in deltas) / len(deltas)
        stdev = round(variance ** 0.5, 1)
        consistency_rows.append({'player_id': pid, 'matches': len(deltas), 'volatility': stdev})
    consistency_rows.sort(key=lambda r: r['volatility'])
    most_consistent = consistency_rows[:5]
    most_volatile = sorted(consistency_rows, key=lambda r: -r['volatility'])[:5]

    # Format specialist - biggest gap between singles and doubles win rate, min 2 matches in each
    format_rows = []
    for pid, fs in format_stats.items():
        if not in_group(pid):
            continue
        singles_total = fs['singles_w'] + fs['singles_l']
        doubles_total = fs['doubles_w'] + fs['doubles_l']
        if singles_total < 2 or doubles_total < 2:
            continue
        singles_pct = round(fs['singles_w'] / singles_total * 100, 1)
        doubles_pct = round(fs['doubles_w'] / doubles_total * 100, 1)
        format_rows.append({
            'player_id': pid,
            'singles_win_pct': singles_pct, 'doubles_win_pct': doubles_pct,
            'gap': round(abs(singles_pct - doubles_pct), 1),
            'stronger_format': 'singles' if singles_pct > doubles_pct else 'doubles'
        })
    format_rows.sort(key=lambda r: -r['gap'])

    # Deep-run rate - fraction of tournament appearances that included a knockout-stage match
    deep_run_rows = []
    for pid, sets in tournament_stage_sets.items():
        if not in_group(pid):
            continue
        all_tournaments = sets['group_tournaments'] | sets['knockout_tournaments']
        if not all_tournaments:
            continue
        rate = round(len(sets['knockout_tournaments']) / len(all_tournaments) * 100, 1)
        deep_run_rows.append({
            'player_id': pid,
            'tournaments_entered': len(all_tournaments),
            'reached_knockout': len(sets['knockout_tournaments']),
            'deep_run_rate': rate
        })
    deep_run_rows.sort(key=lambda r: -r['deep_run_rate'])

    # Best partnerships - doubles pairs by win rate, minimum 3 matches together
    partnership_rows = []
    for key, ps in pair_stats.items():
        total = ps['wins'] + ps['losses']
        if total < 3 or not side_in_group(ps['members']):
            continue
        partnership_rows.append({
            'member_ids': ps['members'], 'matches': total, 'wins': ps['wins'],
            'losses': ps['losses'], 'win_pct': round(ps['wins'] / total * 100, 1)
        })
    partnership_rows.sort(key=lambda r: (-r['win_pct'], -r['matches']))

    # Deuce specialists - most wins by exactly 2 past the target
    deuce_rows = sorted(
        ({'player_id': pid, 'deuce_wins': n} for pid, n in deuce_wins.items() if in_group(pid)),
        key=lambda r: -r['deuce_wins'])

    # Undefeated sessions - days with 3+ matches and zero losses
    undefeated_counts = {}
    for (pid, day), rec in session_record.items():
        if rec['wins'] >= 3 and rec['losses'] == 0:
            undefeated_counts[pid] = undefeated_counts.get(pid, 0) + 1
    undefeated_rows = sorted(
        ({'player_id': pid, 'sessions': n} for pid, n in undefeated_counts.items() if in_group(pid)),
        key=lambda r: -r['sessions'])

    # Biggest single-match rating swing (positive) per player
    swing_rows = sorted(
        ({'player_id': pid, 'swing': round(max(deltas), 1)}
         for pid, deltas in player_deltas.items() if deltas and max(deltas) > 0 and in_group(pid)),
        key=lambda r: -r['swing'])

    # Session MVP - best total rating delta on each play date (co-winners on ties)
    session_mvp_rows = []
    mvp_counts = {}
    for day in sorted(session_deltas, reverse=True):
        eligible = {pid: d for pid, d in session_deltas[day].items() if in_group(pid)}
        if not eligible:
            continue
        best = max(eligible.values())
        mvp_ids = sorted(pid for pid, d in eligible.items() if abs(d - best) < 0.01)
        for pid in mvp_ids:
            mvp_counts[pid] = mvp_counts.get(pid, 0) + 1
        session_mvp_rows.append({'date': day, 'player_ids': mvp_ids, 'delta': round(best, 1)})
    mvp_count_rows = sorted(({'player_id': pid, 'mvp_days': n} for pid, n in mvp_counts.items()),
                             key=lambda r: -r['mvp_days'])

    # Resolve every name from the current Players table, right here at the
    # end - this is the one place names get attached to the output.
    for row in consistency_rows:
        row['name'] = resolve_name(row['player_id'])
    for row in format_rows:
        row['name'] = resolve_name(row['player_id'])
    for row in deep_run_rows:
        row['name'] = resolve_name(row['player_id'])
    for g in giant_killer_candidates:
        g['winner_names'] = [resolve_name(pid) for pid in g['winner_ids']]
        g['loser_names'] = [resolve_name(pid) for pid in g['loser_ids']]
    for c in comeback_candidates:
        c['winner_names'] = [resolve_name(pid) for pid in c['winner_ids']]
    if biggest_blowout:
        biggest_blowout['team_a_names'] = [resolve_name(pid) for pid in biggest_blowout['team_a_ids']]
        biggest_blowout['team_b_names'] = [resolve_name(pid) for pid in biggest_blowout['team_b_ids']]
    if best_streak:
        best_streak['name'] = resolve_name(best_streak['player_id'])
    peak_ratings_list = [
        {'player_id': pid, 'name': resolve_name(pid), 'rating': int(round(rating))}
        for pid, rating in peak_rating.items() if in_group(pid)
    ]
    for row in partnership_rows:
        row['names'] = [resolve_name(pid) for pid in row['member_ids']]
    for row in deuce_rows + undefeated_rows + swing_rows + mvp_count_rows:
        row['name'] = resolve_name(row['player_id'])
    for row in session_mvp_rows:
        row['names'] = [resolve_name(pid) for pid in row['player_ids']]

    return {
        'longest_win_streak': best_streak,
        'biggest_blowout': biggest_blowout,
        'peak_ratings': sorted(peak_ratings_list, key=lambda p: -p['rating'])[:10],
        'giant_killer_top5': giant_killer_candidates[:5],
        'comeback_top5': comeback_candidates[:5],
        'most_consistent': most_consistent,
        'most_volatile': most_volatile,
        'format_specialists': format_rows[:5],
        'deep_run_rates': deep_run_rows[:10],
        'best_partnerships': partnership_rows[:5],
        'deuce_specialists': deuce_rows[:5],
        'undefeated_sessions': undefeated_rows[:5],
        'biggest_swings': swing_rows[:5],
        'session_mvps': session_mvp_rows[:10],
        'mvp_counts': mvp_count_rows[:5]
    }


def compute_achievements(player_id, matches, tournaments):
    """Milestone/tiered achievement progress for one player: total matches
    played, tournament championships won, and their own personal-best win
    streak (distinct from the single overall record-holder tracked in
    hall_of_fame - this is specifically about this player's own history)."""
    total_matches = sum(
        1 for m in matches
        if player_id in (m.get('team_a') or []) or player_id in (m.get('team_b') or [])
    )

    tournament_wins = 0
    runner_ups = 0
    third_places = 0
    for t in tournaments:
        if t.get('status') != 'completed':
            continue
        knockout = t.get('knockout') or {}
        rounds = knockout.get('rounds') or []
        if rounds and rounds[-1]:
            final_match = rounds[-1][0]
            winner_id = final_match.get('winner_id')
            if winner_id:
                player_a = final_match.get('player_a') or {}
                player_b = final_match.get('player_b') or {}
                winner_entity = player_a if winner_id == player_a.get('player_id') else player_b
                loser_entity = player_b if winner_id == player_a.get('player_id') else player_a
                if player_id in (winner_entity.get('members') or []):
                    tournament_wins += 1
                elif player_id in (loser_entity.get('members') or []):
                    runner_ups += 1
        third = knockout.get('third_place_match')
        if third and third.get('winner_id'):
            ta = third.get('player_a') or {}
            tb = third.get('player_b') or {}
            third_winner = ta if third['winner_id'] == ta.get('player_id') else tb
            if player_id in (third_winner.get('members') or []):
                third_places += 1

    player_matches = sorted(
        [m for m in matches if player_id in (m.get('team_a') or []) or player_id in (m.get('team_b') or [])],
        key=lambda m: m.get('date', '')
    )
    current_streak = 0
    best_streak = 0
    for m in player_matches:
        winner = m.get('winner')
        team_a = m.get('team_a') or []
        if winner not in ('A', 'B'):
            continue
        won = (winner == 'A' and player_id in team_a) or (winner == 'B' and player_id not in team_a)
        if won:
            current_streak += 1
            best_streak = max(best_streak, current_streak)
        else:
            current_streak = 0

    # Deuce wins, undefeated sessions, attendance streak, and peak rating -
    # all from this player's own match history.
    deuce_wins = 0
    session_record = {}   # yyyy-mm-dd -> {'wins','losses'}
    peak = 0
    for m in player_matches:
        winner = m.get('winner')
        team_a = m.get('team_a') or []
        score_a, score_b = m.get('score_a'), m.get('score_b')
        day = (m.get('date') or '')[:10]
        rating = (m.get('ratings_after') or {}).get(player_id)
        if rating is not None:
            peak = max(peak, int(round(float(rating))))
        if winner not in ('A', 'B') or score_a is None or score_b is None:
            continue
        won = (winner == 'A' and player_id in team_a) or (winner == 'B' and player_id not in team_a)
        rec = session_record.setdefault(day, {'wins': 0, 'losses': 0})
        rec['wins' if won else 'losses'] += 1
        win_score, lose_score = max(float(score_a), float(score_b)), min(float(score_a), float(score_b))
        if won and win_score - lose_score == 2 and win_score >= 22:
            deuce_wins += 1

    undefeated_sessions = sum(1 for rec in session_record.values()
                               if rec['wins'] >= 3 and rec['losses'] == 0)

    # Best attendance streak: longest run of consecutive CLUB session dates
    # (days when anyone played) on which this player also appeared.
    club_days = sorted({(m.get('date') or '')[:10] for m in matches if m.get('date')})
    attended = set(session_record.keys())
    best_attendance = run = 0
    for day in club_days:
        run = run + 1 if day in attended else 0
        best_attendance = max(best_attendance, run)

    return {
        'player_id': player_id,
        'total_matches': total_matches,
        'tournament_wins': tournament_wins,
        'runner_ups': runner_ups,
        'third_places': third_places,
        'podium_finishes': tournament_wins + runner_ups + third_places,
        'personal_best_streak': best_streak,
        'current_streak': current_streak,
        'deuce_wins': deuce_wins,
        'undefeated_sessions': undefeated_sessions,
        'best_attendance_streak': best_attendance,
        'peak_rating': peak
    }


def compute_top_opponents(player_id, matches, top_n=15):
    """This player's win/loss record against every opponent they've ever
    faced (singles or doubles, as an OPPONENT - not a teammate), ranked
    by how many times they've played each other, most-played first."""
    records = {}  # opponent_id -> {'wins': int, 'losses': int}
    for m in matches:
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        winner = m.get('winner')
        if winner not in ('A', 'B'):
            continue
        player_in_a = player_id in team_a
        player_in_b = player_id in team_b
        if not (player_in_a or player_in_b):
            continue
        opponents = team_b if player_in_a else team_a
        player_won = (winner == 'A' and player_in_a) or (winner == 'B' and player_in_b)
        for opp_id in opponents:
            if opp_id == player_id:
                continue
            rec = records.setdefault(opp_id, {'wins': 0, 'losses': 0})
            if player_won:
                rec['wins'] += 1
            else:
                rec['losses'] += 1

    rows = []
    for opp_id, rec in records.items():
        p = players_table.get_item(Key={'player_id': opp_id}).get('Item')
        total = rec['wins'] + rec['losses']
        rows.append({
            'opponent_id': opp_id,
            'opponent_name': display_name(p, opp_id),
            'matches': total,
            'wins': rec['wins'],
            'losses': rec['losses'],
            'win_rate': round(rec['wins'] / total * 100, 1) if total else 0
        })
    rows.sort(key=lambda r: -r['matches'])
    return {'player_id': player_id, 'opponents': rows[:top_n]}


def compute_overall_record(player_id, matches):
    """This player's total win/loss record, split by singles and doubles."""
    record = {'singles_wins': 0, 'singles_losses': 0, 'doubles_wins': 0, 'doubles_losses': 0}
    for m in matches:
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        winner = m.get('winner')
        match_type = m.get('match_type')
        if winner not in ('A', 'B') or match_type not in ('singles', 'doubles'):
            continue
        if player_id in team_a:
            won = winner == 'A'
        elif player_id in team_b:
            won = winner == 'B'
        else:
            continue
        key = 'singles' if match_type == 'singles' else 'doubles'
        record[f'{key}_wins' if won else f'{key}_losses'] += 1

    total_wins = record['singles_wins'] + record['doubles_wins']
    total_losses = record['singles_losses'] + record['doubles_losses']
    return {
        'player_id': player_id,
        'total_wins': total_wins, 'total_losses': total_losses,
        'singles_wins': record['singles_wins'], 'singles_losses': record['singles_losses'],
        'doubles_wins': record['doubles_wins'], 'doubles_losses': record['doubles_losses']
    }


def compute_head_to_head(player_id, opponent_id, matches):
    """One player's win/loss record specifically as an OPPONENT of another
    player - distinct from partnerships, which only covers doubles
    teammates. Counts any match (singles or doubles) where the two were
    on opposite teams."""
    wins = 0
    losses = 0
    for m in matches:
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        winner = m.get('winner')
        if winner not in ('A', 'B'):
            continue
        player_in_a = player_id in team_a
        player_in_b = player_id in team_b
        opponent_in_a = opponent_id in team_a
        opponent_in_b = opponent_id in team_b
        if not ((player_in_a and opponent_in_b) or (player_in_b and opponent_in_a)):
            continue  # not opposing teams in this match (or one/both absent)
        player_won = (winner == 'A' and player_in_a) or (winner == 'B' and player_in_b)
        if player_won:
            wins += 1
        else:
            losses += 1
    total = wins + losses
    return {
        'player_id': player_id, 'opponent_id': opponent_id,
        'matches': total, 'wins': wins, 'losses': losses,
        'win_rate': round(wins / total * 100, 1) if total else 0
    }


def compute_recent_form(player_id, matches, limit=10):
    """A player's last N matches, in chronological order (oldest to
    newest) so a left-to-right rendering naturally puts the most recent
    result on the right."""
    player_matches = sorted(
        [m for m in matches if player_id in (m.get('team_a') or []) or player_id in (m.get('team_b') or [])],
        key=lambda m: m.get('date', '')
    )
    recent = player_matches[-limit:]
    form = []
    for m in recent:
        winner = m.get('winner')
        team_a = m.get('team_a') or []
        if winner not in ('A', 'B'):
            continue
        won = (winner == 'A' and player_id in team_a) or (winner == 'B' and player_id not in team_a)
        opponent_names = m.get('team_b_names') if player_id in team_a else m.get('team_a_names')
        form.append({
            'date': m.get('date'),
            'result': 'W' if won else 'L',
            'opponent_names': opponent_names or []
        })
    return {'player_id': player_id, 'form': form}


def compute_diversity(items, group_id_filter=None):
    """For every player: how concentrated their doubles partnerships are.
    'top_partner_pct' is the share of their matches played with their single
    most frequent partner - a simple, intuitive stand-in for 'how entangled
    is this rating with one fixed pairing'. Sorted with the most
    concentrated (least-mixed) players first. Optionally scoped to a
    group - shows only that group's members, but each member's own
    partner-concentration is computed from their full history."""
    member_ids = get_group_member_ids(group_id_filter) if group_id_filter else None

    partner_counts = {}  # player_id -> {partner_id: count}
    for m in items:
        if m.get('match_type') != 'doubles':
            continue
        for team in (m.get('team_a') or [], m.get('team_b') or []):
            if len(team) != 2:
                continue
            p1, p2 = team
            partner_counts.setdefault(p1, {}).setdefault(p2, 0)
            partner_counts[p1][p2] += 1
            partner_counts.setdefault(p2, {}).setdefault(p1, 0)
            partner_counts[p2][p1] += 1

    result = []
    for pid, counts in partner_counts.items():
        if member_ids is not None and pid not in member_ids:
            continue
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        total = sum(counts.values())
        top_partner_id, top_count = max(counts.items(), key=lambda kv: kv[1])
        top_partner = players_table.get_item(Key={'player_id': top_partner_id}).get('Item')
        result.append({
            'player_id': pid,
            'name': display_name(p, pid),
            'total_matches': total,
            'distinct_partners': len(counts),
            'top_partner_id': top_partner_id,
            'top_partner_name': display_name(top_partner, top_partner_id),
            'top_partner_pct': round(top_count / total * 100, 1) if total else 0
        })

    result.sort(key=lambda r: -r['top_partner_pct'])
    return {'players': result}


def compute_progress_history_summary(scope_label, period_name):
    """Reads the permanent, locked-in weekly/monthly/yearly winner history
    for one scope (global or group_{id}) and one period type, computing
    streaks (consecutive periods won in a row) and lifetime holder counts
    for 'most improved' - the gamified badges built on top of history that
    can never be recomputed retroactively once it's been overwritten."""
    items = history_table.scan().get('Items', [])
    filtered = [i for i in items if i.get('scope') == scope_label and i.get('period') == period_name]
    filtered.sort(key=lambda i: i.get('period_start', ''))

    def winner_ids(entry):
        """Set of 'most improved' co-winners for a history row. New rows
        store a list (ties are structural: both doubles partners always get
        identical Elo deltas); rows written before that change only have the
        singular field, so fall back to it."""
        ids = entry.get('most_improved_player_ids')
        if ids:
            return set(ids)
        pid = entry.get('most_improved_player_id')
        return {pid} if pid else set()

    # A "hold" and a "streak" belong to each individual co-winner: if A & B
    # tie this week and A alone wins next week, A is on a 2-streak while B
    # is on a 1-streak.
    holder_counts = {}
    for entry in filtered:
        for pid in winner_ids(entry):
            holder_counts[pid] = holder_counts.get(pid, 0) + 1

    current_streaks = []
    if filtered:
        latest_winners = winner_ids(filtered[-1])
        for pid in latest_winners:
            streak = 0
            for entry in reversed(filtered):
                if pid in winner_ids(entry):
                    streak += 1
                else:
                    break
            current_streaks.append({'player_id': pid, 'streak': streak})
        current_streaks.sort(key=lambda s: (-s['streak'], s['player_id']))

    longest_streaks = {}
    running = {}  # pid -> current consecutive count
    for entry in filtered:
        winners = winner_ids(entry)
        for pid in winners:
            running[pid] = running.get(pid, 0) + 1
            longest_streaks[pid] = max(longest_streaks.get(pid, 0), running[pid])
        for pid in list(running):
            if pid not in winners:
                running[pid] = 0

    return {
        'history': [
            {
                'period_start': e.get('period_start'), 'period_end': e.get('period_end'),
                'computed_at': e.get('computed_at'),
                'most_improved_name': e.get('most_improved_name'), 'most_improved_delta': e.get('most_improved_delta'),
                'most_improved_names': e.get('most_improved_names'),
                'most_improved_player_ids': e.get('most_improved_player_ids')
                    or ([e['most_improved_player_id']] if e.get('most_improved_player_id') else []),
                'most_active_name': e.get('most_active_name'), 'most_active_matches': e.get('most_active_matches'),
                'most_active_names': e.get('most_active_names'),
                'most_active_player_ids': e.get('most_active_player_ids')
                    or ([e['most_active_player_id']] if e.get('most_active_player_id') else []),
            } for e in filtered
        ],
        'holder_counts': [{'player_id': pid, 'count': c} for pid, c in sorted(holder_counts.items(), key=lambda kv: (-kv[1], kv[0]))],
        # Legacy shape (single winner) plus the full co-winner list.
        'current_streak': ({'player_id': current_streaks[0]['player_id'], 'streak': current_streaks[0]['streak']}
                            if current_streaks else None),
        'current_streaks': current_streaks,
        'longest_streaks': [{'player_id': pid, 'streak': s} for pid, s in sorted(longest_streaks.items(), key=lambda kv: (-kv[1], kv[0]))]
    }


def compute_progress_badges(items, group_id_filter=None):
    """For each of the last week/month/year: who improved their rating the
    most, and who played the most matches. Names are resolved live from
    the current Players table, same reasoning as hall_of_fame - a rename
    should show up immediately everywhere, not just in future matches.
    Optionally scoped to a group - shows only that group's members
    competing against each other for the badge, but each member's own
    rating delta is computed from their full match history."""
    member_ids = get_group_member_ids(group_id_filter) if group_id_filter else None

    matches = sorted(items, key=lambda m: m.get('date', ''))
    now = datetime.now(timezone.utc)
    periods = {
        'week': now - timedelta(days=7),
        'month': now - timedelta(days=30),
        'year': now - timedelta(days=365),
    }

    name_cache = {}

    def resolve_name(pid):
        if pid not in name_cache:
            p = players_table.get_item(Key={'player_id': pid}).get('Item')
            name_cache[pid] = display_name(p, pid)
        return name_cache[pid]

    result = {}
    for period_name, cutoff in periods.items():
        rating_before_cutoff = {}
        rating_current = {}
        matches_in_period = {}

        for m in matches:
            date_str = m.get('date', '')
            try:
                match_date = datetime.fromisoformat(date_str)
            except ValueError:
                continue
            ratings_after = m.get('ratings_after') or {}
            for pid, rating in ratings_after.items():
                rating = float(rating)
                if match_date < cutoff:
                    rating_before_cutoff[pid] = rating
                rating_current[pid] = rating
                if match_date >= cutoff:
                    matches_in_period[pid] = matches_in_period.get(pid, 0) + 1

        progress_rows = []
        for pid, current in rating_current.items():
            if member_ids is not None and pid not in member_ids:
                continue
            start = rating_before_cutoff.get(pid, 1000.0)
            delta = round(current - start, 1)
            progress_rows.append({
                'player_id': pid, 'name': resolve_name(pid),
                'delta': delta, 'current_rating': int(round(current)),
                'matches_in_period': matches_in_period.get(pid, 0)
            })
        progress_rows.sort(key=lambda r: -r['delta'])

        most_active = None
        eligible_activity = {pid: cnt for pid, cnt in matches_in_period.items()
                              if member_ids is None or pid in member_ids}
        if eligible_activity:
            active_pid = max(eligible_activity.items(), key=lambda kv: kv[1])[0]
            most_active = {'player_id': active_pid, 'name': resolve_name(active_pid),
                            'matches': eligible_activity[active_pid]}

        result[period_name] = {
            'most_improved_top5': [r for r in progress_rows if r['matches_in_period'] > 0][:5],
            'most_active': most_active
        }

    return result


def compute_partner_distribution(player_id, items, top_n=10):
    """For the radar/spider chart: one player's doubles partners, sorted by
    how often they've played together, capped at top_n so the chart stays
    readable. Percentages are based on the total within whatever scope was
    already applied to `items` (a specific group, or every match). Also
    tracks how many of each partner's matches came from a tournament
    (fixed pairing) versus standalone play, so the frontend can optionally
    highlight the tournament-driven share separately."""
    partner_counts = {}
    partner_tournament_counts = {}
    total = 0
    for m in items:
        if m.get('match_type') != 'doubles':
            continue
        team_a = m.get('team_a') or []
        team_b = m.get('team_b') or []
        if player_id in team_a and len(team_a) == 2:
            team = team_a
        elif player_id in team_b and len(team_b) == 2:
            team = team_b
        else:
            continue
        partner_id = next((pid for pid in team if pid != player_id), None)
        if not partner_id:
            continue
        partner_counts[partner_id] = partner_counts.get(partner_id, 0) + 1
        if m.get('tournament_id'):
            partner_tournament_counts[partner_id] = partner_tournament_counts.get(partner_id, 0) + 1
        total += 1

    result = []
    for pid, count in partner_counts.items():
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        tcount = partner_tournament_counts.get(pid, 0)
        result.append({
            'partner_id': pid,
            'name': display_name(p, pid),
            'matches': count,
            'percentage': round(count / total * 100, 1) if total else 0,
            'tournament_matches': tcount,
            'tournament_percentage': round(tcount / total * 100, 1) if total else 0
        })

    result.sort(key=lambda r: -r['matches'])
    return {'player_id': player_id, 'total_matches': total, 'partners': result[:top_n]}


def _response(status_code, body_dict):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body_dict, default=str)
    }
