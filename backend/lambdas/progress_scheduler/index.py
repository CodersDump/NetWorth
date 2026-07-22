"""
NetWorth - progress_scheduler Lambda

Triggered daily by an EventBridge rule (see ProgressSchedulerDailyRule in
the CloudFormation template). Checks whether today marks the start of a
new week/month/year, and if so, permanently locks in the *previous*
period's "most improved" and "most active" result - for the whole player
pool (scope="global") and separately for each existing group.

This is what makes streaks and "times held" badges possible later: once a
period's winner is written here, it never changes, so counting
consecutive wins or lifetime holds becomes a simple scan over this table.

Safe to run more than once for the same day - history_id is deterministic
(scope#period#period_start), so re-writing the same period just
overwrites it with the same result rather than duplicating entries.

Env vars:
    MATCHES_TABLE, PLAYERS_TABLE, GROUPS_TABLE, PROGRESS_HISTORY_TABLE
"""
import os
import boto3
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal

dynamodb = boto3.resource('dynamodb')
matches_table = dynamodb.Table(os.environ['MATCHES_TABLE'])
players_table = dynamodb.Table(os.environ['PLAYERS_TABLE'])
groups_table = dynamodb.Table(os.environ['GROUPS_TABLE'])
history_table = dynamodb.Table(os.environ['PROGRESS_HISTORY_TABLE'])


def get_group_member_ids(group_id):
    """The set of player_ids belonging to a group - used to decide WHO is
    eligible to win a group's badge, not to restrict which matches count.
    Each member's own rating delta still comes from their FULL match
    history, including matches never tagged with any group at all."""
    group = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    return set(group.get('member_ids', [])) if group else set()


def handler(event, context):
    today = datetime.now(timezone.utc).date()
    matches = matches_table.scan().get('Items', [])
    matches.sort(key=lambda m: m.get('date', ''))
    groups = groups_table.scan().get('Items', [])

    periods_to_close = []

    if today.weekday() == 0:  # Monday - last week just ended
        period_start = today - timedelta(days=7)
        period_end = today
        periods_to_close.append(('week', period_start, period_end))

    if today.day == 1:  # 1st of the month - last month just ended
        last_day_prev_month = today.replace(day=1) - timedelta(days=1)
        period_start = last_day_prev_month.replace(day=1)
        period_end = today.replace(day=1)
        periods_to_close.append(('month', period_start, period_end))

    if today.month == 1 and today.day == 1:  # Jan 1 - last year just ended
        period_start = date(today.year - 1, 1, 1)
        period_end = date(today.year, 1, 1)
        periods_to_close.append(('year', period_start, period_end))

    closed = []
    for period_name, period_start, period_end in periods_to_close:
        period_start_dt = datetime.combine(period_start, datetime.min.time(), tzinfo=timezone.utc)
        period_end_dt = datetime.combine(period_end, datetime.min.time(), tzinfo=timezone.utc)

        scopes = [('global', None)] + [(f"group_{g['group_id']}", g['group_id']) for g in groups]
        for scope_label, group_id in scopes:
            member_ids = get_group_member_ids(group_id) if group_id else None
            snapshot = compute_period_snapshot(matches, period_start_dt, period_end_dt, member_ids)
            if snapshot['most_improved'] is None and snapshot['most_active'] is None:
                continue  # no eligible activity in this scope for this period - nothing to record

            write_history_entry(scope_label, group_id, period_name, period_start.isoformat(), period_end.isoformat(), snapshot)
            closed.append(f"{scope_label}/{period_name}/{period_start.isoformat()}")

    return {'statusCode': 200, 'body': f"Closed periods: {closed}"}


def compute_period_snapshot(matches, period_start_dt, period_end_dt, member_ids=None):
    """Rating change and match count for every player within a fixed,
    closed date range [period_start_dt, period_end_dt) - computed from
    ALL matches regardless of group tagging. member_ids, if given, only
    restricts which players are ELIGIBLE to win the badge for this scope;
    it does not change how their own delta is calculated."""
    rating_before = {}
    rating_current = {}
    matches_in_period = {}

    for m in matches:
        date_str = m.get('date', '')
        try:
            match_date = datetime.fromisoformat(date_str)
        except ValueError:
            continue
        if match_date >= period_end_dt:
            continue
        ratings_after = m.get('ratings_after') or {}
        for pid, rating in ratings_after.items():
            rating = float(rating)
            if match_date < period_start_dt:
                rating_before[pid] = rating
            else:
                rating_current[pid] = rating
                matches_in_period[pid] = matches_in_period.get(pid, 0) + 1

    progress_rows = []
    for pid, current in rating_current.items():
        if member_ids is not None and pid not in member_ids:
            continue
        start = rating_before.get(pid, 1000.0)
        delta = round(current - start, 1)
        progress_rows.append({'player_id': pid, 'delta': delta})
    # Deterministic order: delta descending, then player_id - so re-running
    # against identical data always produces identical results, instead of
    # depending on dict iteration order of ratings_after maps.
    progress_rows.sort(key=lambda r: (-r['delta'], r['player_id']))

    # Ties are STRUCTURAL here, not rare: with team-average Elo both doubles
    # partners always receive the same delta, so a pair that plays together
    # moves in lockstep. Record ALL co-winners rather than crowning one of
    # them by accident of ordering.
    most_improved = None
    if progress_rows:
        top_delta = progress_rows[0]['delta']
        co_winner_ids = sorted(r['player_id'] for r in progress_rows if r['delta'] == top_delta)
        most_improved = {'player_ids': co_winner_ids, 'delta': top_delta}

    most_active = None
    eligible_activity = {pid: cnt for pid, cnt in matches_in_period.items()
                          if member_ids is None or pid in member_ids}
    if eligible_activity:
        top_count = max(eligible_activity.values())
        co_active_ids = sorted(pid for pid, cnt in eligible_activity.items() if cnt == top_count)
        most_active = {'player_ids': co_active_ids, 'matches': top_count}

    return {'most_improved': most_improved, 'most_active': most_active}


def write_history_entry(scope_label, group_id, period_name, period_start_iso, period_end_iso, snapshot):
    history_id = f"{scope_label}#{period_name}#{period_start_iso}"
    item = {
        'history_id': history_id,
        'scope': scope_label,
        'group_id': group_id,
        'period': period_name,
        'period_start': period_start_iso,
        'period_end': period_end_iso,
        'computed_at': datetime.now(timezone.utc).isoformat(),
    }
    def resolve_named(pids):
        """(name, pid) pairs sorted by display name, so the 'primary' legacy
        winner and the rendered order are stable across runs."""
        pairs = []
        for pid in pids:
            p = players_table.get_item(Key={'player_id': pid}).get('Item')
            pairs.append((p['name'] if p else pid, pid))
        pairs.sort()
        return pairs

    if snapshot['most_improved']:
        pairs = resolve_named(snapshot['most_improved']['player_ids'])
        item['most_improved_player_ids'] = [pid for _, pid in pairs]
        item['most_improved_names'] = [name for name, _ in pairs]
        item['most_improved_delta'] = Decimal(str(snapshot['most_improved']['delta']))
        # Legacy singular fields, kept so anything reading the old schema
        # (or history rows written before this change) still works.
        item['most_improved_player_id'] = pairs[0][1]
        item['most_improved_name'] = ' & '.join(name for name, _ in pairs)
    if snapshot['most_active']:
        pairs = resolve_named(snapshot['most_active']['player_ids'])
        item['most_active_player_ids'] = [pid for _, pid in pairs]
        item['most_active_names'] = [name for name, _ in pairs]
        item['most_active_matches'] = snapshot['most_active']['matches']
        item['most_active_player_id'] = pairs[0][1]
        item['most_active_name'] = ' & '.join(name for name, _ in pairs)

    history_table.put_item(Item=item)
