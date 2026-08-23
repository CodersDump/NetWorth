"""
NetWorth - tournaments Lambda (singles or doubles)

Routes (via API Gateway {proxy+} on /tournaments):
    POST /tournaments                              -> create tournament
    GET  /tournaments?group_id=X                   -> list tournaments
    GET  /tournaments/{tournament_id}               -> get tournament detail (+ standings)
    POST /tournaments/{tournament_id}/group-score   -> record a group-stage fixture score
    POST /tournaments/{tournament_id}/knockout-score -> record a knockout match score

Formats:
    "knockout"            - random single-elimination bracket
    "groups_then_knockout" - random subgroups (round robin), top N per group
                             advance to a knockout bracket

Body extra for creation:
    "match_type": "singles" | "doubles"  (default "singles")
    For doubles, group members are randomly paired into 2-player teams
    before bracket/group generation. Each "entity" in the bracket has a
    synthetic id, a display name ("Alice & Bob"), and a "members" list of
    the underlying player_id(s) used for Elo updates.

Env vars:
    TOURNAMENTS_TABLE, GROUPS_TABLE, PLAYERS_TABLE, MATCHES_TABLE
"""
import json
import os
import uuid
import random
import boto3
from botocore.exceptions import ClientError
from datetime import datetime, timezone
from string import ascii_uppercase

dynamodb = boto3.resource('dynamodb')
tournaments_table = dynamodb.Table(os.environ['TOURNAMENTS_TABLE'])
groups_table = dynamodb.Table(os.environ['GROUPS_TABLE'])
players_table = dynamodb.Table(os.environ['PLAYERS_TABLE'])
matches_table = dynamodb.Table(os.environ['MATCHES_TABLE'])


def _scan_all(table, **kw):
    """Full-table scan that follows LastEvaluatedKey - a bare .scan() returns
    only the first 1 MB page (KNOWN_ISSUES #15). Every scan in this file
    reads matches and/or players to compute or recompute ratings/results -
    a silently truncated page here means wrong tournament math with no
    error, not a crash. Copied from matches/players lambdas (KNOWN_ISSUES
    #6 - not shared, keep every copy in sync)."""
    items, last = [], None
    while True:
        if last:
            kw['ExclusiveStartKey'] = last
        resp = table.scan(**kw)
        items.extend(resp.get('Items', []))
        last = resp.get('LastEvaluatedKey')
        if not last:
            return items


K_FACTOR = 32
COMEBACK_BONUS_THRESHOLD = 5   # minimum deficit overcome to count as a genuine comeback
COMEBACK_BONUS_PER_POINT = 0.3
COMEBACK_BONUS_CAP = 8


def compute_comeback_bonus(momentum):
    """Extra rating-point bonus for the winning side, on top of the
    standard Elo delta, when they overcame a genuine mid-game deficit.
    Only ever non-zero for matches with a point-by-point log."""
    if not momentum:
        return 0
    deficit = float(momentum.get('winner_overcame_deficit', 0))
    if deficit < COMEBACK_BONUS_THRESHOLD:
        return 0
    return min(deficit * COMEBACK_BONUS_PER_POINT, COMEBACK_BONUS_CAP)


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
CONFIRMATION_CODE = os.environ['CONFIRMATION_CODE']  # supplied at deploy time via GitHub Secrets -> CFN parameter, never stored in the repo


def _is_valid_completed_game(score_a, score_b, target):
    """Same BWF-style rule as the standalone matches Lambda: win by 2 at
    target+ points, hard cap at target+9 where reaching it wins outright."""
    cap = target + 9
    hi, lo = max(score_a, score_b), min(score_a, score_b)
    if hi > cap or lo > cap:
        return False
    if hi == cap:
        return True
    if hi >= target and (hi - lo) >= 2:
        return True
    return False


# Manual-draft tie matches (unlike legacy tournaments) have no organizer-
# configured scoring target at all today - points_to_win/best_of are
# hardcoded defaults (21, best-of-1), never something set at creation for
# this format - and in practice a club tournament's individual matches may
# genuinely be played to 11, 15, or 21 points depending on the day/court/
# time available, not one fixed rule (owner report, 2026-08-21: "it might
# be group stages at 21 and knockout at 11 or 15, but it's not
# guaranteed"). _score_tie_match below accepts a completed game at ANY of
# these common targets without the "invalid score, submit anyway?"
# confirmation a single fixed target would otherwise force on every
# non-21 finish - genuinely implausible entries (an evident typo) still
# fall through to that same confirmation as a safety net.
MANUAL_DRAFT_ACCEPTED_TARGETS = (11, 15, 21)


def _is_valid_manual_draft_game_score(score_a, score_b):
    return any(_is_valid_completed_game(score_a, score_b, t) for t in MANUAL_DRAFT_ACCEPTED_TARGETS)


def _caller_claims(event):
    """Same pattern as matches lambda - see that file's comment for
    context. Present on /create-tournament and, as of manual-draft mode,
    on the whole /tournament-draft{proxy+} tree - NOT on plain
    /tournaments{proxy+}, which is AuthorizationType: NONE at the gateway
    and so never gets requestContext.authorizer.claims populated."""
    return (event.get('requestContext') or {}).get('authorizer', {}).get('claims') or {}


def _is_super_admin(claims):
    """Ported from groups/index.py - identical logic, kept in sync by hand
    (no shared Lambda layer exists yet, see KNOWN_ISSUES #6)."""
    groups = (claims.get('cognito:groups') or '').split(',')
    return 'SuperAdmin' in groups


def _authorize_tournament_organizer(item, claims):
    """Shared check for every manual-draft organizer-only write (set
    leaders, assign/lock pools, and later run the auction): SuperAdmin, or
    already owner/admin of THIS tournament's group. Ported from
    groups/index.py's _authorize_group_action - tournaments/index.py had no
    role-based check anywhere before manual-draft mode (not even DELETE,
    which is gated only by the shared CONFIRMATION_CODE secret, not
    identity). Returns None if allowed, or an error response if not -
    callers just do `if denied: return denied`."""
    if _is_super_admin(claims):
        return None
    caller_player_id = claims.get('custom:player_id')
    group = groups_table.get_item(Key={'group_id': item['group_id']}).get('Item')
    if not group:
        return _response(404, {'error': 'group not found'})
    caller_role = group.get('roles', {}).get(caller_player_id) if caller_player_id else None
    if caller_role not in ('owner', 'admin'):
        return _response(403, {'error': "you must be an owner or admin of this tournament's group to manage the draft"})
    return None


def _authorize_pool_auction_viewer(item, claims):
    """Who may see pool assignments / auction budgets & bids for a
    manual-draft tournament (owner request: this must never reach the
    public, unauthenticated GET /tournaments/{id} - anyone browsing
    tournaments could otherwise see who's in which pool and every leader's
    remaining budget/bid history - see _redact_pool_auction_detail, which
    strips both from that route unconditionally). During the live
    pool-forming/auction phase: organizer (SuperAdmin or group owner/admin)
    OR any of this tournament's leaders - leaders need this to do their
    job live. Once that phase has passed (squads_locked onward): organizer
    only, by the owner's explicit choice - a leader who isn't also the
    organizer no longer has access either."""
    if _authorize_tournament_organizer(item, claims) is None:
        return None
    if item.get('status') in ('pools_open', 'pools_locked', 'auction'):
        caller_pid = claims.get('custom:player_id')
        if caller_pid and caller_pid in (item.get('leaders') or []):
            return None
    return _response(403, {'error': "you do not have access to this tournament's pool/auction detail"})


def create_tournament_enforced(event):
    if not _caller_claims(event):
        return _response(403, {'error': 'log in to create a tournament'})
    return create_tournament(event)


def handler(event, context):
    try:
        method = event.get('httpMethod')
        proxy = (event.get('pathParameters') or {}).get('proxy', '')
        parts = [p for p in proxy.split('/') if p] if proxy else []

        # Epic 7: creating a tournament now requires a real Cognito login,
        # via this isolated top-level route (same reasoning as every other
        # isolated route this session).
        if event.get('resource') == '/create-tournament' and method == 'POST':
            return create_tournament_enforced(event)

        # Manual-draft mode (leaders + pools + auction) lives on its own
        # Cognito-authorized resource tree, /tournament-draft{proxy+} - see
        # _caller_claims' comment for why this can't just be bolted onto
        # /tournaments{proxy+}.
        if (event.get('resource') or '').startswith('/tournament-draft'):
            return handle_draft_route(event)

        if not parts:
            if method == 'POST':
                # Original anonymous path - genuinely closed, not left as
                # a guest fallback (Epic 7 asked for real restriction here).
                return _response(403, {'error': 'log in to create a tournament - use /create-tournament'})
            if method == 'GET':
                return list_tournaments(event)
        elif len(parts) == 1:
            if method == 'GET':
                return get_tournament(parts[0])
            elif method == 'DELETE':
                return delete_tournament(parts[0], event)
        elif len(parts) == 2 and parts[1] == 'group-score':
            if method == 'POST':
                return record_group_score(parts[0], event)
        elif len(parts) == 2 and parts[1] == 'knockout-score':
            if method == 'POST':
                return record_knockout_score(parts[0], event)
        elif len(parts) == 2 and parts[1] == 'substitute':
            if method == 'POST':
                return substitute_player(parts[0], event)

        return _response(404, {'error': 'not found'})
    except Exception as e:
        return _response(500, {'error': str(e)})


# ---------- creation ----------

def seeded_order(players):
    """Sort by current rating, descending. New players just use their
    default 1000 rating - a provisional rating still helps balance far
    more than pure random, and snake pairing (strongest with weakest)
    naturally spreads new players across different teams rather than
    clustering them, so no separate 'experienced vs new' handling is
    needed."""
    return sorted(players, key=lambda p: -float(p.get('rating', 1000)))


def pair_for_balance(ordered_players):
    """Given a skill-ordered list, pair strongest with weakest (snake
    pairing) so doubles teams end up roughly balanced against each other,
    rather than stacking all the strong players together."""
    n = len(ordered_players)
    pairs = []
    i, j = 0, n - 1
    while i < j:
        pairs.append((ordered_players[i], ordered_players[j]))
        i += 1
        j -= 1
    leftover = ordered_players[i] if i == j else None
    return pairs, leftover


def create_tournament(event):
    body = json.loads(event.get('body') or '{}')
    group_id = body.get('group_id')
    name = (body.get('name') or '').strip()
    fmt = body.get('format', 'knockout')
    match_type = body.get('match_type', 'singles')
    points_to_win = int(body.get('points_to_win', 21))
    best_of = int(body.get('best_of', 1))
    num_subgroups = int(body.get('num_subgroups', 2))
    advance_per_group = int(body.get('advance_per_group', 2))
    pairing_mode = body.get('pairing_mode', 'random')  # 'random' | 'seeded' | 'manual'
    manual_teams = body.get('manual_teams')  # optional: [["pid1","pid2"], ["pid3","pid4"], ...]
    participant_ids = body.get('participant_ids')  # optional: subset of group members playing today

    if not group_id or not name:
        return _response(400, {'error': 'group_id and name are required'})
    if match_type not in ('singles', 'doubles'):
        return _response(400, {'error': 'match_type must be singles or doubles'})
    if best_of not in (1, 3):
        return _response(400, {'error': 'best_of must be 1 or 3'})
    if pairing_mode not in ('random', 'seeded', 'manual'):
        return _response(400, {'error': 'pairing_mode must be random, seeded, or manual'})

    group = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    if not group:
        return _response(404, {'error': 'group not found'})

    excluded_player = None

    if manual_teams:
        expected_size = 1 if match_type == 'singles' else 2
        entities = []
        seen_ids = set()
        for team_ids in manual_teams:
            if len(team_ids) != expected_size:
                return _response(400, {'error': f'each manual team needs exactly {expected_size} player(s) for {match_type}'})
            team_players = []
            for pid in team_ids:
                if pid in seen_ids:
                    return _response(400, {'error': 'a player appears in more than one manual team'})
                seen_ids.add(pid)
                p = players_table.get_item(Key={'player_id': pid}).get('Item')
                if not p:
                    return _response(404, {'error': f'player not found: {pid}'})
                team_players.append(p)
            name_str = ' & '.join(p['name'] for p in team_players)
            entities.append({
                'player_id': str(uuid.uuid4()),
                'name': name_str,
                'members': [p['player_id'] for p in team_players],
                'member_ratings': [p.get('rating', 1000) for p in team_players]
            })
        pairing_mode = 'manual'
    else:
        member_ids = group.get('member_ids', [])
        if participant_ids:
            invalid = [pid for pid in participant_ids if pid not in member_ids]
            if invalid:
                return _response(400, {'error': f'these player_ids are not members of this group: {invalid}'})
            member_ids = participant_ids

        players = []
        for pid in member_ids:
            p = players_table.get_item(Key={'player_id': pid}).get('Item')
            if p:
                players.append({'player_id': p['player_id'], 'name': p['name'], 'rating': p.get('rating', 1000)})

        filler_player_id = body.get('filler_player_id')
        if filler_player_id:
            if any(p['player_id'] == filler_player_id for p in players):
                return _response(400, {'error': 'filler_player_id is already in the participant list'})
            filler = players_table.get_item(Key={'player_id': filler_player_id}).get('Item')
            if not filler:
                return _response(404, {'error': 'filler_player_id not found'})
            players.append({'player_id': filler['player_id'], 'name': filler['name'], 'rating': filler.get('rating', 1000)})

        if pairing_mode == 'seeded':
            ordered = seeded_order(players)
        else:
            ordered = players[:]
            random.shuffle(ordered)

        if match_type == 'doubles':
            if len(ordered) < 4:
                return _response(400, {'error': 'doubles needs at least 4 players'})
            if pairing_mode == 'seeded':
                pairs, leftover = pair_for_balance(ordered)
                if leftover:
                    excluded_player = leftover['name']
            else:
                if len(ordered) % 2 == 1:
                    excluded_player = ordered.pop()['name']
                pairs = [(ordered[i], ordered[i + 1]) for i in range(0, len(ordered), 2)]
            entities = []
            for p1, p2 in pairs:
                entities.append({
                    'player_id': str(uuid.uuid4()),
                    'name': f"{p1['name']} & {p2['name']}",
                    'members': [p1['player_id'], p2['player_id']],
                    'member_ratings': [p1['rating'], p2['rating']]
                })
        else:
            if len(ordered) < 2:
                return _response(400, {'error': 'group needs at least 2 players'})
            entities = [{'player_id': p['player_id'], 'name': p['name'], 'members': [p['player_id']],
                         'member_ratings': [p['rating']]} for p in ordered]

    tournament_id = str(uuid.uuid4())
    item = {
        'tournament_id': tournament_id,
        'group_id': group_id,
        'name': name,
        'format': fmt,
        'match_type': match_type,
        'points_to_win': points_to_win,
        'best_of': best_of,
        'pairing_mode': pairing_mode,
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    if excluded_player:
        item['excluded_player'] = excluded_player

    if fmt == 'groups_then_knockout':
        if num_subgroups < 2 or num_subgroups > len(entities):
            return _response(400, {'error': 'num_subgroups must be between 2 and the number of teams/players'})
        subgroup_names = list(ascii_uppercase[:num_subgroups])
        subgroups = {n: {'members': [], 'fixtures': []} for n in subgroup_names}
        for idx, entity in enumerate(entities):
            subgroups[subgroup_names[idx % num_subgroups]]['members'].append(entity)
        for sg in subgroups.values():
            sg['fixtures'] = build_round_robin(sg['members'])
        item['subgroups'] = subgroups
        item['advance_per_group'] = advance_per_group
        item['status'] = 'group_stage'
    else:
        item['knockout'] = {'rounds': [build_knockout_round(entities)]}
        item['status'] = 'knockout'

    tournaments_table.put_item(Item=item)
    return _response(200, item)


def build_round_robin(entities):
    fixtures = []
    for i in range(len(entities)):
        for j in range(i + 1, len(entities)):
            fixtures.append({
                'fixture_id': str(uuid.uuid4()),
                'player_a': entities[i],
                'player_b': entities[j],
                'games': [],
                'games_won_a': 0,
                'games_won_b': 0,
                'played': False,
                'winner_id': None
            })
    return fixtures


def build_knockout_round(entities):
    n = len(entities)
    next_pow2 = 1
    while next_pow2 < n:
        next_pow2 *= 2
    byes_needed = next_pow2 - n

    matches = []
    i = 0
    byes_given = 0
    while i < len(entities):
        if byes_given < byes_needed:
            matches.append(_bye_match(entities[i]))
            byes_given += 1
            i += 1
        else:
            entity_a = entities[i]
            entity_b = entities[i + 1] if i + 1 < len(entities) else None
            if entity_b is None:
                matches.append(_bye_match(entity_a))
            else:
                matches.append({
                    'match_id': str(uuid.uuid4()),
                    'player_a': entity_a,
                    'player_b': entity_b,
                    'games': [],
                    'games_won_a': 0,
                    'games_won_b': 0,
                    'played': False,
                    'winner_id': None
                })
            i += 2
    return matches


def _bye_match(entity):
    return {
        'match_id': str(uuid.uuid4()),
        'player_a': entity,
        'player_b': None,
        'games': [],
        'games_won_a': 0,
        'games_won_b': 0,
        'played': True,
        'winner_id': entity['player_id'],
        'bye': True
    }


# ---------- manual draft mode: leaders & pools (Phase A) ----------
#
# A "manual_draft" tournament is built in stages, unlike the existing
# knockout/groups_then_knockout formats which build their whole entity
# list + bracket at creation time:
#   pools_open -> pools_locked -> auction -> squads_locked -> group_stage
#   -> knockout -> completed
# This section covers only the first two states (pools_open/pools_locked -
# leader selection + the drag/tap pool board). The auction engine
# (pools_locked -> auction -> squads_locked) and the tie-based schedule
# generator (squads_locked -> group_stage -> knockout) are later phases;
# see docs/BACKLOG.md for the phased plan.

def handle_draft_route(event):
    method = event.get('httpMethod')
    proxy = (event.get('pathParameters') or {}).get('proxy', '')
    parts = [p for p in proxy.split('/') if p] if proxy else []

    claims = _caller_claims(event)
    if not claims:
        return _response(403, {'error': 'log in to manage a tournament draft'})

    if not parts:
        if method == 'POST':
            return create_manual_draft_tournament(event, claims)
        return _response(404, {'error': 'not found'})

    tournament_id = parts[0]
    if len(parts) == 1 and method == 'GET':
        return get_draft_sensitive_detail(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'leaders' and method == 'POST':
        return set_leaders(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'add-player' and method == 'POST':
        return add_draft_player(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'remove-player' and method == 'POST':
        return remove_draft_player(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'pools' and method == 'PUT':
        return set_pool_assignment(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'lock-pools' and method == 'POST':
        return lock_pools(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'start-auction' and method == 'POST':
        return start_auction(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'open-lot' and method == 'POST':
        return open_lot(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'bid' and method == 'POST':
        return submit_bid(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'close-lot' and method == 'POST':
        return close_lot(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'organizer-assign' and method == 'POST':
        return organizer_assign(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'skip-lot' and method == 'POST':
        return skip_lot(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'state' and method == 'GET':
        return get_draft_state(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'generate-schedule' and method == 'POST':
        return generate_schedule(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'regenerate-schedule' and method == 'POST':
        return regenerate_schedule(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'rename-squad' and method == 'POST':
        return rename_squad(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'set-squad-pairs' and method == 'POST':
        return set_squad_pairs(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'move-squad-player' and method == 'POST':
        return move_squad_player(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'substitute-squad-player' and method == 'POST':
        return substitute_squad_player(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'pick-tie-player' and method == 'POST':
        return pick_tie_player(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'group-tie-score' and method == 'POST':
        return record_group_tie_score(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'knockout-tie-score' and method == 'POST':
        return record_knockout_tie_score(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'cancel-group-tie-match' and method == 'POST':
        return cancel_group_tie_match(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'forfeit-group-tie-match' and method == 'POST':
        return forfeit_group_tie_match(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'cancel-knockout-tie-match' and method == 'POST':
        return cancel_knockout_tie_match(tournament_id, event, claims)
    if len(parts) == 2 and parts[1] == 'forfeit-knockout-tie-match' and method == 'POST':
        return forfeit_knockout_tie_match(tournament_id, event, claims)

    return _response(404, {'error': 'not found'})


def _draft_get_tournament(tournament_id):
    """Shared load+validate for every route below: must exist and must be
    a manual_draft tournament. Returns (item, None) or (None, error_response)."""
    item = tournaments_table.get_item(Key={'tournament_id': tournament_id}).get('Item')
    if not item:
        return None, _response(404, {'error': 'tournament not found'})
    if item.get('format') != 'manual_draft':
        return None, _response(400, {'error': 'this is not a manual-draft tournament'})
    return item, None


def _draft_everyone(item):
    """Every player currently accounted for in this tournament's pool
    board - the union of the unassigned tray and every pool's members."""
    everyone = set(item['pools']['unassigned'])
    for plist in item['pools']['assignments'].values():
        everyone.update(plist)
    return everyone


def create_manual_draft_tournament(event, claims):
    """Creates the shell for a manual-mode tournament: leaders, pools, the
    auction, squads, and the eventual group_stage/knockout are all empty/
    absent until the routes below (and later phases) fill them in - no
    entities or bracket exist yet, unlike the existing knockout/
    groups_then_knockout formats which build those at creation time."""
    body = json.loads(event.get('body') or '{}')
    group_id = body.get('group_id')
    name = (body.get('name') or '').strip()
    if not group_id or not name:
        return _response(400, {'error': 'group_id and name are required'})

    group = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    if not group:
        return _response(404, {'error': 'group not found'})

    denied = _authorize_tournament_organizer({'group_id': group_id}, claims)
    if denied:
        return denied

    try:
        budget_per_leader = int(body.get('budget_per_leader', 1000))
        num_pools = int(body.get('num_pools', 4))
        picks_per_pool = int(body.get('picks_per_pool', 2))
        group_matches_per_tie = int(body.get('group_matches_per_tie', 2))
        knockout_matches_per_tie = int(body.get('knockout_matches_per_tie', 1))
        # Per-stage overrides (owner request, 2026-08-23: "next time onwards
        # will it ask me how many sets for each semis or finals or third
        # place matches?" - a real live event ran semis+final at one format
        # (best-of-3 to 11) but third place as a single match to 21).
        # Both default to knockout_matches_per_tie so an organizer who
        # doesn't touch these fields gets byte-identical behavior to before.
        final_matches_per_tie = int(body.get('final_matches_per_tie', knockout_matches_per_tie))
        third_place_matches_per_tie = int(body.get('third_place_matches_per_tie', knockout_matches_per_tie))
        num_groups = int(body.get('num_groups', 1))
        advance_per_group = int(body.get('advance_per_group', 1))
        # Games per individual match, per stage (owner request, 2026-08-22:
        # "group stages are played with 21 and the knockout are played with
        # 3 set of 15 or 11 - but it is not guaranteed"). Defaults to 1
        # (today's behavior, byte-identical for every existing tournament)
        # - an organizer opts a new tournament into best-of-3 per stage
        # independently, since the group stage is usually still single-game
        # even when the knockout isn't. The per-game point target itself
        # stays flexible (11/15/21 all accepted without an override - see
        # MANUAL_DRAFT_ACCEPTED_TARGETS) regardless of best_of.
        group_best_of = int(body.get('group_best_of', 1))
        knockout_best_of = int(body.get('knockout_best_of', 1))
    except (TypeError, ValueError):
        return _response(400, {'error': 'budget_per_leader, num_pools, picks_per_pool, group_matches_per_tie, '
                                         'knockout_matches_per_tie, final_matches_per_tie, '
                                         'third_place_matches_per_tie, num_groups, advance_per_group, group_best_of, '
                                         'knockout_best_of must be numbers'})

    match_type = body.get('match_type', 'singles')
    if match_type not in ('singles', 'doubles'):
        return _response(400, {'error': "match_type must be 'singles' or 'doubles'"})

    if group_best_of not in (1, 3):
        return _response(400, {'error': 'group_best_of must be 1 or 3'})
    if knockout_best_of not in (1, 3):
        return _response(400, {'error': 'knockout_best_of must be 1 or 3'})

    group_mode = body.get('group_mode', 'squads')
    if group_mode not in ('squads', 'cross_squad'):
        return _response(400, {'error': "group_mode must be 'squads' or 'cross_squad'"})

    if num_pools < 2:
        return _response(400, {'error': 'num_pools must be at least 2'})
    if picks_per_pool < 1:
        return _response(400, {'error': 'picks_per_pool must be at least 1'})
    if budget_per_leader < 1:
        return _response(400, {'error': 'budget_per_leader must be positive'})
    if group_matches_per_tie < 1 or knockout_matches_per_tie < 1:
        return _response(400, {'error': 'matches_per_tie values must be at least 1'})
    if final_matches_per_tie < 1 or third_place_matches_per_tie < 1:
        return _response(400, {'error': 'matches_per_tie values must be at least 1'})
    if num_groups < 1:
        return _response(400, {'error': 'num_groups must be at least 1'})
    if advance_per_group < 1:
        return _response(400, {'error': 'advance_per_group must be at least 1'})

    member_ids = list(group.get('member_ids', []))
    if len(member_ids) < num_pools:
        return _response(400, {'error': 'the group needs at least as many members as pools'})

    tournament_id = str(uuid.uuid4())
    pool_names = [str(n) for n in range(1, num_pools + 1)]
    item = {
        'tournament_id': tournament_id,
        'group_id': group_id,
        'name': name,
        'format': 'manual_draft',
        'status': 'pools_open',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'created_by': claims.get('email'),
        'manual_draft': {
            'budget_per_leader': budget_per_leader,
            'num_pools': num_pools,
            'picks_per_pool': picks_per_pool,
            'group_matches_per_tie': group_matches_per_tie,
            'knockout_matches_per_tie': knockout_matches_per_tie,
            'final_matches_per_tie': final_matches_per_tie,
            'third_place_matches_per_tie': third_place_matches_per_tie,
            'match_type': match_type,
            'num_groups': num_groups,
            'advance_per_group': advance_per_group,
            'group_mode': group_mode,
            'group_best_of': group_best_of,
            'knockout_best_of': knockout_best_of,
        },
        'leaders': [],
        'pools': {
            'locked': False,
            'assignments': {p: [] for p in pool_names},
            'unassigned': member_ids,
        },
    }
    tournaments_table.put_item(Item=item)
    return _response(200, item)


def set_leaders(tournament_id, event, claims):
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied
    if item.get('status') != 'pools_open':
        return _response(400, {'error': 'leaders can only be set while pools are still open'})

    body = json.loads(event.get('body') or '{}')
    leader_ids = body.get('leader_ids')
    if not leader_ids or not isinstance(leader_ids, list):
        return _response(400, {'error': 'leader_ids (a non-empty list) is required'})
    if len(set(leader_ids)) != len(leader_ids):
        return _response(400, {'error': 'a player appears more than once in leader_ids'})

    invalid = [pid for pid in leader_ids if pid not in _draft_everyone(item)]
    if invalid:
        return _response(400, {'error': f'these player_ids are not in this tournament: {invalid}'})

    item['leaders'] = leader_ids
    tournaments_table.put_item(Item=item)
    return _response(200, item)


def add_draft_player(tournament_id, event, claims):
    """Lets the organizer drop a player into the unassigned tray while
    pools are still open - typically right after using the existing
    /register-and-join route (groups/index.py) to create a brand-new
    profile for someone who isn't in the system yet. No new player-
    creation logic needed here, this just wires an existing player_id into
    the pool board."""
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied
    if item.get('status') != 'pools_open':
        return _response(400, {'error': 'players can only be added while pools are still open'})

    body = json.loads(event.get('body') or '{}')
    player_id = body.get('player_id')
    if not player_id:
        return _response(400, {'error': 'player_id is required'})
    if not players_table.get_item(Key={'player_id': player_id}).get('Item'):
        return _response(404, {'error': 'player not found'})
    if player_id in _draft_everyone(item):
        return _response(400, {'error': 'that player is already in this tournament'})

    item['pools']['unassigned'].append(player_id)
    tournaments_table.put_item(Item=item)
    return _response(200, item)


def remove_draft_player(tournament_id, event, claims):
    """The inverse of add_draft_player: drops someone out of this
    tournament's playing roster entirely, from wherever they currently sit
    (the unassigned tray or a pool). Every group member lands in
    pools.unassigned automatically when the tournament is created, and
    lock_pools refuses to proceed until that tray is empty - with no way to
    take someone out, an organizer who is the group owner/admin but isn't
    personally playing (injury, health reasons, etc) has no way to get
    themselves - or anyone else who isn't participating - out of that
    required roster, and locking pools gets permanently stuck. This lets
    the organizer explicitly excuse a member from the roster while pools
    are still being formed."""
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied
    if item.get('status') != 'pools_open':
        return _response(400, {'error': 'players can only be removed while pools are still open'})

    body = json.loads(event.get('body') or '{}')
    player_id = body.get('player_id')
    if not player_id:
        return _response(400, {'error': 'player_id is required'})
    if player_id not in _draft_everyone(item):
        return _response(400, {'error': 'that player is not part of this tournament'})
    if player_id in (item.get('leaders') or []):
        return _response(400, {'error': 'this player is a leader - remove them from leaders first'})

    item['pools']['unassigned'] = [pid for pid in item['pools']['unassigned'] if pid != player_id]
    for name in item['pools']['assignments']:
        item['pools']['assignments'][name] = [pid for pid in item['pools']['assignments'][name] if pid != player_id]

    tournaments_table.put_item(Item=item)
    return _response(200, item)


def set_pool_assignment(tournament_id, event, claims):
    """Full replace of one pool's member list - the simplest, idempotent
    contract for a drag/tap board that re-sends a pool's whole new
    membership on every move. Any player moved INTO this pool is
    automatically removed from wherever they were before (another pool, or
    the unassigned tray), so every player in _draft_everyone(item) still
    appears in exactly one place after the write."""
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied
    if item['pools'].get('locked'):
        return _response(400, {'error': 'pools are already locked'})

    body = json.loads(event.get('body') or '{}')
    pool = str(body.get('pool', ''))
    player_ids = body.get('player_ids')
    if pool not in item['pools']['assignments']:
        return _response(400, {'error': f'unknown pool: {pool}'})
    if player_ids is None or not isinstance(player_ids, list):
        return _response(400, {'error': 'player_ids (a list, may be empty) is required'})
    if len(set(player_ids)) != len(player_ids):
        return _response(400, {'error': 'a player appears more than once in this pool'})

    everyone = _draft_everyone(item)
    invalid = [pid for pid in player_ids if pid not in everyone]
    if invalid:
        return _response(400, {'error': f'these player_ids are not part of this tournament: {invalid}'})

    old_list = item['pools']['assignments'][pool]
    item['pools']['assignments'][pool] = list(player_ids)
    moved_in = set(player_ids)
    removed = set(old_list) - moved_in

    # Anyone newly assigned to this pool is stripped out of every other
    # pool/the unassigned tray, wherever they were before.
    for name in item['pools']['assignments']:
        if name == pool:
            continue
        item['pools']['assignments'][name] = [pid for pid in item['pools']['assignments'][name] if pid not in moved_in]
    item['pools']['unassigned'] = [pid for pid in item['pools']['unassigned'] if pid not in moved_in]

    # Anyone who was in this pool but isn't in the new list dropped out of
    # it - send them back to the unassigned tray (this is how a player
    # gets un-assigned, not just moved), unless they're somehow already
    # accounted for elsewhere (shouldn't happen, but don't duplicate them
    # if so).
    still_placed = set(item['pools']['unassigned'])
    for plist in item['pools']['assignments'].values():
        still_placed.update(plist)
    for pid in removed:
        if pid not in still_placed:
            item['pools']['unassigned'].append(pid)

    tournaments_table.put_item(Item=item)
    return _response(200, item)


def lock_pools(tournament_id, event, claims):
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied
    if item['pools'].get('locked'):
        return _response(400, {'error': 'pools are already locked'})
    if item['pools']['unassigned']:
        return _response(400, {'error': 'every player must be assigned to a pool before locking'})
    if not item.get('leaders'):
        return _response(400, {'error': 'set leaders before locking pools'})

    picks_per_pool = item['manual_draft']['picks_per_pool']
    pool_of = {}
    for name, plist in item['pools']['assignments'].items():
        for pid in plist:
            pool_of[pid] = name

    missing_leaders = [lid for lid in item['leaders'] if lid not in pool_of]
    if missing_leaders:
        return _response(400, {'error': f'these leaders are not assigned to a pool: {missing_leaders}'})

    # Cheap early warning, not a hard guarantee: every pool needs enough
    # non-leader-owned members for every leader who doesn't already belong
    # to that pool to draft picks_per_pool from it. Leaders competing for
    # the same players during the actual auction can still leave someone
    # short later - that's handled at auction time, not here.
    for name, plist in item['pools']['assignments'].items():
        leaders_in_pool = sum(1 for lid in item['leaders'] if pool_of.get(lid) == name)
        leaders_needing_full_quota = len(item['leaders']) - leaders_in_pool
        available = len(plist) - leaders_in_pool
        if leaders_needing_full_quota * picks_per_pool > available:
            return _response(400, {'error': (
                f'pool {name} does not have enough players for every leader to pick {picks_per_pool} from it '
                f'(has {available} available, needs {leaders_needing_full_quota * picks_per_pool})'
            )})

    item['pools']['locked'] = True
    item['status'] = 'pools_locked'
    tournaments_table.put_item(Item=item)
    return _response(200, item)


# ---------- manual draft mode: the auction (Phase B) ----------
#
# Organizer-paced: the organizer opens one player ("lot") at a time, any
# registered leader can bid (and re-raise their own bid) while it's open,
# and the organizer closes it to award the player to the current highest
# bidder. Leaders' clients poll GET .../state every ~1.75s while a lot is
# open - see docs/BACKLOG.md for why polling was chosen over a WebSocket.
#
# Concurrency: at most one lot is open at a time (organizer-serialized), so
# the only real race is two leaders bidding on the SAME open lot at nearly
# the same moment. submit_bid() below handles that with a conditional
# DynamoDB update_item (not a read-modify-write put_item like every other
# route in this file) - the loser of the race gets a 409 and their next
# poll shows the real current high bid, rather than silently overwriting
# someone else's higher bid.

def _draft_decided_ids(draft):
    """Every player_id that's no longer available to auction: already won
    by a leader (everyone in a squad_member_ids list except the leader
    entry itself, which is pre-seeded with the leader's own id), or marked
    unsold."""
    sold = set()
    for lid, info in draft['leaders'].items():
        sold.update(pid for pid in info['squad_member_ids'] if pid != lid)
    return sold | set(draft.get('unsold', []))


def _authorize_leader(item, claims):
    """Caller must be one of THIS tournament's registered leaders (matched
    by their linked player_id) - used for bid, not for organizer-only
    auction actions (open/close/skip lot, which stay on
    _authorize_tournament_organizer)."""
    caller_pid = claims.get('custom:player_id')
    if not caller_pid or caller_pid not in (item.get('leaders') or []):
        return _response(403, {'error': 'you are not a registered leader for this draft'}), None
    return None, caller_pid


def start_auction(tournament_id, event, claims):
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied
    if item.get('status') != 'pools_locked':
        return _response(400, {'error': 'pools must be locked before starting the auction'})

    leaders = item.get('leaders') or []
    pool_of = {}
    for name, plist in item['pools']['assignments'].items():
        for pid in plist:
            pool_of[pid] = name

    pool_names = sorted(item['pools']['assignments'].keys(), key=int)
    # Pool-ordered nomination queue - leaders themselves are never queued,
    # they're already "won" by themselves (pool_picks seeded below).
    queue = [{'player_id': pid, 'pool': name}
             for name in pool_names for pid in item['pools']['assignments'][name]
             if pid not in leaders]

    budget = item['manual_draft']['budget_per_leader']
    draft_leaders = {}
    for lid in leaders:
        pool_picks = {name: 0 for name in pool_names}
        own_pool = pool_of.get(lid)
        if own_pool is not None:
            # A leader who's already in a pool only needs to pick ONE MORE
            # from it, not picks_per_pool more - seeding their own pick to
            # 1 here means the ordinary "pool_picks[p] >= picks_per_pool"
            # check works unmodified everywhere else, no special-casing.
            pool_picks[own_pool] = 1
        draft_leaders[lid] = {
            'remaining_budget': budget,
            'pool_picks': pool_picks,
            'squad_member_ids': [lid],
        }

    item['draft'] = {
        'status': 'in_progress',
        'queue': queue,
        'current_lot': None,
        'leaders': draft_leaders,
        'unsold': [],
    }
    item['status'] = 'auction'
    tournaments_table.put_item(Item=item)
    return _response(200, item)


def open_lot(tournament_id, event, claims):
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied
    if item.get('status') != 'auction' or (item.get('draft') or {}).get('status') != 'in_progress':
        return _response(400, {'error': 'this tournament is not in an active auction'})
    draft = item['draft']
    if draft.get('current_lot'):
        return _response(400, {'error': 'a lot is already open - close or skip it before opening another'})

    body = json.loads(event.get('body') or '{}')
    player_id = body.get('player_id')
    if not player_id:
        return _response(400, {'error': 'player_id is required'})

    queue_entry = next((q for q in draft['queue'] if q['player_id'] == player_id), None)
    if not queue_entry:
        return _response(400, {'error': 'that player is not in this auction'})
    if player_id in _draft_decided_ids(draft):
        return _response(400, {'error': 'that player has already been sold or marked unsold'})

    draft['current_lot'] = {
        'player_id': player_id,
        'pool': queue_entry['pool'],
        'status': 'open',
        'high_bid': 0,
        'high_bidder_id': None,
        'opened_at': datetime.now(timezone.utc).isoformat(),
        'bid_history': [],
    }
    tournaments_table.put_item(Item=item)
    return _response(200, item)


def submit_bid(tournament_id, event, claims):
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    denied, caller_pid = _authorize_leader(item, claims)
    if denied:
        return denied
    if item.get('status') != 'auction' or (item.get('draft') or {}).get('status') != 'in_progress':
        return _response(400, {'error': 'this tournament is not in an active auction'})
    draft = item['draft']
    current_lot = draft.get('current_lot')
    if not current_lot or current_lot.get('status') != 'open':
        return _response(400, {'error': 'no lot is currently open'})

    body = json.loads(event.get('body') or '{}')
    try:
        amount = int(body.get('amount'))
    except (TypeError, ValueError):
        return _response(400, {'error': 'amount must be a number'})
    if amount <= current_lot['high_bid']:
        return _response(400, {'error': f"amount must be higher than the current bid ({current_lot['high_bid']})"})

    leader = draft['leaders'].get(caller_pid)
    if not leader:
        return _response(404, {'error': 'leader state not found for you in this draft'})
    if amount > leader['remaining_budget']:
        return _response(400, {'error': 'amount exceeds your remaining budget'})
    picks_per_pool = item['manual_draft']['picks_per_pool']
    pool = current_lot['pool']
    if leader['pool_picks'].get(pool, 0) >= picks_per_pool:
        return _response(400, {'error': f'you have already filled your quota for pool {pool}'})

    # Atomic conditional write - NOT a read-modify-write put_item like
    # every other route in this file. Two leaders racing to raise the same
    # lot serialize correctly here: the loser gets ConditionalCheckFailed
    # (-> 409) instead of silently clobbering the winner's higher bid.
    entry = {'leader_id': caller_pid, 'amount': amount, 'at': datetime.now(timezone.utc).isoformat()}
    try:
        tournaments_table.update_item(
            Key={'tournament_id': tournament_id},
            UpdateExpression=(
                'SET draft.current_lot.high_bid = :nb, '
                'draft.current_lot.high_bidder_id = :lid, '
                'draft.current_lot.bid_history = list_append(draft.current_lot.bid_history, :entry)'
            ),
            ConditionExpression='draft.current_lot.player_id = :pid AND draft.current_lot.high_bid < :nb',
            ExpressionAttributeValues={':nb': amount, ':lid': caller_pid, ':pid': current_lot['player_id'], ':entry': [entry]},
        )
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == 'ConditionalCheckFailedException':
            return _response(409, {'error': 'someone bid higher (or the lot changed) just before this - refresh and try again'})
        raise

    updated = tournaments_table.get_item(Key={'tournament_id': tournament_id}).get('Item')
    return _response(200, updated)


def _maybe_freeze_squads(item, draft):
    """Shared by close_lot and organizer_assign: once every leader's every
    pool quota is met, freeze the draft and build squads from each
    leader's accumulated squad_member_ids. Mutates item/draft in place;
    caller still owns the put_item."""
    picks_per_pool = item['manual_draft']['picks_per_pool']
    all_quotas_met = all(
        count >= picks_per_pool
        for info in draft['leaders'].values()
        for count in info['pool_picks'].values()
    )
    if not all_quotas_met:
        return
    draft['status'] = 'completed'
    squads = {}
    for lid, info in draft['leaders'].items():
        member_ids = info['squad_member_ids']
        members_data = [players_table.get_item(Key={'player_id': pid}).get('Item') for pid in member_ids]
        leader_player = players_table.get_item(Key={'player_id': lid}).get('Item')
        leader_name = leader_player['name'] if leader_player else lid
        squads[lid] = {
            'entity_id': str(uuid.uuid4()),
            'name': f"Team {leader_name}",
            'members': member_ids,
            'member_ratings': [(p.get('rating', 1000) if p else 1000) for p in members_data],
            'locked': True,
        }
    item['squads'] = squads
    item['status'] = 'squads_locked'


def close_lot(tournament_id, event, claims):
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied
    if item.get('status') != 'auction' or (item.get('draft') or {}).get('status') != 'in_progress':
        return _response(400, {'error': 'this tournament is not in an active auction'})
    draft = item['draft']
    current_lot = draft.get('current_lot')
    if not current_lot or current_lot.get('status') != 'open':
        return _response(400, {'error': 'no lot is currently open'})
    winner_id = current_lot.get('high_bidder_id')
    if not winner_id:
        return _response(400, {'error': 'nobody has bid on this player yet - use skip-lot instead of close-lot'})

    pool = current_lot['pool']
    player_id = current_lot['player_id']
    amount = current_lot['high_bid']
    leader = draft['leaders'][winner_id]
    leader['remaining_budget'] -= amount
    leader['pool_picks'][pool] = leader['pool_picks'].get(pool, 0) + 1
    leader['squad_member_ids'].append(player_id)
    draft['current_lot'] = None

    _maybe_freeze_squads(item, draft)

    tournaments_table.put_item(Item=item)
    return _response(200, item)


def organizer_assign(tournament_id, event, claims):
    """Lets the organizer record a winning bid and award a player entirely
    on their own say-so, for auctions run partly or fully outside the app
    (called out loud, decided in person, tracked on a whiteboard) where not
    every leader has the app open. Skips the open-lot -> bid -> close-lot
    dance and the leader-authenticated /bid route: the organizer picks the
    player, picks which leader gets them, and types the winning amount, all
    in one organizer-only action. Requires no lot to currently be open, so
    it never collides with a live bid in progress on another player."""
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied
    if item.get('status') != 'auction' or (item.get('draft') or {}).get('status') != 'in_progress':
        return _response(400, {'error': 'this tournament is not in an active auction'})
    draft = item['draft']
    if draft.get('current_lot'):
        return _response(400, {'error': 'a lot is currently open - close or skip it before using organizer-assign'})

    body = json.loads(event.get('body') or '{}')
    player_id = body.get('player_id')
    leader_id = body.get('leader_id')
    amount = body.get('amount')
    if not player_id or not leader_id or amount is None:
        return _response(400, {'error': 'player_id, leader_id, and amount are required'})
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return _response(400, {'error': 'amount must be a number'})
    if amount < 0:
        return _response(400, {'error': 'amount cannot be negative'})

    queue_entry = next((q for q in draft['queue'] if q['player_id'] == player_id), None)
    if not queue_entry:
        return _response(400, {'error': 'that player is not in this auction'})
    if player_id in _draft_decided_ids(draft):
        return _response(400, {'error': 'that player has already been sold or marked unsold'})

    leader = draft['leaders'].get(leader_id)
    if not leader:
        return _response(400, {'error': 'unknown leader_id'})
    if amount > leader['remaining_budget']:
        return _response(400, {'error': "amount exceeds that leader's remaining budget"})
    picks_per_pool = item['manual_draft']['picks_per_pool']
    pool = queue_entry['pool']
    if leader['pool_picks'].get(pool, 0) >= picks_per_pool:
        return _response(400, {'error': f'{leader_id} has already filled their quota for pool {pool}'})

    leader['remaining_budget'] -= amount
    leader['pool_picks'][pool] = leader['pool_picks'].get(pool, 0) + 1
    leader['squad_member_ids'].append(player_id)

    _maybe_freeze_squads(item, draft)

    tournaments_table.put_item(Item=item)
    return _response(200, item)


def skip_lot(tournament_id, event, claims):
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied
    if item.get('status') != 'auction' or (item.get('draft') or {}).get('status') != 'in_progress':
        return _response(400, {'error': 'this tournament is not in an active auction'})
    draft = item['draft']
    current_lot = draft.get('current_lot')
    if not current_lot or current_lot.get('status') != 'open':
        return _response(400, {'error': 'no lot is currently open'})
    if current_lot.get('high_bidder_id'):
        return _response(400, {'error': 'this player already has a bid - use close-lot to award them, not skip-lot'})

    draft.setdefault('unsold', []).append(current_lot['player_id'])
    draft['current_lot'] = None
    tournaments_table.put_item(Item=item)
    return _response(200, item)


def get_draft_state(tournament_id, event, claims):
    """The polling endpoint - a small payload (no bid_history/full item)
    since leaders' clients hit this every ~1.75s while a lot is open. Same
    "organizer, or a leader only while the phase is live" rule as every
    other pool/auction-detail read - see _authorize_pool_auction_viewer."""
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    denied = _authorize_pool_auction_viewer(item, claims)
    if denied:
        return denied
    draft = item.get('draft')
    if not draft:
        return _response(400, {'error': 'the auction has not started yet'})
    decided = _draft_decided_ids(draft)
    return _response(200, {
        'status': draft['status'],
        'current_lot': draft.get('current_lot'),
        'leaders': {lid: {'remaining_budget': info['remaining_budget'], 'pool_picks': info['pool_picks']}
                    for lid, info in draft['leaders'].items()},
        'queue_length': len(draft['queue']),
        'decided_count': len(decided),
        'unsold_count': len(draft.get('unsold', [])),
    })


def get_draft_sensitive_detail(tournament_id, event, claims):
    """The privileged counterpart to the public GET /tournaments/{id},
    which now always redacts pools/draft for manual-draft tournaments (see
    _redact_pool_auction_detail) - this Cognito-gated route is the only
    place that data is ever served from, and only to whoever
    _authorize_pool_auction_viewer allows. The frontend calls this
    alongside the public read and merges the result in when it succeeds,
    silently keeping the redacted stub on a 403 (that's the normal,
    expected outcome for anyone who isn't the organizer or, while their
    phase is live, a leader)."""
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    denied = _authorize_pool_auction_viewer(item, claims)
    if denied:
        return denied
    return _response(200, {'pools': item.get('pools'), 'draft': item.get('draft')})


# ---------- manual draft mode: tie-based schedule (Phase C) ----------
#
# Once every leader's squad is locked, `generate_schedule` builds a single
# round-robin of squad-vs-squad "ties" (`group_stage`), each holding
# `group_matches_per_tie` individual matches. Each match is scored with the
# SAME `_submit_game`/Elo pipeline every other match in this Lambda already
# uses (a tie's match is just a fixture-shaped dict - `player_a`/
# `player_b`/`games`/`games_won_a`/`games_won_b`/`played`/`winner_id` -
# nested one level deeper under a tie instead of a subgroup) - Elo is
# completely untouched, still updates globally per individual match exactly
# as today. What's new is the TIE container around those matches: each
# side's own leader nominates who plays each match slot (`pick_tie_player`,
# not the organizer), and the tie itself is decided by match-wins first,
# aggregate point differential as a cricket-NRR-style tiebreak
# (`_update_tie_progress`) - a genuine deadlock on both is left `decided:
# False` for the organizer to resolve manually, the same philosophy as
# unsold auction players in Phase B. Once every group-stage tie is decided,
# a knockout tie-bracket seeds from squad standings automatically.

def build_tie(squad_a_id, squad_b_id, matches_per_tie):
    # matches_per_tie usually arrives fresh out of item['manual_draft'] or
    # item['knockout'] - DynamoDB round-trips every stored number as
    # decimal.Decimal, not int, so range() below would raise "'Decimal'
    # object cannot be interpreted as an integer" the moment this ran
    # against a real table (the FakeTable test harness never round-trips
    # through Decimal, which is why this went unnoticed until now). Cast
    # defensively here rather than trusting every caller to remember to.
    matches_per_tie = int(matches_per_tie)
    return {
        'tie_id': str(uuid.uuid4()),
        'squad_a': squad_a_id,
        'squad_b': squad_b_id,
        'matches': [
            {'match_id': str(uuid.uuid4()), 'player_a': None, 'player_b': None,
             'games': [], 'games_won_a': 0, 'games_won_b': 0, 'played': False, 'winner_id': None}
            for _ in range(matches_per_tie)
        ],
        'wins_a': 0, 'wins_b': 0, 'point_diff_a': 0, 'point_diff_b': 0,
        'decided': False, 'winner_squad_id': None,
    }


def build_tie_round_robin(squad_ids, matches_per_tie):
    ties = []
    for i in range(len(squad_ids)):
        for j in range(i + 1, len(squad_ids)):
            ties.append(build_tie(squad_ids[i], squad_ids[j], matches_per_tie))
    return ties


def _bye_tie(squad_id):
    """Mirrors _bye_match: auto-decided the instant it's created, no
    matches to play - the lone squad advances untouched."""
    return {
        'tie_id': str(uuid.uuid4()), 'squad_a': squad_id, 'squad_b': None,
        'matches': [], 'wins_a': 0, 'wins_b': 0, 'point_diff_a': 0, 'point_diff_b': 0,
        'decided': True, 'winner_squad_id': squad_id, 'bye': True,
    }


def build_knockout_tie_round(squad_ids, matches_per_tie):
    """Generalizes build_knockout_round: same power-of-2/byes-needed
    bracket-seeding logic, ties instead of plain matches."""
    n = len(squad_ids)
    next_pow2 = 1
    while next_pow2 < n:
        next_pow2 *= 2
    byes_needed = next_pow2 - n

    ties = []
    i = 0
    byes_given = 0
    while i < len(squad_ids):
        if byes_given < byes_needed:
            ties.append(_bye_tie(squad_ids[i]))
            byes_given += 1
            i += 1
        else:
            a = squad_ids[i]
            b = squad_ids[i + 1] if i + 1 < len(squad_ids) else None
            ties.append(_bye_tie(a) if b is None else build_tie(a, b, matches_per_tie))
            i += 2
    return ties


def _update_tie_progress(tie):
    """Recomputes wins_a/wins_b/point_diff_a/point_diff_b from the tie's
    played matches, and decides the tie once every match has been played:
    match-wins first, aggregate point differential (cricket-NRR style) as
    the tiebreak. A genuine deadlock on both is left `decided: False` for
    the organizer to resolve manually - same philosophy as an unsold
    auction player in Phase B, never guessed at silently.

    A match can also be resolved without being actually played (owner
    report, 2026-08-23, live event: 2 group matches could never happen -
    players unavailable on both sides - and separately, a no-show should
    let the other side win outright):
    - `forfeited_by` ('a' or 'b') marks a match one side didn't show up
      for; the OTHER side is credited a match win, matched by side rather
      than by resolving player_id (a forfeiting side may never have
      nominated a lineup at all - see _forfeit_tie_match).
    - `cancelled` marks a match that simply can't be played and shouldn't
      decide anything - it's excluded from both the win tally and the
      point-diff tally entirely (as if it never existed), same as a bye
      contributes nothing. If EVERY match in a tie ends up cancelled, the
      tie itself is resolved with no winner (`decided: True,
      winner_squad_id: None, cancelled: True`) rather than left pending
      forever, which is what would otherwise happen since 0-0 falls
      straight into the genuine-deadlock branch below.

    A tie also decides itself EARLY, the moment one side clinches an
    unbeatable majority of its own matches_per_tie (owner report,
    2026-08-23: a best-of-3 knockout tie that finished 2-0 still showed a
    pointless, still-open Match #3 - "I can see the third match still
    shows up. It should not be"). Standard best-of-N stopping rule:
    winning more than half of the tie's total match slots can't be caught
    by whatever's left, so there's no reason to wait for every slot to be
    played/cancelled/forfeited - matches how any real best-of-3 series
    actually works (stops at 2-0, never plays game 3)."""
    matches = tie['matches']

    def side_a_won(m):
        if not m['played']:
            return False
        if m.get('forfeited_by'):
            return m['forfeited_by'] == 'b'
        return not m.get('cancelled') and m['winner_id'] == (m['player_a'] or {}).get('player_id')

    def side_b_won(m):
        if not m['played']:
            return False
        if m.get('forfeited_by'):
            return m['forfeited_by'] == 'a'
        return not m.get('cancelled') and m['winner_id'] == (m['player_b'] or {}).get('player_id')

    wins_a = sum(1 for m in matches if side_a_won(m))
    wins_b = sum(1 for m in matches if side_b_won(m))
    diff_a = sum(sum(g['score_a'] for g in m['games']) - sum(g['score_b'] for g in m['games'])
                 for m in matches if m['played'] and not m.get('cancelled'))
    tie['wins_a'], tie['wins_b'] = wins_a, wins_b
    tie['point_diff_a'], tie['point_diff_b'] = diff_a, -diff_a

    needed_wins = len(matches) // 2 + 1
    if matches and wins_a >= needed_wins:
        tie['decided'], tie['winner_squad_id'] = True, tie['squad_a']
        return
    if matches and wins_b >= needed_wins:
        tie['decided'], tie['winner_squad_id'] = True, tie['squad_b']
        return

    if not all(m['played'] for m in matches):
        return
    if matches and all(m.get('cancelled') for m in matches):
        tie['decided'], tie['winner_squad_id'], tie['cancelled'] = True, None, True
        return
    if wins_a > wins_b:
        tie['decided'], tie['winner_squad_id'] = True, tie['squad_a']
    elif wins_b > wins_a:
        tie['decided'], tie['winner_squad_id'] = True, tie['squad_b']
    elif diff_a > 0:
        tie['decided'], tie['winner_squad_id'] = True, tie['squad_a']
    elif diff_a < 0:
        tie['decided'], tie['winner_squad_id'] = True, tie['squad_b']
    else:
        tie['decided'], tie['winner_squad_id'] = False, None  # genuine deadlock


def _score_tie_match(item, tie, match_index, score_a, score_b, override, point_log, stage_label):
    """Submits one individual match's score within a tie. Raises ValueError
    on any validation failure (caught by the calling route and turned into
    a 400) - reuses _submit_game unchanged since a tie's match is shaped
    exactly like every other fixture/knockout match in this file."""
    matches = tie['matches']
    if match_index < 0 or match_index >= len(matches):
        raise ValueError('invalid match_index')
    # A tie can now decide itself early, before every match slot is played
    # (see _update_tie_progress's majority check) - once that's happened,
    # any remaining slot is moot and shouldn't be scoreable: it can't
    # change who won the tie, and letting it add to point_diff_a/b would
    # still leak into that squad's standings tiebreaker even though the
    # match had no bearing on this tie's own outcome.
    if tie.get('decided'):
        raise ValueError('this tie is already decided - the remaining match is not needed')
    match = matches[match_index]
    if match['played']:
        raise ValueError('this match is already decided')
    if not match.get('player_a') or not match.get('player_b'):
        raise ValueError('both squads must nominate a player for this match before it can be scored')

    # best_of is stage-specific (owner request, 2026-08-22: group stage is
    # usually single-game even when the knockout is best-of-3) - a group
    # match uses manual_draft.group_best_of, a knockout or third-place
    # match uses manual_draft.knockout_best_of. Both default to 1, matching
    # every tournament created before this config existed.
    md = item.get('manual_draft', {})
    best_of = md.get('group_best_of', 1) if stage_label == 'group' else md.get('knockout_best_of', 1)
    target = item.get('points_to_win', 21)
    # Score flexibility (owner report, 2026-08-21) - see
    # _is_valid_manual_draft_game_score above: a completed game at any of
    # the common 11/15/21 targets is accepted directly, without forcing
    # the caller through an explicit override confirmation first.
    if not override and score_a != score_b and _is_valid_manual_draft_game_score(score_a, score_b):
        override = True
    decided = _submit_game(match, score_a, score_b, best_of, target, override)
    if decided:
        total_a = sum(g['score_a'] for g in match['games'])
        total_b = sum(g['score_b'] for g in match['games'])
        winner = 'A' if match['games_won_a'] > match['games_won_b'] else 'B'
        # match_type comes from the tournament's own manual_draft config, not
        # a hardcoded 'singles' - a doubles-configured tournament nominates
        # player pairs (see pick_tie_player) and needs the doubles K-factor/
        # pairing-count Elo path in update_elo_and_log, not the singles one.
        match_type = item.get('manual_draft', {}).get('match_type', 'singles')
        update_elo_and_log(match_type, match['player_a'], match['player_b'], total_a, total_b,
                            item['group_id'], item['tournament_id'], stage_label,
                            winner_override=winner, games=match['games'], point_log=point_log)
        _update_tie_progress(tie)


def _cancel_tie_match(tie, match_index):
    """Marks one match as administratively cancelled - can't be played
    (owner report, 2026-08-23: players unavailable on both sides, a live
    group match could never happen) and shouldn't decide anything. No
    Elo change, no score - see _update_tie_progress for how a cancelled
    match is excluded from the tie's win/point-diff tally."""
    matches = tie['matches']
    if match_index < 0 or match_index >= len(matches):
        raise ValueError('invalid match_index')
    if tie.get('decided'):
        raise ValueError('this tie is already decided - the remaining match is not needed')
    match = matches[match_index]
    if match['played']:
        raise ValueError('this match is already decided')
    match['played'] = True
    match['cancelled'] = True
    match['games'] = []
    match['games_won_a'] = 0
    match['games_won_b'] = 0
    match['winner_id'] = None
    _update_tie_progress(tie)


def _forfeit_tie_match(tie, match_index, forfeited_by):
    """Marks one match as forfeited by one side (owner report, 2026-08-23:
    "keep an option for forfeit when either of the team doesn't show up")
    - the OTHER side is credited the match win. Deliberately doesn't
    require either side to have nominated a lineup first (unlike a normal
    score submission) - the whole point is to cover the case where the
    absent side never nominated anyone. No Elo change: a no-show isn't a
    real result, so nobody's rating moves for it."""
    matches = tie['matches']
    if match_index < 0 or match_index >= len(matches):
        raise ValueError('invalid match_index')
    if forfeited_by not in ('a', 'b'):
        raise ValueError('forfeited_by must be "a" or "b"')
    if tie.get('decided'):
        raise ValueError('this tie is already decided - the remaining match is not needed')
    match = matches[match_index]
    if match['played']:
        raise ValueError('this match is already decided')
    match['played'] = True
    match['forfeited_by'] = forfeited_by
    match['games'] = []
    match['games_won_a'] = 0
    match['games_won_b'] = 0
    winner_entity = match.get('player_b') if forfeited_by == 'a' else match.get('player_a')
    match['winner_id'] = winner_entity['player_id'] if winner_entity else None
    _update_tie_progress(tie)


def _find_tie(item, tie_id):
    """A tie_id is a UUID unique across the whole tournament, so it can be
    looked up without the caller needing to say which stage/round it's
    in - group stage, any knockout round, or the third-place match."""
    for tie in (item.get('group_stage') or {}).get('ties', []):
        if tie['tie_id'] == tie_id:
            return tie
    for rnd in (item.get('knockout') or {}).get('rounds', []):
        for tie in rnd:
            if tie.get('tie_id') == tie_id:
                return tie
    tpm = (item.get('knockout') or {}).get('third_place_match')
    if tpm and tpm.get('tie_id') == tie_id:
        return tpm
    return None


def _tie_side_leader_id(item, side_id):
    """Resolves a tie's squad_a/squad_b value to the leader id who's
    allowed to act for it. For the regular squads-per-group mode side_id
    already IS the leader id (unchanged). For cross-squad group mode
    (owner request, 2026-08-21), side_id is a rep_id (a squad's pre-fixed
    pair for this one group) - resolves to that rep's parent_squad_id,
    which IS the leader id, so that squad's own leader can still act for
    their rep exactly as if it were their whole squad."""
    return (item.get('reps') or {}).get(side_id, {}).get('parent_squad_id', side_id)


def _authorize_tie_scorer(item, tie, claims):
    """Organizer, or one of THIS tie's own two squad leaders - matches the
    plan's "organizer or either squad's leader" auth level for score
    submission (this whole route tree is Cognito-gated, unlike the legacy
    /tournaments{proxy+} scoring routes which have no auth at all)."""
    denied = _authorize_tournament_organizer(item, claims)
    if not denied:
        return None
    caller_pid = claims.get('custom:player_id')
    side_leaders = (_tie_side_leader_id(item, tie.get('squad_a')), _tie_side_leader_id(item, tie.get('squad_b')))
    if caller_pid and caller_pid in side_leaders:
        return None
    return _response(403, {'error': "only the organizer or one of this tie's two squad leaders can submit a score"})


def compute_squad_standings(item, squad_ids=None):
    """Squad-level standings: sorted by (ties_won desc, aggregate point
    differential desc) - same score-based rule used to decide a single
    tie, one level up. Pass squad_ids to scope this to one group's
    standings (real separate groups, owner request 2026-08-21) instead of
    every squad in the tournament - every tie-walking line below already
    guards with `if a in stats`/`if b in stats`, so restricting which
    squads have a stats entry is the only change needed to scope it."""
    # Merges in item['reps'] alongside item['squads'] - cross-squad group
    # mode (owner request, 2026-08-21) plays ties between REP entities (a
    # squad's pre-fixed pair/player for one specific group), not whole
    # squads, so a tie's squad_a/squad_b may be a rep_id. item.get('reps')
    # is always {} for every tournament not using this mode, so this is a
    # no-op everywhere else.
    squads = {**(item.get('squads') or {}), **(item.get('reps') or {})}
    if squad_ids is not None:
        squads = {sid: sq for sid, sq in squads.items() if sid in squad_ids}
    stats = {sid: {'squad_id': sid, 'name': sq.get('name', sid), 'ties_won': 0, 'ties_lost': 0, 'point_diff': 0}
              for sid, sq in squads.items()}
    for tie in (item.get('group_stage') or {}).get('ties', []):
        if not tie.get('decided'):
            continue
        a, b = tie.get('squad_a'), tie.get('squad_b')
        if a in stats:
            stats[a]['point_diff'] += tie.get('point_diff_a', 0)
        if b in stats:
            stats[b]['point_diff'] += tie.get('point_diff_b', 0)
        winner = tie.get('winner_squad_id')
        if winner == a and a in stats:
            stats[a]['ties_won'] += 1
            if b in stats:
                stats[b]['ties_lost'] += 1
        elif winner == b and b in stats:
            stats[b]['ties_won'] += 1
            if a in stats:
                stats[a]['ties_lost'] += 1
    standings = list(stats.values())
    standings.sort(key=lambda s: (-s['ties_won'], -s['point_diff']))
    return standings


def compute_projected_knockout(item):
    """Read-time-only preview of the knockout matchup, computed from the
    group stage's CURRENT standings - shown while one or more group ties
    are still undecided (owner report, 2026-08-22: "2 matches are pending
    in group stage but we clearly see the teams qualifying for semifinal,
    will that matchup not be released?"). The real item['knockout'] only
    gets built once EVERY group tie is decided (see record_group_tie_score)
    - this fills that gap with a clearly-labeled projection instead of
    making everyone wait for the last couple of matches once the outcome
    is already obvious from the table.

    Never persisted (same "computed fresh on every read" convention as
    squad_standings/player_tournament_stats), and entirely separate from
    the real knockout - no tie objects are created, nothing here is ever
    scored. compute_squad_standings only credits DECIDED ties, so a squad
    with pending ties gets no partial credit either way - this is exactly
    the same ranking a viewer would read off the standings table right
    now. It's still just a projection, not a guarantee: point
    differential (part of the tiebreak) is unbounded, so a squad's
    position here could still change depending on how the remaining
    matches go - the frontend must label this "projected", never final.

    Scoped to the simple case only: real separate named groups
    (group_stage['groups']) need advance_per_group + tiebreak-injection
    logic (_inject_group_tiebreakers_if_needed/
    _advance_squads_to_knockout_from_groups) to know who's even eligible
    to advance - deliberately not replicated here, so this returns None
    for that case rather than guessing at a shape that could turn out
    wrong. Also None once there's nothing left pending (the real knockout
    is either already built or about to be on the next score submission)."""
    group_stage = item.get('group_stage') or {}
    if group_stage.get('groups'):
        return None
    ties = group_stage.get('ties', [])
    if not ties:
        return None
    pending_ties = [t for t in ties if not t.get('decided')]
    if not pending_ties:
        return None

    standings = compute_squad_standings(item)
    seeded_squad_ids = [s['squad_id'] for s in standings]
    md = item.get('manual_draft', {})
    matches_per_tie = int(md.get('knockout_matches_per_tie', 1))
    final_matches_per_tie = int(md.get('final_matches_per_tie', matches_per_tie))
    first_round_matches_per_tie = final_matches_per_tie if len(seeded_squad_ids) <= 2 else matches_per_tie
    return {
        'rounds': [build_knockout_tie_round(seeded_squad_ids, first_round_matches_per_tie)],
        'pending_group_ties': len(pending_ties),
    }


def compute_group_stage_projection(item):
    """Real-separate-groups sibling of compute_projected_knockout (owner
    report, 2026-08-22, live event with real named groups: "group A has
    these teams and then these teams proceed to knockout... under bracket
    it should also just list the groups"). The real advancement path
    (_advance_squads_to_knockout_from_groups) shuffles qualifiers into the
    knockout pairing RANDOMLY (with a best-effort same-group swap to avoid
    round-1 rematches) - so unlike the flat round-robin case, there's no
    single deterministic "projected matchup" to show even once every
    qualifier is fully known. What IS knowable ahead of time, from the
    exact same per-group standings the Table view already shows
    (`group_standings`), is WHICH squads are projected to advance from
    each group - so that's what this returns instead of a pairing.

    One entry per group name: `squads` (that group's own
    compute_squad_standings output, already in current-standings order),
    `advancing_ids` (squads currently safely inside the top
    advance_per_group cutoff), `contested_ids` (empty, unless the group's
    standings are EXACTLY level - both ties_won and point_diff - right at
    that cutoff, in which case this holds the two squads contesting the
    last spot and advancing_ids stops one short of advance_per_group -
    mirrors the exact same boundary check
    _inject_group_tiebreakers_if_needed uses for real, which would append
    a tiebreaker match rather than guess), and `pending_ties` (how many of
    THIS group's own ties are still undecided). Never persisted - same
    "computed fresh on every read" convention as the rest of this file's
    read-time-only fields. Only meaningful while `status == 'group_stage'`
    - by the time the real knockout exists there's nothing left to add."""
    group_stage = item.get('group_stage') or {}
    groups = group_stage.get('groups')
    if not groups:
        return None
    advance_n = int(item.get('manual_draft', {}).get('advance_per_group', 1))
    result = {}
    for name, members in groups.items():
        standings = compute_squad_standings(item, squad_ids=members)
        group_ties = [t for t in group_stage.get('ties', []) if t.get('group') == name]
        pending_ties = sum(1 for t in group_ties if not t.get('decided'))
        contested_ids = []
        advancing_ids = [s['squad_id'] for s in standings[:advance_n]]
        if 0 < advance_n < len(standings):
            boundary_a, boundary_b = standings[advance_n - 1], standings[advance_n]
            if boundary_a['ties_won'] == boundary_b['ties_won'] and boundary_a['point_diff'] == boundary_b['point_diff']:
                contested_ids = [boundary_a['squad_id'], boundary_b['squad_id']]
                advancing_ids = advancing_ids[:advance_n - 1]
        result[name] = {
            'squads': standings,
            'advancing_ids': advancing_ids,
            'contested_ids': contested_ids,
            'pending_ties': pending_ties,
        }
    return result


def compute_squad_standings_by_parent(item):
    """Cross-squad group mode sibling of compute_squad_standings: rolls
    each real squad's several rep entities (one fixed pair/player per
    group - see _build_cross_squad_group_stage) back up into a single row
    per real squad. The plain per-rep table isn't meaningful on its own
    here since two reps from the same squad never play each other and
    land in different groups - this is the "who's actually winning"
    table for the tournament as a whole."""
    reps = item.get('reps') or {}
    squads = item.get('squads') or {}
    stats = {sid: {'squad_id': sid, 'name': sq.get('name', sid), 'ties_won': 0, 'ties_lost': 0, 'point_diff': 0}
              for sid, sq in squads.items()}
    for tie in (item.get('group_stage') or {}).get('ties', []):
        if not tie.get('decided'):
            continue
        a, b = tie.get('squad_a'), tie.get('squad_b')
        parent_a = reps.get(a, {}).get('parent_squad_id', a)
        parent_b = reps.get(b, {}).get('parent_squad_id', b)
        if parent_a in stats:
            stats[parent_a]['point_diff'] += tie.get('point_diff_a', 0)
        if parent_b in stats:
            stats[parent_b]['point_diff'] += tie.get('point_diff_b', 0)
        winner = tie.get('winner_squad_id')
        winner_parent = reps.get(winner, {}).get('parent_squad_id', winner)
        if winner_parent == parent_a and parent_a in stats:
            stats[parent_a]['ties_won'] += 1
            if parent_b in stats:
                stats[parent_b]['ties_lost'] += 1
        elif winner_parent == parent_b and parent_b in stats:
            stats[parent_b]['ties_won'] += 1
            if parent_a in stats:
                stats[parent_a]['ties_lost'] += 1
    standings = list(stats.values())
    standings.sort(key=lambda s: (-s['ties_won'], -s['point_diff']))
    return standings


def compute_player_tournament_scores(item):
    """A tournament-scoped, non-Elo per-player score/leaderboard - a
    read-time-only aggregation (like compute_all_standings, never
    persisted) over every individual tie-match a player appeared in, group
    stage or knockout. Deliberately separate from Elo, which is untouched -
    update_elo_and_log already ran, exactly as today, the moment each match
    was scored."""
    stats = {}

    def touch(pid, name):
        if pid not in stats:
            stats[pid] = {'player_id': pid, 'name': name, 'matches_played': 0, 'wins': 0, 'losses': 0, 'point_diff': 0}
        return stats[pid]

    def apply_match(m):
        # A cancelled match (owner request, 2026-08-23: players unavailable
        # on both sides, can't be replayed) never happened - excluded here
        # the same way it's excluded from the tie's own win/point-diff
        # tally (_update_tie_progress), so it doesn't inflate anyone's
        # matches_played with a phantom 0-0. A forfeited match DID resolve
        # a real winner/loser and still counts normally below.
        if not m['played'] or m.get('cancelled') or not m.get('player_a') or not m.get('player_b'):
            return
        a, b = m['player_a'], m['player_b']
        total_a = sum(g['score_a'] for g in m['games'])
        total_b = sum(g['score_b'] for g in m['games'])
        a_won = m['winner_id'] == a['player_id']
        b_won = m['winner_id'] == b['player_id']

        def member_names(entity):
            # a doubles pair entity's own 'name' is "X & Y" - split it back
            # out per-member when the shape matches, so individual players
            # get their own name in the tournament-scoped leaderboard rather
            # than the pair's combined label; falls back to the player_id.
            members = entity.get('members', [entity['player_id']])
            parts = entity.get('name', '').split(' & ')
            if len(members) == len(parts):
                return dict(zip(members, parts))
            return {pid: pid for pid in members}

        # a/b are "entities" - a single player for a singles tournament, or a
        # synthetic pair {player_id: <pair uuid>, members: [p1, p2]} for a
        # doubles one (see pick_tie_player/build doubles entity). Either way,
        # credit belongs to the real player_ids in `members`, not the pair's
        # own synthetic id - matches how update_elo_and_log already resolves
        # entity.get('members', [entity['player_id']]) for Elo.
        for pid, nm in member_names(a).items():
            s = touch(pid, nm)
            s['matches_played'] += 1
            s['point_diff'] += (total_a - total_b)
            if a_won:
                s['wins'] += 1
            elif b_won:
                s['losses'] += 1
        for pid, nm in member_names(b).items():
            s = touch(pid, nm)
            s['matches_played'] += 1
            s['point_diff'] += (total_b - total_a)
            if b_won:
                s['wins'] += 1
            elif a_won:
                s['losses'] += 1

    for tie in (item.get('group_stage') or {}).get('ties', []):
        for m in tie.get('matches', []):
            apply_match(m)
    for rnd in (item.get('knockout') or {}).get('rounds', []):
        for tie in rnd:
            for m in tie.get('matches', []):
                apply_match(m)
    tpm = (item.get('knockout') or {}).get('third_place_match')
    if tpm:
        for m in tpm.get('matches', []):
            apply_match(m)

    result = list(stats.values())
    result.sort(key=lambda s: (-s['wins'], -s['point_diff']))
    return result


def rename_squad(tournament_id, event, claims):
    """Squads get an auto-generated name ("Team <leader>") the instant the
    auction auto-freezes - the owner asked for the organizer OR that
    squad's own leader to be able to rename it to something the club
    actually wants to call it. Available from squads_locked onward (once
    the squads dict exists) through to completed - a name is cosmetic, so
    there's no reason to lock it once the schedule starts."""
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    if not item.get('squads'):
        return _response(400, {'error': 'squads do not exist yet for this tournament'})

    body = json.loads(event.get('body') or '{}')
    squad_id = body.get('squad_id')
    name = (body.get('name') or '').strip()
    if not squad_id or not name:
        return _response(400, {'error': 'squad_id and name are required'})
    if len(name) > 60:
        return _response(400, {'error': 'name is too long (60 characters max)'})

    squad = (item.get('squads') or {}).get(squad_id)
    if not squad:
        return _response(400, {'error': 'unknown squad_id'})

    caller_pid = claims.get('custom:player_id')
    if caller_pid != squad_id and _authorize_tournament_organizer(item, claims) is not None:
        return _response(403, {'error': "only the organizer or this squad's own leader can rename it"})

    squad['name'] = name
    tournaments_table.put_item(Item=item)
    return _response(200, item)


def set_squad_pairs(tournament_id, event, claims):
    """Cross-squad group mode only (owner request, 2026-08-21): before the
    group stage is generated, each squad's own leader (or the organizer)
    fixes that squad's own doubles pairs (or, for singles, its own solo
    reps) upfront - exactly manual_draft.num_groups of them, one per group
    the squad will be represented in. _build_cross_squad_group_stage then
    sends exactly one of these pre-fixed units into every group, instead
    of leaders picking who plays match-by-match like the regular squads-
    per-group mode does. Can be set/edited any time up through group_stage
    as long as nothing in the CURRENT schedule has been played yet - same
    safety net as regenerate_schedule, since re-splitting after real
    results existed would be incoherent."""
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    if item.get('status') not in ('squads_locked', 'group_stage'):
        return _response(400, {'error': 'squad pairs can only be set once squads are locked, before the group stage has been played'})
    if item.get('status') == 'group_stage':
        existing_ties = (item.get('group_stage') or {}).get('ties', [])
        if any(m.get('played') for t in existing_ties for m in (t.get('matches') or [])):
            return _response(400, {'error': 'cannot change squad pairs - some matches in the current schedule have already been played'})

    body = json.loads(event.get('body') or '{}')
    squad_id = body.get('squad_id')
    pairs = body.get('pairs')
    if not squad_id or not isinstance(pairs, list) or not pairs:
        return _response(400, {'error': 'squad_id and a non-empty pairs list are required'})

    squad = (item.get('squads') or {}).get(squad_id)
    if not squad:
        return _response(400, {'error': 'unknown squad_id'})

    caller_pid = claims.get('custom:player_id')
    if caller_pid != squad_id and _authorize_tournament_organizer(item, claims) is not None:
        return _response(403, {'error': "only the organizer or this squad's own leader can set its pairs"})

    num_groups = int(item.get('manual_draft', {}).get('num_groups', 1))
    if len(pairs) != num_groups:
        return _response(400, {'error': f'this tournament has {num_groups} group{"s" if num_groups != 1 else ""} - '
                                         f'set exactly {num_groups} pair{"s" if num_groups != 1 else ""} (one per group), got {len(pairs)}'})

    match_type = item.get('manual_draft', {}).get('match_type', 'singles')
    expected_size = 2 if match_type == 'doubles' else 1
    squad_members = set(squad.get('members', []))
    seen = set()
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != expected_size:
            return _response(400, {'error': f'this is a {match_type} tournament - each pair needs exactly {expected_size} '
                                             f'player{"s" if expected_size > 1 else ""}'})
        for pid in pair:
            if pid not in squad_members:
                return _response(400, {'error': f'{pid} is not a member of this squad'})
            if pid in seen:
                return _response(400, {'error': f'{pid} appears in more than one pair'})
            seen.add(pid)

    squad['pairs'] = pairs
    tournaments_table.put_item(Item=item)
    return _response(200, item)


def move_squad_player(tournament_id, event, claims):
    """Organizer-only roster rebalancing between two squads, before the
    schedule exists - the auction is over, so there's no budget bookkeeping
    to touch, just a plain roster move. Restricted to squads_locked only:
    once ties are generated they reference squad_a/squad_b by id, and
    moving a player to a DIFFERENT squad mid-tournament would change who's
    on which side of an already-scheduled fixture, which isn't coherent -
    substitute_squad_player (below) is the right tool once matches exist."""
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied
    if item.get('status') != 'squads_locked':
        return _response(400, {'error': 'squads can only be rebalanced after the auction, before the schedule is generated'})

    body = json.loads(event.get('body') or '{}')
    player_id = body.get('player_id')
    to_squad_id = body.get('to_squad_id')
    if not player_id or not to_squad_id:
        return _response(400, {'error': 'player_id and to_squad_id are required'})

    squads = item.get('squads') or {}
    if to_squad_id not in squads:
        return _response(400, {'error': 'unknown to_squad_id'})
    if player_id in squads:
        return _response(400, {'error': 'a squad leader cannot be moved to another squad'})

    from_squad_id = next((sid for sid, sq in squads.items() if player_id in sq.get('members', [])), None)
    if not from_squad_id:
        return _response(400, {'error': 'that player is not a member of any squad'})
    if from_squad_id == to_squad_id:
        return _response(400, {'error': 'that player is already on this squad'})

    from_squad, to_squad = squads[from_squad_id], squads[to_squad_id]
    idx = from_squad['members'].index(player_id)
    from_squad['members'].pop(idx)
    rating = from_squad.get('member_ratings', []).pop(idx) if idx < len(from_squad.get('member_ratings', [])) else 1000
    to_squad['members'].append(player_id)
    to_squad.setdefault('member_ratings', []).append(rating)

    tournaments_table.put_item(Item=item)
    return _response(200, item)


def _rebuild_entity_after_substitution(entity, old_player_id, new_player_id, new_player):
    """Swaps old_player_id for new_player_id inside a squad-pair/rep/
    match-player entity's members + member_ratings, and regenerates its
    display name from the (now different) member names - mirrors how
    pick_tie_player and _build_cross_squad_group_stage build these names
    in the first place. Never touches entity_id/player_id, which is the
    synthetic pair/rep identity and doesn't change just because one of its
    humans did. Returns False (no-op) if old_player_id isn't actually one
    of this entity's members."""
    members = entity.get('members')
    if not members or old_player_id not in members:
        return False
    idx = members.index(old_player_id)
    members[idx] = new_player_id
    ratings = entity.get('member_ratings')
    if ratings is not None and idx < len(ratings):
        ratings[idx] = new_player.get('rating', 1000)
    names = []
    for pid in members:
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        names.append(p['name'] if p else pid)
    entity['name'] = ' & '.join(names) if len(names) > 1 else names[0]
    return True


def substitute_squad_player(tournament_id, event, claims):
    """Organizer-only real substitution for a manual-draft squad: swaps a
    current squad member out for a brand-new replacement who isn't already
    on any squad. Unlike move_squad_player, this doesn't touch which two
    squads are facing each other, so it's safe at any point up to
    'completed' - injury/unavailability mid-tournament is exactly the
    'hard stop' the owner reported (substitute_player flatly rejects every
    manual-draft tournament). Any FUTURE unplayed match slot where the
    outgoing player was already (but not yet) nominated is cleared back to
    None so the leader is prompted to re-pick - already-PLAYED matches keep
    their recorded player snapshot untouched, exactly like the legacy
    substitute_player's own "don't touch history" behavior."""
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied
    if item.get('status') not in ('squads_locked', 'group_stage', 'knockout'):
        return _response(400, {'error': 'squad substitution is only available from squads_locked through knockout'})

    body = json.loads(event.get('body') or '{}')
    squad_id = body.get('squad_id')
    old_player_id = body.get('old_player_id')
    new_player_id = body.get('new_player_id')
    if not squad_id or not old_player_id or not new_player_id:
        return _response(400, {'error': 'squad_id, old_player_id, new_player_id are required'})
    if old_player_id == new_player_id:
        return _response(400, {'error': 'old_player_id and new_player_id must be different'})

    squads = item.get('squads') or {}
    squad = squads.get(squad_id)
    if not squad:
        return _response(400, {'error': 'unknown squad_id'})
    if old_player_id not in squad.get('members', []):
        return _response(400, {'error': 'that player is not a member of this squad'})
    if old_player_id == squad_id:
        return _response(400, {'error': 'the squad leader cannot be substituted out - only picked members can be'})

    everyone_on_a_squad = {pid for sq in squads.values() for pid in sq.get('members', [])}
    if new_player_id in everyone_on_a_squad:
        return _response(400, {'error': 'that replacement is already a member of a squad in this tournament'})
    new_player = players_table.get_item(Key={'player_id': new_player_id}).get('Item')
    if not new_player:
        return _response(404, {'error': 'replacement player not found'})

    idx = squad['members'].index(old_player_id)
    squad['members'][idx] = new_player_id
    if idx < len(squad.get('member_ratings', [])):
        squad['member_ratings'][idx] = new_player.get('rating', 1000)

    # Cross-squad group mode (owner report, 2026-08-21: a substituted-out
    # player still showed up in the schedule, and still got credited the
    # Elo for a match the replacement actually played): a squad's fixed
    # `pairs` (set via set_squad_pairs) and the rep entities built from
    # them (item['reps']) are snapshots taken once, at group-generation
    # time - updating squad['members'] alone, as above, never reaches
    # either of them, so the outgoing player kept appearing everywhere the
    # schedule had already baked their name in. Both need the same swap.
    cross_squad = bool(item.get('reps'))
    if cross_squad:
        for pair in squad.get('pairs') or []:
            if old_player_id in pair:
                pair[pair.index(old_player_id)] = new_player_id
        for rep in (item.get('reps') or {}).values():
            if rep.get('parent_squad_id') == squad_id:
                _rebuild_entity_after_substitution(rep, old_player_id, new_player_id, new_player)

    # Any not-yet-played match slot where the outgoing player was already
    # nominated needs fixing too. Regular squads-per-group mode still has
    # a re-nomination step (pick_tie_player), so clearing the slot back to
    # None there is correct, exactly as before. Cross-squad reps have NO
    # re-nomination step at all - clearing would strand that match at
    # "waiting on lineup" forever - so those get repaired in place with
    # the same swap instead. _tie_side_leader_id resolves squad_a/squad_b
    # correctly either way (it's a no-op passthrough for regular mode,
    # and resolves a rep_id back to its parent squad for cross-squad).
    cleared = 0
    repaired = 0

    def fix_pending(tie):
        nonlocal cleared, repaired
        if _tie_side_leader_id(item, tie.get('squad_a')) == squad_id:
            side_key = 'player_a'
        elif _tie_side_leader_id(item, tie.get('squad_b')) == squad_id:
            side_key = 'player_b'
        else:
            return
        for m in tie.get('matches', []):
            if m['played']:
                continue
            entity = m.get(side_key)
            if not entity or old_player_id not in entity.get('members', [entity.get('player_id')]):
                continue
            if cross_squad:
                _rebuild_entity_after_substitution(entity, old_player_id, new_player_id, new_player)
                repaired += 1
            else:
                m[side_key] = None
                cleared += 1

    for tie in (item.get('group_stage') or {}).get('ties', []):
        fix_pending(tie)
    for rnd in (item.get('knockout') or {}).get('rounds', []):
        for tie in rnd:
            fix_pending(tie)
    tpm = (item.get('knockout') or {}).get('third_place_match')
    if tpm:
        fix_pending(tpm)

    item.setdefault('squad_substitutions', []).append({
        'squad_id': squad_id, 'old_player_id': old_player_id, 'new_player_id': new_player_id,
        'at': datetime.now(timezone.utc).isoformat(), 'by': claims.get('email'),
        'pending_slots_cleared': cleared, 'pending_slots_repaired': repaired,
    })

    tournaments_table.put_item(Item=item)
    return _response(200, item)


def _build_group_stage(item):
    """Shared schedule-building logic, used both by generate_schedule (the
    first time, from squads_locked) and regenerate_schedule (an organizer
    fix-up re-run, from group_stage, before anything's been played).
    Mutates item in place (sets item['group_stage']). Returns None on
    success, or an error _response on failure - the caller is responsible
    for status transitions and persisting."""
    squad_ids = list((item.get('squads') or {}).keys())
    if len(squad_ids) < 2:
        return _response(400, {'error': 'need at least 2 squads to generate a schedule'})

    matches_per_tie = int(item['manual_draft']['group_matches_per_tie'])
    num_groups = int(item.get('manual_draft', {}).get('num_groups', 1))

    if num_groups <= 1:
        # Unchanged from the original design: one round-robin across every
        # squad, no elimination before the knockout (which is seeded by
        # combined standings) - this is still the default, and every
        # existing manual-draft tournament (created before num_groups
        # existed) behaves byte-identically since .get(..., 1) is 1 for them.
        item['group_stage'] = {'matches_per_tie': matches_per_tie, 'ties': build_tie_round_robin(squad_ids, matches_per_tie)}
    else:
        # Real separate groups (owner request, 2026-08-21): squads randomly
        # split into num_groups named groups (A, B, C...), round-robin only
        # within each group - mirrors the existing groups_then_knockout
        # legacy format's own subgroup/advance_per_group design as closely
        # as possible (see inject_tiebreakers_if_needed/advance_to_knockout)
        # rather than inventing a parallel mechanism.
        if num_groups > len(squad_ids):
            return _response(400, {'error': f'cannot split {len(squad_ids)} squads into {num_groups} groups'})
        if len(squad_ids) < num_groups * 2:
            # A group with only 1 squad in it has no one to play - no ties,
            # no matches, nothing on screen. Caught here (not just "more
            # groups than squads") after an owner hit exactly this: 4
            # squads split into 4 groups quietly produced 4 empty,
            # unplayable groups instead of an error.
            max_groups = len(squad_ids) // 2
            return _response(400, {'error': f'each group needs at least 2 squads to play any matches - {len(squad_ids)} '
                                             f'squads split into {num_groups} groups would leave at least one group with '
                                             f'only 1 squad. Use at most {max_groups} group{"s" if max_groups != 1 else ""}.'})
        group_names = list(ascii_uppercase[:num_groups])
        shuffled = list(squad_ids)
        random.shuffle(shuffled)
        groups = {n: [] for n in group_names}
        for idx, sid in enumerate(shuffled):
            groups[group_names[idx % num_groups]].append(sid)
        all_ties = []
        for name, members in groups.items():
            for tie in build_tie_round_robin(members, matches_per_tie):
                tie['group'] = name
                all_ties.append(tie)
        item['group_stage'] = {'matches_per_tie': matches_per_tie, 'ties': all_ties, 'groups': groups}
    return None


def _fill_cross_squad_match_players(item, ties):
    """Cross-squad group mode (owner request, 2026-08-21): a tie's two
    sides are fixed rep entities (a squad's pre-set pair/player - see
    set_squad_pairs), already fully known the instant the tie is built -
    there's no leader lineup to nominate match-by-match (pick_tie_player),
    so every match's players get filled in immediately here instead.
    Applies wherever cross-squad ties get built: the group stage itself,
    the first knockout round, later knockout rounds, and the third-place
    match. No-op (leaves matches with player_a/player_b=None, exactly as
    the regular squads-per-group mode does) for any tournament without rep
    entities, and for bye ties, which have no matches at all."""
    reps = item.get('reps') or {}
    if not reps:
        return
    for tie in ties:
        if tie.get('bye'):
            continue
        rep_a, rep_b = reps.get(tie.get('squad_a')), reps.get(tie.get('squad_b'))
        if not rep_a or not rep_b:
            continue
        for m in tie.get('matches', []):
            if m['player_a'] is None:
                m['player_a'] = {'player_id': rep_a['entity_id'], 'name': rep_a['name'],
                                  'members': list(rep_a['members']), 'member_ratings': list(rep_a['member_ratings'])}
            if m['player_b'] is None:
                m['player_b'] = {'player_id': rep_b['entity_id'], 'name': rep_b['name'],
                                  'members': list(rep_b['members']), 'member_ratings': list(rep_b['member_ratings'])}


def _build_cross_squad_group_stage(item):
    """Cross-squad group mode (owner request, 2026-08-21): instead of
    splitting WHOLE squads across groups, each squad contributes exactly
    one fixed rep unit (a pre-set doubles pair, or a solo player for
    singles - see set_squad_pairs) to EVERY group, so a group ends up
    with one rep from each squad rather than 2+ whole squads. Mutates
    item in place (sets item['reps'] and item['group_stage'], the latter
    tagged group_stage.cross_squad=True so the frontend/knockout/champion
    logic downstream know to resolve rep_id -> parent squad). Returns
    None on success, or an error _response on failure."""
    squads = item.get('squads') or {}
    squad_ids = list(squads.keys())
    if len(squad_ids) < 1:
        return _response(400, {'error': 'need at least 1 squad to generate cross-squad groups'})

    num_groups = int(item.get('manual_draft', {}).get('num_groups', 1))
    if num_groups < 1:
        return _response(400, {'error': 'num_groups must be at least 1'})

    match_type = item.get('manual_draft', {}).get('match_type', 'singles')
    expected_size = 2 if match_type == 'doubles' else 1

    for sid in squad_ids:
        pairs = squads[sid].get('pairs')
        if not pairs or len(pairs) != num_groups:
            return _response(400, {'error': f"squad '{squads[sid].get('name', sid)}' needs exactly {num_groups} pair"
                                             f"{'s' if num_groups != 1 else ''} set (one per group) before groups can be "
                                             f"generated - set its pairs first"})
        for pair in pairs:
            if not isinstance(pair, list) or len(pair) != expected_size:
                return _response(400, {'error': f"squad '{squads[sid].get('name', sid)}' has a pair of the wrong size "
                                                 f"for this {match_type} tournament (expected {expected_size} "
                                                 f"player{'s' if expected_size != 1 else ''} per pair)"})

    group_names = list(ascii_uppercase[:num_groups])
    groups = {n: [] for n in group_names}
    reps = {}

    for sid in squad_ids:
        squad = squads[sid]
        member_ratings = dict(zip(squad.get('members', []), squad.get('member_ratings', [])))
        rep_ids_for_squad = []
        for i, pair in enumerate(squad['pairs']):
            names = []
            for pid in pair:
                p = players_table.get_item(Key={'player_id': pid}).get('Item')
                names.append(p['name'] if p else pid)
            rep_id = f"{sid}::rep{i}"
            reps[rep_id] = {
                'entity_id': rep_id,
                'name': ' & '.join(names) if expected_size == 2 else names[0],
                'members': list(pair),
                'member_ratings': [member_ratings.get(pid, 1000) for pid in pair],
                'parent_squad_id': sid,
                'locked': True,
            }
            rep_ids_for_squad.append(rep_id)
        # Randomize which of this squad's reps lands in which group - a
        # squad's own reps are otherwise built in a fixed order (rep0,
        # rep1, ...), and always mapping rep0 -> Group A would be an
        # arbitrary, non-random assignment.
        random.shuffle(rep_ids_for_squad)
        for group_name, rep_id in zip(group_names, rep_ids_for_squad):
            groups[group_name].append(rep_id)

    matches_per_tie = int(item['manual_draft']['group_matches_per_tie'])
    all_ties = []
    for name, members in groups.items():
        for tie in build_tie_round_robin(members, matches_per_tie):
            tie['group'] = name
            all_ties.append(tie)

    item['reps'] = reps
    _fill_cross_squad_match_players(item, all_ties)
    item['group_stage'] = {'matches_per_tie': matches_per_tie, 'ties': all_ties, 'groups': groups, 'cross_squad': True}
    return None


def generate_schedule(tournament_id, event, claims):
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied
    if item.get('status') != 'squads_locked':
        return _response(400, {'error': 'squads must be locked before generating the schedule'})

    # Accepts an optional group_mode override, same as regenerate_schedule -
    # a tournament created before cross-squad group mode existed (or simply
    # created with the default 'squads' mode) can still switch to
    # 'cross_squad' right here at first-generation time, once its squads
    # have set their pairs, without a separate round-trip through
    # regenerate_schedule.
    body = json.loads(event.get('body') or '{}')
    if 'group_mode' in body:
        new_group_mode = body['group_mode']
        if new_group_mode not in ('squads', 'cross_squad'):
            return _response(400, {'error': "group_mode must be 'squads' or 'cross_squad'"})
        item.setdefault('manual_draft', {})['group_mode'] = new_group_mode

    if item.get('manual_draft', {}).get('group_mode') == 'cross_squad':
        build_err = _build_cross_squad_group_stage(item)
    else:
        build_err = _build_group_stage(item)
    if build_err:
        return build_err

    item['status'] = 'group_stage'
    tournaments_table.put_item(Item=item)
    return _response(200, item)


def regenerate_schedule(tournament_id, event, claims):
    """Organizer repair action: re-run schedule generation for a tournament
    already in the group stage, optionally changing num_groups/
    advance_per_group first. Exists because a group-count mistake only
    becomes obvious once real squad names are on screen (e.g. num_groups
    equal to the squad count silently produced one-squad, zero-match
    groups) - the organizer needs a way to fix that without re-running the
    whole leader/pool/auction process, since squads are already locked and
    this leaves them untouched. Only allowed while genuinely nothing in the
    current group stage has been played yet, so a real match result is
    never silently discarded."""
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied
    if item.get('status') != 'group_stage':
        return _response(400, {'error': 'can only regenerate the schedule while still in the group stage'})

    existing_ties = (item.get('group_stage') or {}).get('ties', [])
    if any(m.get('played') for t in existing_ties for m in (t.get('matches') or [])):
        return _response(400, {'error': 'cannot regenerate - some matches in the current schedule have already been played'})

    body = json.loads(event.get('body') or '{}')
    manual_draft = item.setdefault('manual_draft', {})
    if 'num_groups' in body:
        try:
            new_num_groups = int(body['num_groups'])
        except (TypeError, ValueError):
            return _response(400, {'error': 'num_groups must be a number'})
        if new_num_groups < 1:
            return _response(400, {'error': 'num_groups must be at least 1'})
        manual_draft['num_groups'] = new_num_groups
    if 'advance_per_group' in body:
        try:
            new_advance = int(body['advance_per_group'])
        except (TypeError, ValueError):
            return _response(400, {'error': 'advance_per_group must be a number'})
        if new_advance < 1:
            return _response(400, {'error': 'advance_per_group must be at least 1'})
        manual_draft['advance_per_group'] = new_advance
    if 'group_mode' in body:
        new_group_mode = body['group_mode']
        if new_group_mode not in ('squads', 'cross_squad'):
            return _response(400, {'error': "group_mode must be 'squads' or 'cross_squad'"})
        manual_draft['group_mode'] = new_group_mode
        if new_group_mode == 'squads':
            item.pop('reps', None)  # switching back out of cross-squad mode - stale rep entities would otherwise linger unused

    if manual_draft.get('group_mode') == 'cross_squad':
        build_err = _build_cross_squad_group_stage(item)
    else:
        build_err = _build_group_stage(item)
    if build_err:
        return build_err

    tournaments_table.put_item(Item=item)
    return _response(200, item)


def pick_tie_player(tournament_id, event, claims):
    """A leader nominates which of their own squad's members plays a given
    match slot within a tie. Originally leader-only by design, but the
    owner asked for the organizer to be able to set a tie's lineup too
    (in consultation with the leader, e.g. over chat/in person) - same
    "the app shouldn't be a hard stop if not everyone has it open" spirit
    as organizer-assign during the auction. A leader still nominates for
    their own squad exactly as before, with no body changes needed; the
    organizer additionally needs to say which of the tie's two squads
    they're nominating for, via `squad_id` in the body (there's no
    "caller's own squad" to infer it from)."""
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    if item.get('status') not in ('group_stage', 'knockout', 'completed'):
        # 'completed' stays allowed so the third-place match can still be
        # nominated/played after the final itself is already decided -
        # matches record_knockout_tie_score's own status check below.
        return _response(400, {'error': 'this tournament has no active ties right now'})

    body = json.loads(event.get('body') or '{}')
    tie_id = body.get('tie_id')
    match_index = body.get('match_index')
    # Singles tournaments send a single player_id; doubles ones send a
    # player_ids pair (see manual_draft.match_type, set at creation). Accept
    # either shape here and normalize to a list so the validation below is
    # written once, not duplicated per match_type.
    if body.get('player_ids') is not None:
        player_ids = body.get('player_ids')
    elif body.get('player_id'):
        player_ids = [body.get('player_id')]
    else:
        player_ids = None
    if not tie_id or match_index is None or not player_ids or not isinstance(player_ids, list):
        return _response(400, {'error': 'tie_id, match_index, and player_id (or player_ids for doubles) are required'})

    tie = _find_tie(item, tie_id)
    if not tie:
        return _response(404, {'error': 'tie not found'})
    try:
        match_index = int(match_index)
    except (TypeError, ValueError):
        return _response(400, {'error': 'match_index must be a number'})
    if match_index < 0 or match_index >= len(tie['matches']):
        return _response(400, {'error': 'invalid match_index'})
    match = tie['matches'][match_index]
    if match['played']:
        return _response(400, {'error': 'this match is already decided - the lineup can no longer change'})

    caller_pid = claims.get('custom:player_id')
    if caller_pid == tie.get('squad_a'):
        side, acting_squad_id = 'a', tie.get('squad_a')
    elif caller_pid == tie.get('squad_b'):
        side, acting_squad_id = 'b', tie.get('squad_b')
    elif _authorize_tournament_organizer(item, claims) is None:
        acting_squad_id = body.get('squad_id')
        if acting_squad_id == tie.get('squad_a'):
            side = 'a'
        elif acting_squad_id == tie.get('squad_b'):
            side = 'b'
        else:
            return _response(400, {'error': "as organizer, include squad_id (one of this tie's two squads) "
                                             "to say which side's lineup you're setting"})
    else:
        return _response(403, {'error': "only this tie's two squad leaders, or the organizer, can set the lineup"})

    match_type = item.get('manual_draft', {}).get('match_type', 'singles')
    expected_size = 2 if match_type == 'doubles' else 1
    if len(player_ids) != expected_size:
        return _response(400, {'error': f'this is a {match_type} tournament - nominate exactly {expected_size} '
                                         f'player{"s" if expected_size > 1 else ""}'})
    if len(set(player_ids)) != len(player_ids):
        return _response(400, {'error': 'the same player cannot be nominated twice for one match'})

    squad = (item.get('squads') or {}).get(acting_squad_id)
    squad_members = squad.get('members', []) if squad else []
    not_in_squad = [pid for pid in player_ids if pid not in squad_members]
    if not_in_squad:
        return _response(400, {'error': f'these players are not members of that squad: {not_in_squad}'})

    players = [players_table.get_item(Key={'player_id': pid}).get('Item') for pid in player_ids]
    if match_type == 'doubles':
        p1, p2 = players
        entity = {
            'player_id': str(uuid.uuid4()),
            'name': f"{p1['name'] if p1 else player_ids[0]} & {p2['name'] if p2 else player_ids[1]}",
            'members': player_ids,
            'member_ratings': [(p.get('rating', 1000) if p else 1000) for p in players],
        }
    else:
        p = players[0]
        entity = {'player_id': player_ids[0], 'name': p['name'] if p else player_ids[0],
                   'members': [player_ids[0]], 'member_ratings': [p.get('rating', 1000) if p else 1000]}
    if side == 'a':
        match['player_a'] = entity
    else:
        match['player_b'] = entity

    tournaments_table.put_item(Item=item)
    return _response(200, _hide_pool_auction_from_non_organizer(item, claims))


def _generate_knockout_from_group_stage(item):
    standings = compute_squad_standings(item)
    seeded_squad_ids = [s['squad_id'] for s in standings]
    md = item.get('manual_draft', {})
    matches_per_tie = int(md['knockout_matches_per_tie'])
    final_matches_per_tie = int(md.get('final_matches_per_tie', matches_per_tie))
    third_place_matches_per_tie = int(md.get('third_place_matches_per_tie', matches_per_tie))
    # If this first round is already down to one tie (e.g. only 2 squads
    # total), it IS the final - build it with the final's own match count.
    first_round_matches_per_tie = final_matches_per_tie if len(seeded_squad_ids) <= 2 else matches_per_tie
    item['knockout'] = {
        'matches_per_tie': matches_per_tie,
        'final_matches_per_tie': final_matches_per_tie,
        'third_place_matches_per_tie': third_place_matches_per_tie,
        'rounds': [build_knockout_tie_round(seeded_squad_ids, first_round_matches_per_tie)],
    }
    item['status'] = 'knockout'


def _inject_group_tiebreakers_if_needed(item):
    """Real-separate-groups sibling of the legacy groups_then_knockout
    format's inject_tiebreakers_if_needed: if two squads are level on both
    ties_won AND point_diff right at their group's advance_per_group
    boundary, append one more tie between exactly those two squads instead
    of guessing who advances. Returns True if any group still has one
    pending (so advancement should wait)."""
    groups = item['group_stage']['groups']
    advance_n = int(item.get('manual_draft', {}).get('advance_per_group', 2))
    matches_per_tie = item['group_stage']['matches_per_tie']
    needs_tiebreaker = False

    for name, members in groups.items():
        group_ties = [t for t in item['group_stage']['ties'] if t.get('group') == name]
        if any(t.get('tiebreaker') and not t.get('decided') for t in group_ties):
            needs_tiebreaker = True
            continue

        standings = compute_squad_standings(item, squad_ids=members)
        if len(standings) <= advance_n:
            continue

        boundary_a, boundary_b = standings[advance_n - 1], standings[advance_n]
        tied = (boundary_a['ties_won'] == boundary_b['ties_won'] and boundary_a['point_diff'] == boundary_b['point_diff'])
        if not tied:
            continue

        pair_key = {boundary_a['squad_id'], boundary_b['squad_id']}
        already_resolved = any(
            t.get('tiebreaker') and t.get('decided') and {t.get('squad_a'), t.get('squad_b')} == pair_key
            for t in group_ties
        )
        if already_resolved:
            continue

        tie = build_tie(boundary_a['squad_id'], boundary_b['squad_id'], matches_per_tie)
        tie['group'] = name
        tie['tiebreaker'] = True
        item['group_stage']['ties'].append(tie)
        needs_tiebreaker = True

    return needs_tiebreaker


def _advance_squads_to_knockout_from_groups(item):
    """Real-separate-groups sibling of the legacy groups_then_knockout
    format's advance_to_knockout: top advance_per_group squads from each
    group qualify.

    Used to shuffle qualifiers into the bracket randomly, with a
    best-effort swap to avoid two squads from the SAME group meeting in
    round one. Changed to a deterministic pairing (owner report,
    2026-08-23, live event: "the group A qualifies went against group B
    qualifies and similarly c with D") - a real live bracket is always
    adjacent groups in name order (A-B, C-D, ...), never a random draw, so
    the app's auto-generated knockout draw had no guarantee of matching
    what organizers already played out live. Sorts groups by name and
    pairs them consecutively instead of shuffling. The same-group-
    avoidance swap below still matters when advance_per_group > 1 (two
    qualifiers from one group can land adjacent in that case) - it's just
    never triggered by group order alone anymore."""
    groups = item['group_stage']['groups']
    advance_n = int(item.get('manual_draft', {}).get('advance_per_group', 2))
    qualifiers = []
    for name in sorted(groups.keys()):
        standings = compute_squad_standings(item, squad_ids=groups[name])
        for s in standings[:advance_n]:
            qualifiers.append({'squad_id': s['squad_id'], 'group': name})

    for i in range(0, len(qualifiers) - 1, 2):
        if qualifiers[i]['group'] == qualifiers[i + 1]['group']:
            for j in range(i + 2, len(qualifiers)):
                if qualifiers[j]['group'] != qualifiers[i]['group']:
                    qualifiers[i + 1], qualifiers[j] = qualifiers[j], qualifiers[i + 1]
                    break

    seeded_squad_ids = [q['squad_id'] for q in qualifiers]
    md = item.get('manual_draft', {})
    matches_per_tie = int(md['knockout_matches_per_tie'])
    final_matches_per_tie = int(md.get('final_matches_per_tie', matches_per_tie))
    third_place_matches_per_tie = int(md.get('third_place_matches_per_tie', matches_per_tie))
    # If this first round is already down to one tie (e.g. only 2 groups,
    # 1 qualifier each), it IS the final - build it with the final's own
    # match count rather than the semifinal-tier default.
    first_round_matches_per_tie = final_matches_per_tie if len(seeded_squad_ids) <= 2 else matches_per_tie
    first_round = build_knockout_tie_round(seeded_squad_ids, first_round_matches_per_tie)
    _fill_cross_squad_match_players(item, first_round)  # no-op unless this is cross-squad group mode
    item['knockout'] = {
        'matches_per_tie': matches_per_tie,
        'final_matches_per_tie': final_matches_per_tie,
        'third_place_matches_per_tie': third_place_matches_per_tie,
        'rounds': [first_round],
    }
    item['status'] = 'knockout'


def record_group_tie_score(tournament_id, event, claims):
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    if item.get('status') != 'group_stage':
        return _response(400, {'error': 'tournament is not in group stage'})

    body = json.loads(event.get('body') or '{}')
    tie_id = body.get('tie_id')
    match_index = body.get('match_index')
    score_a = body.get('score_a')
    score_b = body.get('score_b')
    override = bool(body.get('override'))
    point_log = body.get('point_log')
    if not tie_id or match_index is None or score_a is None or score_b is None:
        return _response(400, {'error': 'tie_id, match_index, score_a, score_b are required'})

    tie = next((t for t in item.get('group_stage', {}).get('ties', []) if t['tie_id'] == tie_id), None)
    if not tie:
        return _response(404, {'error': 'tie not found'})

    denied = _authorize_tie_scorer(item, tie, claims)
    if denied:
        return denied

    try:
        _score_tie_match(item, tie, int(match_index), int(score_a), int(score_b), override, point_log, 'group')
    except ValueError as e:
        return _response(400, {'error': str(e)})

    _after_group_tie_resolved(item)
    tournaments_table.put_item(Item=item)
    return _response(200, _hide_pool_auction_from_non_organizer(item, claims))


def _after_group_tie_resolved(item):
    """Shared by every route that can make a group tie `decided` (score,
    cancel, forfeit): once every group tie is resolved one way or another,
    either injects a boundary tiebreaker or advances to knockout - exactly
    the block record_group_tie_score always ran inline, now reused by the
    cancel/forfeit routes below too."""
    if all(t.get('decided') for t in item['group_stage']['ties']):
        if item['group_stage'].get('groups'):
            if not _inject_group_tiebreakers_if_needed(item):
                _advance_squads_to_knockout_from_groups(item)
        else:
            _generate_knockout_from_group_stage(item)


def cancel_group_tie_match(tournament_id, event, claims):
    """Organizer-only: administratively cancels one group match that can
    never be played (owner report, 2026-08-23: players unavailable on both
    sides for 2 group matches at a live event, with no replacement
    possible) - counts toward neither side, but stops blocking
    advancement. See _cancel_tie_match/_update_tie_progress."""
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    if item.get('status') != 'group_stage':
        return _response(400, {'error': 'tournament is not in group stage'})

    body = json.loads(event.get('body') or '{}')
    tie_id = body.get('tie_id')
    match_index = body.get('match_index')
    if not tie_id or match_index is None:
        return _response(400, {'error': 'tie_id and match_index are required'})

    tie = next((t for t in item.get('group_stage', {}).get('ties', []) if t['tie_id'] == tie_id), None)
    if not tie:
        return _response(404, {'error': 'tie not found'})

    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied

    try:
        _cancel_tie_match(tie, int(match_index))
    except ValueError as e:
        return _response(400, {'error': str(e)})

    _after_group_tie_resolved(item)
    tournaments_table.put_item(Item=item)
    return _response(200, _hide_pool_auction_from_non_organizer(item, claims))


def forfeit_group_tie_match(tournament_id, event, claims):
    """Organizer-only sibling of cancel_group_tie_match: one side didn't
    show up, so the other side is awarded the match win outright (owner
    request, 2026-08-23). Body: {tie_id, match_index, forfeited_by: 'a'|'b'}."""
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    if item.get('status') != 'group_stage':
        return _response(400, {'error': 'tournament is not in group stage'})

    body = json.loads(event.get('body') or '{}')
    tie_id = body.get('tie_id')
    match_index = body.get('match_index')
    forfeited_by = body.get('forfeited_by')
    if not tie_id or match_index is None or not forfeited_by:
        return _response(400, {'error': 'tie_id, match_index and forfeited_by are required'})

    tie = next((t for t in item.get('group_stage', {}).get('ties', []) if t['tie_id'] == tie_id), None)
    if not tie:
        return _response(404, {'error': 'tie not found'})

    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied

    try:
        _forfeit_tie_match(tie, int(match_index), forfeited_by)
    except ValueError as e:
        return _response(400, {'error': str(e)})

    _after_group_tie_resolved(item)
    tournaments_table.put_item(Item=item)
    return _response(200, _hide_pool_auction_from_non_organizer(item, claims))


def _advance_knockout_ties_if_round_complete(item):
    """Mirrors record_knockout_score's round-advancement + third-place-
    match auto-creation (L1471-1502-ish, pre-Phase-C line numbers), but for
    ties instead of plain matches."""
    rounds = item['knockout']['rounds']
    current_round = rounds[-1]
    if not all(t.get('decided') for t in current_round):
        return

    if len(current_round) == 1:
        item['status'] = 'completed'
        winner_id = current_round[0]['winner_squad_id']
        # Cross-squad group mode (owner request, 2026-08-21): the knockout
        # winner is a rep entity (a specific pre-fixed pair), not a real
        # squad - resolve it back to its parent squad so the champion
        # banner still names a squad, and separately keep the winning
        # rep_id for display of exactly which pair won it.
        item['champion_squad_id'] = _tie_side_leader_id(item, winner_id)
        if winner_id in (item.get('reps') or {}):
            item['champion_rep_id'] = winner_id
        return

    matches_per_tie = int(item['knockout'].get('matches_per_tie', 1))
    winners = [t['winner_squad_id'] for t in current_round]
    # The round we're about to build is the final exactly when it comes out
    # to a single tie - use the final's own match count there instead of
    # the semifinal-tier default (owner request, 2026-08-23: separate
    # "how many sets" per semis/final/third place).
    next_round_matches_per_tie = (
        int(item['knockout'].get('final_matches_per_tie', matches_per_tie))
        if len(winners) == 1 else matches_per_tie
    )
    next_round = build_knockout_tie_round(winners, next_round_matches_per_tie)
    _fill_cross_squad_match_players(item, next_round)  # no-op unless this is cross-squad group mode
    rounds.append(next_round)

    if len(current_round) == 2 and 'third_place_match' not in item['knockout']:
        losers = []
        for t in current_round:
            losers.append(t['squad_b'] if t['winner_squad_id'] == t['squad_a'] else t['squad_a'])
        third_place_matches_per_tie = int(item['knockout'].get('third_place_matches_per_tie', matches_per_tie))
        third_place = build_tie(losers[0], losers[1], third_place_matches_per_tie)
        _fill_cross_squad_match_players(item, [third_place])  # no-op unless this is cross-squad group mode
        item['knockout']['third_place_match'] = third_place


def record_knockout_tie_score(tournament_id, event, claims):
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    if item.get('status') not in ('knockout', 'completed'):
        return _response(400, {'error': 'tournament is not in knockout stage'})

    body = json.loads(event.get('body') or '{}')
    tie_id = body.get('tie_id')
    match_index = body.get('match_index')
    score_a = body.get('score_a')
    score_b = body.get('score_b')
    override = bool(body.get('override'))
    point_log = body.get('point_log')
    if not tie_id or match_index is None or score_a is None or score_b is None:
        return _response(400, {'error': 'tie_id, match_index, score_a, score_b are required'})

    tie = _find_tie(item, tie_id)
    if not tie:
        return _response(404, {'error': 'tie not found'})
    is_third_place = (item.get('knockout') or {}).get('third_place_match') is tie

    denied = _authorize_tie_scorer(item, tie, claims)
    if denied:
        return denied

    try:
        _score_tie_match(item, tie, int(match_index), int(score_a), int(score_b), override, point_log,
                          'third_place' if is_third_place else 'knockout')
    except ValueError as e:
        return _response(400, {'error': str(e)})

    if not is_third_place and tie.get('decided'):
        _advance_knockout_ties_if_round_complete(item)

    tournaments_table.put_item(Item=item)
    return _response(200, _hide_pool_auction_from_non_organizer(item, claims))


def cancel_knockout_tie_match(tournament_id, event, claims):
    """Organizer-only knockout/third-place sibling of cancel_group_tie_match
    (owner request, 2026-08-23) - see _cancel_tie_match/_update_tie_progress."""
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    if item.get('status') not in ('knockout', 'completed'):
        return _response(400, {'error': 'tournament is not in knockout stage'})

    body = json.loads(event.get('body') or '{}')
    tie_id = body.get('tie_id')
    match_index = body.get('match_index')
    if not tie_id or match_index is None:
        return _response(400, {'error': 'tie_id and match_index are required'})

    tie = _find_tie(item, tie_id)
    if not tie:
        return _response(404, {'error': 'tie not found'})
    is_third_place = (item.get('knockout') or {}).get('third_place_match') is tie

    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied

    try:
        _cancel_tie_match(tie, int(match_index))
    except ValueError as e:
        return _response(400, {'error': str(e)})

    if not is_third_place and tie.get('decided'):
        _advance_knockout_ties_if_round_complete(item)

    tournaments_table.put_item(Item=item)
    return _response(200, _hide_pool_auction_from_non_organizer(item, claims))


def forfeit_knockout_tie_match(tournament_id, event, claims):
    """Organizer-only knockout/third-place sibling of forfeit_group_tie_match
    (owner request, 2026-08-23). Body: {tie_id, match_index, forfeited_by: 'a'|'b'}."""
    item, err = _draft_get_tournament(tournament_id)
    if err:
        return err
    if item.get('status') not in ('knockout', 'completed'):
        return _response(400, {'error': 'tournament is not in knockout stage'})

    body = json.loads(event.get('body') or '{}')
    tie_id = body.get('tie_id')
    match_index = body.get('match_index')
    forfeited_by = body.get('forfeited_by')
    if not tie_id or match_index is None or not forfeited_by:
        return _response(400, {'error': 'tie_id, match_index and forfeited_by are required'})

    tie = _find_tie(item, tie_id)
    if not tie:
        return _response(404, {'error': 'tie not found'})
    is_third_place = (item.get('knockout') or {}).get('third_place_match') is tie

    denied = _authorize_tournament_organizer(item, claims)
    if denied:
        return denied

    try:
        _forfeit_tie_match(tie, int(match_index), forfeited_by)
    except ValueError as e:
        return _response(400, {'error': str(e)})

    if not is_third_place and tie.get('decided'):
        _advance_knockout_ties_if_round_complete(item)

    tournaments_table.put_item(Item=item)
    return _response(200, _hide_pool_auction_from_non_organizer(item, claims))


# ---------- reads ----------

def list_tournaments(event):
    params = event.get('queryStringParameters') or {}
    group_id = params.get('group_id')
    items = _scan_all(tournaments_table)
    if group_id:
        items = [i for i in items if i.get('group_id') == group_id]
    result = [
        {
            'tournament_id': i['tournament_id'],
            'name': i['name'],
            'group_id': i['group_id'],
            'format': i['format'],
            'match_type': i.get('match_type', 'singles'),
            'points_to_win': i.get('points_to_win', 21),
            'best_of': i.get('best_of', 1),
            'status': i['status'],
            'created_at': i['created_at']
        }
        for i in items
    ]
    result.sort(key=lambda i: i['created_at'], reverse=True)
    return _response(200, {'tournaments': result})


def _redact_pool_auction_detail(item):
    """GET /tournaments/{id} is unauthenticated - literally anyone browsing
    tournaments can call it, so pool assignments and auction budgets/bids
    can never be safely returned here (owner request: keep both restricted
    to the organizer, and to leaders only while their phase is live - see
    _authorize_pool_auction_viewer). Replaces both with a minimal stub and
    returns a NEW item dict - deliberately does not mutate the dict it was
    given, since that dict may be the exact object a DynamoDB SDK stub (or,
    in principle, a future caching layer) hands back by reference rather
    than a fresh copy; mutating it in place would risk silently wiping the
    real pool/auction data out from under any other in-flight read. The
    Cognito-gated GET /tournament-draft/{id} (get_draft_sensitive_detail)
    is the only route that ever returns the real thing."""
    redacted = dict(item)
    pools = item.get('pools')
    if pools is not None:
        redacted['pools'] = {'locked': pools.get('locked', False), 'redacted': True}
    draft = item.get('draft')
    if draft is not None:
        redacted['draft'] = {'status': draft.get('status'), 'redacted': True}
    return redacted


def _hide_pool_auction_from_non_organizer(item, claims):
    """pick_tie_player/record_group_tie_score/record_knockout_tie_score are
    reachable by a tie's own leader (not just the organizer) - but only
    ever once the pool/auction phase has already passed (their own status
    checks guarantee that). Per _authorize_pool_auction_viewer, a leader's
    access to pools/draft expires the moment that phase passes, so their
    action responses here must not hand the real thing back either -
    otherwise the browser's network tab would leak it even though no UI
    ever renders it. Organizer callers still get it back unredacted."""
    if _authorize_tournament_organizer(item, claims) is None:
        return item
    return _redact_pool_auction_detail(item)


def get_tournament(tournament_id):
    item = tournaments_table.get_item(Key={'tournament_id': tournament_id}).get('Item')
    if not item:
        return _response(404, {'error': 'tournament not found'})
    if 'subgroups' in item:
        item['standings'] = compute_all_standings(item)
    if item.get('format') == 'manual_draft' and 'group_stage' in item:
        # Read-time-only, same as `standings` above - never persisted, so
        # these can't drift out of sync with the ties/matches they're
        # computed from. Cross-squad group mode (owner request,
        # 2026-08-21): the overall table needs to roll each squad's several
        # rep entities back up into one row per real squad - the plain
        # per-rep table isn't meaningful on its own since two reps of the
        # same squad never play each other.
        if (item.get('group_stage') or {}).get('cross_squad'):
            item['squad_standings'] = compute_squad_standings_by_parent(item)
        else:
            item['squad_standings'] = compute_squad_standings(item)
        groups = (item.get('group_stage') or {}).get('groups')
        if groups:
            # Real separate groups (owner request, 2026-08-21): a combined
            # table across every squad isn't meaningful while groups haven't
            # played each other, so also attach one standings table per
            # group - the frontend prefers this when present.
            item['group_standings'] = {name: compute_squad_standings(item, squad_ids=members)
                                        for name, members in groups.items()}
        item['player_tournament_stats'] = compute_player_tournament_scores(item)
        if item.get('status') == 'group_stage':
            if groups:
                group_projection = compute_group_stage_projection(item)
                if group_projection:
                    item['group_stage_projection'] = group_projection
            else:
                projected = compute_projected_knockout(item)
                if projected:
                    item['projected_knockout'] = projected
    if item.get('format') == 'manual_draft':
        item = _redact_pool_auction_detail(item)
    return _response(200, item)


def recompute_all_ratings():
    """Elo is path-dependent - each match's rating change depends on the
    ratings at that exact moment, which depend on everything before it.
    Simply subtracting a delta when a match is deleted isn't mathematically
    safe if anything happened after it. The only fully correct fix: reset
    everyone to 1000 and replay every remaining match in chronological
    order, recomputing from scratch - including replaying each pairing's
    K-factor exactly as it would have been at that point in time."""
    players = _scan_all(players_table)
    current_ratings = {p['player_id']: 1000.0 for p in players}
    pairing_counts = {}  # frozenset({p1,p2}) -> matches played together so far

    matches = _scan_all(matches_table)
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


def delete_tournament(tournament_id, event):
    """Deletes this tournament AND every match record tagged with its
    tournament_id (e.g. from a test tournament you're cleaning up), then
    recomputes every player's rating from scratch off the remaining match
    history - see recompute_all_ratings() for why a simple delta-subtract
    isn't safe."""
    body = json.loads(event.get('body') or '{}')
    if body.get('confirm') != CONFIRMATION_CODE:
        return _response(400, {'error': "confirmation code is missing or incorrect"})

    existing = tournaments_table.get_item(Key={'tournament_id': tournament_id}).get('Item')
    if not existing:
        return _response(404, {'error': 'tournament not found'})

    related_matches = _scan_all(matches_table)
    deleted_match_count = 0
    for m in related_matches:
        if m.get('tournament_id') == tournament_id:
            matches_table.delete_item(Key={'match_id': m['match_id']})
            deleted_match_count += 1

    tournaments_table.delete_item(Key={'tournament_id': tournament_id})

    if deleted_match_count > 0:
        recompute_all_ratings()

    return _response(200, {
        'deleted': tournament_id,
        'name': existing.get('name'),
        'matches_deleted': deleted_match_count,
        'note': 'Player ratings were recomputed from scratch using the remaining match history.' if deleted_match_count > 0 else 'No matches were attached to this tournament.'
    })


def compute_standings(fixtures, entities):
    stats = {
        e['player_id']: {
            'player_id': e['player_id'], 'name': e['name'],
            'wins': 0, 'losses': 0, 'points_won': 0, 'points_lost': 0
        }
        for e in entities
    }
    for f in fixtures:
        if not f['played'] or f.get('bye'):
            continue
        a_id = f['player_a']['player_id']
        b_id = f['player_b']['player_id']
        total_a = sum(g['score_a'] for g in f.get('games', []))
        total_b = sum(g['score_b'] for g in f.get('games', []))
        stats[a_id]['points_won'] += total_a
        stats[a_id]['points_lost'] += total_b
        stats[b_id]['points_won'] += total_b
        stats[b_id]['points_lost'] += total_a
        if f['winner_id'] == a_id:
            stats[a_id]['wins'] += 1
            stats[b_id]['losses'] += 1
        elif f['winner_id'] == b_id:
            stats[b_id]['wins'] += 1
            stats[a_id]['losses'] += 1
    standings = list(stats.values())
    for s in standings:
        s['point_diff'] = s['points_won'] - s['points_lost']
    standings.sort(key=lambda s: (-s['wins'], -s['point_diff']))
    return standings


def compute_all_standings(item):
    return {name: compute_standings(sg['fixtures'], sg['members']) for name, sg in item.get('subgroups', {}).items()}


# ---------- group stage scoring ----------

def _submit_game(fixture, score_a, score_b, best_of, target=21, override=False):
    """Append one game's score to a fixture/match. Returns True if the match is now decided.
    With override=True, skips the strict win-by-2-at-target validation - for
    real matches that ended at a different point total than the tournament's
    configured rules (called early, house rules, etc). A tie still isn't a
    valid result either way, since there's no way to declare a winner."""
    if override:
        if score_a == score_b:
            raise ValueError('scores cannot be tied - a winner is required')
    elif not _is_valid_completed_game(score_a, score_b, target):
        cap = target + 9
        raise ValueError(f'invalid game score: must be won by 2 at {target}+ points, or reach the hard cap of {cap}')

    fixture['games'].append({'score_a': score_a, 'score_b': score_b})
    if score_a > score_b:
        fixture['games_won_a'] += 1
    else:
        fixture['games_won_b'] += 1

    needed_wins = (best_of // 2) + 1
    if fixture['games_won_a'] >= needed_wins or fixture['games_won_b'] >= needed_wins:
        a_id = fixture['player_a']['player_id']
        b_id = fixture['player_b']['player_id']
        fixture['winner_id'] = a_id if fixture['games_won_a'] > fixture['games_won_b'] else b_id
        fixture['played'] = True
        return True
    return False


def record_group_score(tournament_id, event):
    body = json.loads(event.get('body') or '{}')
    subgroup = body.get('subgroup')
    fixture_id = body.get('fixture_id')
    score_a = body.get('score_a')
    score_b = body.get('score_b')
    override = bool(body.get('override'))
    point_log = body.get('point_log')

    if not subgroup or not fixture_id or score_a is None or score_b is None:
        return _response(400, {'error': 'subgroup, fixture_id, score_a, score_b are required'})

    score_a, score_b = int(score_a), int(score_b)

    item = tournaments_table.get_item(Key={'tournament_id': tournament_id}).get('Item')
    if not item:
        return _response(404, {'error': 'tournament not found'})
    if item.get('status') != 'group_stage':
        return _response(400, {'error': 'tournament is not in group stage'})

    sg = item['subgroups'].get(subgroup)
    if not sg:
        return _response(404, {'error': 'subgroup not found'})

    fixture = next((f for f in sg['fixtures'] if f['fixture_id'] == fixture_id), None)
    if not fixture:
        return _response(404, {'error': 'fixture not found'})
    if fixture['played']:
        return _response(400, {'error': 'this fixture is already decided'})

    best_of = item.get('best_of', 1)
    target = item.get('points_to_win', 21)
    try:
        decided = _submit_game(fixture, score_a, score_b, best_of, target, override)
    except ValueError as e:
        return _response(400, {'error': str(e)})

    if decided:
        total_a = sum(g['score_a'] for g in fixture['games'])
        total_b = sum(g['score_b'] for g in fixture['games'])
        winner = 'A' if fixture['games_won_a'] > fixture['games_won_b'] else 'B'
        update_elo_and_log(item.get('match_type', 'singles'), fixture['player_a'], fixture['player_b'],
                            total_a, total_b, item['group_id'], tournament_id, 'group',
                            winner_override=winner, games=fixture['games'], point_log=point_log)

        all_played = all(f['played'] for sg2 in item['subgroups'].values() for f in sg2['fixtures'])
        if all_played:
            if not inject_tiebreakers_if_needed(item):
                advance_to_knockout(item)

    tournaments_table.put_item(Item=item)
    return _response(200, item)


def inject_tiebreakers_if_needed(item):
    """Checks each subgroup for a genuine tie (same wins AND point_diff) at
    the qualifying boundary. If found, appends an unplayed tiebreaker
    fixture between exactly those two teams instead of advancing to
    knockout. Returns True if any subgroup still needs a tiebreaker played
    (meaning advancement should wait)."""
    advance_n = item.get('advance_per_group', 2)
    needs_tiebreaker = False

    for sg in item['subgroups'].values():
        # already has one pending (unplayed) - waiting on it, don't add another
        if any(f.get('tiebreaker') and not f['played'] for f in sg['fixtures']):
            needs_tiebreaker = True
            continue

        standings = compute_standings(sg['fixtures'], sg['members'])
        if len(standings) <= advance_n:
            continue

        boundary_a = standings[advance_n - 1]
        boundary_b = standings[advance_n]
        tied = (boundary_a['wins'] == boundary_b['wins'] and boundary_a['point_diff'] == boundary_b['point_diff'])
        if not tied:
            continue

        pair_key = {boundary_a['player_id'], boundary_b['player_id']}
        already_resolved = any(
            f.get('tiebreaker') and f['played'] and
            {f['player_a']['player_id'], f['player_b']['player_id']} == pair_key
            for f in sg['fixtures']
        )
        if already_resolved:
            continue

        entity_by_id = {e['player_id']: e for e in sg['members']}
        sg['fixtures'].append({
            'fixture_id': str(uuid.uuid4()),
            'player_a': entity_by_id[boundary_a['player_id']],
            'player_b': entity_by_id[boundary_b['player_id']],
            'games': [],
            'games_won_a': 0,
            'games_won_b': 0,
            'played': False,
            'winner_id': None,
            'tiebreaker': True
        })
        needs_tiebreaker = True

    return needs_tiebreaker


def advance_to_knockout(item):
    advance_per_group = item.get('advance_per_group', 2)
    qualifiers = []
    for name, sg in item['subgroups'].items():
        standings = compute_standings(sg['fixtures'], sg['members'])
        entity_by_id = {e['player_id']: e for e in sg['members']}
        for rank, s in enumerate(standings[:advance_per_group]):
            entity = entity_by_id[s['player_id']]
            qualifiers.append({'entity': entity, 'subgroup': name})

    random.shuffle(qualifiers)
    for i in range(0, len(qualifiers) - 1, 2):
        if qualifiers[i]['subgroup'] == qualifiers[i + 1]['subgroup']:
            for j in range(i + 2, len(qualifiers)):
                if qualifiers[j]['subgroup'] != qualifiers[i]['subgroup']:
                    qualifiers[i + 1], qualifiers[j] = qualifiers[j], qualifiers[i + 1]
                    break

    entities = [q['entity'] for q in qualifiers]
    item['knockout'] = {'rounds': [build_knockout_round(entities)]}
    item['status'] = 'knockout'


# ---------- knockout scoring ----------

def record_knockout_score(tournament_id, event):
    body = json.loads(event.get('body') or '{}')
    round_index = body.get('round_index')
    match_index = body.get('match_index')
    third_place = bool(body.get('third_place'))
    score_a = body.get('score_a')
    score_b = body.get('score_b')
    override = bool(body.get('override'))
    point_log = body.get('point_log')

    if score_a is None or score_b is None:
        return _response(400, {'error': 'score_a and score_b are required'})
    if not third_place and (round_index is None or match_index is None):
        return _response(400, {'error': 'round_index and match_index are required (or set third_place: true)'})

    score_a, score_b = int(score_a), int(score_b)

    item = tournaments_table.get_item(Key={'tournament_id': tournament_id}).get('Item')
    if not item:
        return _response(404, {'error': 'tournament not found'})
    if item.get('status') not in ('knockout', 'completed'):
        return _response(400, {'error': 'tournament is not in knockout stage'})

    best_of = item.get('best_of', 1)
    target = item.get('points_to_win', 21)

    if third_place:
        match = item['knockout'].get('third_place_match')
        if not match:
            return _response(404, {'error': 'no third place match exists yet for this tournament'})
        if match['played']:
            return _response(400, {'error': 'this match is already decided'})

        try:
            decided = _submit_game(match, score_a, score_b, best_of, target, override)
        except ValueError as e:
            return _response(400, {'error': str(e)})

        if decided:
            total_a = sum(g['score_a'] for g in match['games'])
            total_b = sum(g['score_b'] for g in match['games'])
            winner = 'A' if match['games_won_a'] > match['games_won_b'] else 'B'
            update_elo_and_log(item.get('match_type', 'singles'), match['player_a'], match['player_b'],
                                total_a, total_b, item['group_id'], tournament_id, 'third_place',
                                winner_override=winner, games=match['games'], point_log=point_log)

        tournaments_table.put_item(Item=item)
        return _response(200, item)

    round_index, match_index = int(round_index), int(match_index)
    rounds = item['knockout']['rounds']
    if round_index >= len(rounds):
        return _response(400, {'error': 'invalid round_index'})
    match = rounds[round_index][match_index]
    if match.get('bye'):
        return _response(400, {'error': 'this match is a bye, no score needed'})
    if match['played']:
        return _response(400, {'error': 'this match is already decided'})

    try:
        decided = _submit_game(match, score_a, score_b, best_of, target, override)
    except ValueError as e:
        return _response(400, {'error': str(e)})

    if decided:
        total_a = sum(g['score_a'] for g in match['games'])
        total_b = sum(g['score_b'] for g in match['games'])
        winner = 'A' if match['games_won_a'] > match['games_won_b'] else 'B'
        update_elo_and_log(item.get('match_type', 'singles'), match['player_a'], match['player_b'],
                            total_a, total_b, item['group_id'], tournament_id, 'knockout',
                            winner_override=winner, games=match['games'], point_log=point_log)

        current_round = rounds[round_index]
        if all(m['played'] for m in current_round):
            if len(current_round) == 1:
                item['status'] = 'completed'
                item['champion_id'] = current_round[0]['winner_id']
            else:
                winners = []
                for m in current_round:
                    pid = m['winner_id']
                    entity = m['player_a'] if m['player_a']['player_id'] == pid else m['player_b']
                    winners.append(entity)
                rounds.append(build_knockout_round(winners))

                # If this round had exactly 2 matches, it was the semifinal
                # stage feeding a single-match final - set up a 3rd place
                # match between the two losers.
                if len(current_round) == 2 and 'third_place_match' not in item['knockout']:
                    losers = []
                    for m in current_round:
                        pid = m['winner_id']
                        loser_entity = m['player_b'] if m['player_a']['player_id'] == pid else m['player_a']
                        losers.append(loser_entity)
                    item['knockout']['third_place_match'] = {
                        'match_id': str(uuid.uuid4()),
                        'player_a': losers[0],
                        'player_b': losers[1],
                        'games': [],
                        'games_won_a': 0,
                        'games_won_b': 0,
                        'played': False,
                        'winner_id': None
                    }

    tournaments_table.put_item(Item=item)
    return _response(200, item)


# ---------- shared Elo + game-log write (entity = single player or doubles team) ----------

def compute_adaptive_k(pairing_count):
    """Higher K for a fresh/novel doubles pairing (each match together is
    high-information). Lower K once a pairing is well-established (each
    additional match together adds little new information) - this is what
    keeps a fixed partnership's ratings from swinging in lockstep every
    time. Singles has no pairing concept, so it always uses flat K_FACTOR."""
    if pairing_count == 0:
        return 40
    elif pairing_count < 5:
        return K_FACTOR
    else:
        return 20


def get_pairing_count(team_ids):
    """How many prior doubles matches has this exact 2-player team played
    together, based on matches already recorded (regardless of opponent)."""
    if len(team_ids) != 2:
        return 0
    pair_key = frozenset(team_ids)
    count = 0
    items = _scan_all(matches_table)
    for m in items:
        if m.get('match_type') != 'doubles':
            continue
        for team in (m.get('team_a') or [], m.get('team_b') or []):
            if len(team) == 2 and frozenset(team) == pair_key:
                count += 1
                break
    return count


def update_elo_and_log(match_type, entity_a, entity_b, score_a, score_b, group_id, tournament_id, stage,
                        winner_override=None, games=None, point_log=None):
    team_a_ids = entity_a.get('members', [entity_a['player_id']])
    team_b_ids = entity_b.get('members', [entity_b['player_id']])

    team_a_players = [players_table.get_item(Key={'player_id': pid}).get('Item') for pid in team_a_ids]
    team_b_players = [players_table.get_item(Key={'player_id': pid}).get('Item') for pid in team_b_ids]
    if any(p is None for p in team_a_players) or any(p is None for p in team_b_players):
        return

    rating_a_avg = sum(float(p.get('rating', 1000)) for p in team_a_players) / len(team_a_players)
    rating_b_avg = sum(float(p.get('rating', 1000)) for p in team_b_players) / len(team_b_players)

    if winner_override == 'A':
        actual_a = 1.0
    elif winner_override == 'B':
        actual_a = 0.0
    else:
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

    winner = winner_override if winner_override else ('A' if score_a > score_b else ('B' if score_b > score_a else 'tie'))

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

    log_item = {
        'match_id': str(uuid.uuid4()),
        'date': datetime.now(timezone.utc).isoformat(),
        'match_type': match_type,
        'team_a': team_a_ids,
        'team_b': team_b_ids,
        'team_a_names': [p['name'] for p in team_a_players],
        'team_b_names': [p['name'] for p in team_b_players],
        'score_a': score_a,
        'score_b': score_b,
        'winner': winner,
        'ratings_after': new_ratings,
        'group_id': group_id,
        'tournament_id': tournament_id,
        'stage': stage
    }
    if games:
        log_item['games'] = games
    if point_log:
        log_item['point_log'] = point_log
        log_item['momentum'] = momentum
    matches_table.put_item(Item=log_item)


def substitute_player(tournament_id, event):
    """Swap a player out of a team for all of that team's FUTURE (unplayed)
    matches in this tournament. Already-played fixtures/matches keep their
    original recorded entity untouched, so past results and Elo stay
    attributed to whoever actually played them."""
    body = json.loads(event.get('body') or '{}')
    team_entity_id = body.get('team_entity_id')
    old_player_id = body.get('old_player_id')
    new_player_id = body.get('new_player_id')

    if not team_entity_id or not old_player_id or not new_player_id:
        return _response(400, {'error': 'team_entity_id, old_player_id, new_player_id are required'})

    item = tournaments_table.get_item(Key={'tournament_id': tournament_id}).get('Item')
    if not item:
        return _response(404, {'error': 'tournament not found'})

    if item.get('format') == 'manual_draft':
        # Manual-draft squads aren't built or named like a legacy
        # subgroups/knockout entity (see apply_substitution below - it
        # expects entity['members']/'name' in the ' & '.join(...) shape
        # every other format uses) and group_stage/knockout here holds
        # ties (squad_a/squad_b + nested matches), not the player_a/
        # player_b entity shape apply_substitution walks. Reject cleanly
        # instead of crashing or silently mangling a squad's name.
        return _response(400, {'error': 'player substitution is not supported for manual-draft squads yet'})

    new_player = players_table.get_item(Key={'player_id': new_player_id}).get('Item')
    if not new_player:
        return _response(404, {'error': 'new player not found'})

    def apply_substitution(entity):
        if not entity or entity.get('player_id') != team_entity_id:
            return False
        members = entity.get('members', [])
        if old_player_id not in members:
            return False
        idx = members.index(old_player_id)
        members[idx] = new_player_id
        # Always rebuild from every CURRENT member's current name, however
        # many members there are - matches the ' & '.join(...) convention
        # every team entity is created with (create_tournament L301),
        # whether the team has 1, 2, or N members. A previous version only
        # did this for exactly 2 members and otherwise overwrote the whole
        # team's name with just the incoming player's name, silently
        # dropping every other member's name for teams of 3+.
        names = []
        for pid in members:
            if pid == new_player_id:
                names.append(new_player['name'])
            else:
                p = players_table.get_item(Key={'player_id': pid}).get('Item')
                names.append(p['name'] if p else pid)
        entity['name'] = ' & '.join(names)
        return True

    updated_any = False

    for sg in item.get('subgroups', {}).values():
        for f in sg['fixtures']:
            if not f['played']:
                updated_any |= apply_substitution(f['player_a'])
                updated_any |= apply_substitution(f['player_b'])
        for e in sg['members']:
            updated_any |= apply_substitution(e)

    if 'knockout' in item:
        for round_ in item['knockout'].get('rounds', []):
            for m in round_:
                if not m['played']:
                    updated_any |= apply_substitution(m['player_a'])
                    updated_any |= apply_substitution(m['player_b'])
        tp = item['knockout'].get('third_place_match')
        if tp and not tp['played']:
            updated_any |= apply_substitution(tp['player_a'])
            updated_any |= apply_substitution(tp['player_b'])

    if not updated_any:
        return _response(404, {'error': 'no unplayed matches found for that team/player combination'})

    substitutions = item.setdefault('substitutions', [])
    substitutions.append({
        'team_entity_id': team_entity_id,
        'old_player_id': old_player_id,
        'new_player_id': new_player_id,
        'new_player_name': new_player['name'],
        'timestamp': datetime.now(timezone.utc).isoformat()
    })

    tournaments_table.put_item(Item=item)
    return _response(200, item)


def _response(status_code, body_dict):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body_dict, default=str)
    }
