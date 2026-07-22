"""
NetWorth - one-time backfill: reconstruct historical progress-badge
history (weekly/monthly/yearly "most improved" and "most active" winners)
for every period that already happened, going back to your earliest
recorded match. Without this, the permanent history table only starts
tracking from whenever the scheduled Lambda first runs live - this fills
in everything that came before.

Uses the exact same period-snapshot logic as the live scheduler, so a
week reconstructed here behaves identically to one closed automatically
going forward (same eligibility for streaks/holder-count badges).

Usage:
    pip install boto3 --break-system-packages   (if not already installed)
    python backfill_progress_history.py

Safe to re-run - history_id is deterministic (scope#period#period_start),
so re-running just overwrites the same entries with the same result.
"""
import boto3
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal

K_FACTOR = 32


def get_group_member_ids(groups_table, group_id):
    group = groups_table.get_item(Key={'group_id': group_id}).get('Item')
    return set(group.get('member_ids', [])) if group else set()


def compute_period_snapshot(matches, period_start_dt, period_end_dt, member_ids=None):
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
    progress_rows.sort(key=lambda r: -r['delta'])

    most_improved = progress_rows[0] if progress_rows else None
    most_active = None
    eligible_activity = {pid: cnt for pid, cnt in matches_in_period.items()
                          if member_ids is None or pid in member_ids}
    if eligible_activity:
        active_pid = max(eligible_activity.items(), key=lambda kv: kv[1])[0]
        most_active = {'player_id': active_pid, 'matches': eligible_activity[active_pid]}

    return {'most_improved': most_improved, 'most_active': most_active}


def write_history_entry(history_table, players_table, scope_label, group_id, period_name,
                         period_start_iso, period_end_iso, snapshot):
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
    if snapshot['most_improved']:
        pid = snapshot['most_improved']['player_id']
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        item['most_improved_player_id'] = pid
        item['most_improved_name'] = p['name'] if p else pid
        item['most_improved_delta'] = Decimal(str(snapshot['most_improved']['delta']))
    if snapshot['most_active']:
        pid = snapshot['most_active']['player_id']
        p = players_table.get_item(Key={'player_id': pid}).get('Item')
        item['most_active_player_id'] = pid
        item['most_active_name'] = p['name'] if p else pid
        item['most_active_matches'] = snapshot['most_active']['matches']

    history_table.put_item(Item=item)
    return item


def iter_weeks(earliest_date, today):
    # go BACK to the Monday of the week containing earliest_date, not
    # forward to the next one - otherwise a match that happened on a
    # weekend gets excluded from its own week entirely
    d = earliest_date - timedelta(days=earliest_date.weekday())
    while d + timedelta(days=7) <= today:
        yield d, d + timedelta(days=7)
        d += timedelta(days=7)


def iter_months(earliest_date, today):
    d = earliest_date.replace(day=1)
    while True:
        if d.month == 12:
            next_month = d.replace(year=d.year + 1, month=1)
        else:
            next_month = d.replace(month=d.month + 1)
        if next_month > today:
            break
        yield d, next_month
        d = next_month


def iter_years(earliest_date, today):
    y = earliest_date.year
    while date(y + 1, 1, 1) <= today:
        yield date(y, 1, 1), date(y + 1, 1, 1)
        y += 1


def main():
    dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
    matches_table = dynamodb.Table('networth-matches')
    players_table = dynamodb.Table('networth-players')
    groups_table = dynamodb.Table('networth-groups')
    history_table = dynamodb.Table('networth-progress-history')

    matches = matches_table.scan().get('Items', [])
    matches.sort(key=lambda m: m.get('date', ''))
    if not matches:
        print("No matches recorded yet - nothing to backfill.")
        return

    earliest_date = datetime.fromisoformat(matches[0]['date']).date()
    today = datetime.now(timezone.utc).date()
    groups = groups_table.scan().get('Items', [])

    scopes = [('global', None)] + [(f"group_{g['group_id']}", g['group_id']) for g in groups]
    period_generators = {'week': iter_weeks, 'month': iter_months, 'year': iter_years}

    written = 0
    for period_name, generator in period_generators.items():
        for period_start, period_end in generator(earliest_date, today):
            period_start_dt = datetime.combine(period_start, datetime.min.time(), tzinfo=timezone.utc)
            period_end_dt = datetime.combine(period_end, datetime.min.time(), tzinfo=timezone.utc)

            for scope_label, group_id in scopes:
                member_ids = get_group_member_ids(groups_table, group_id) if group_id else None
                snapshot = compute_period_snapshot(matches, period_start_dt, period_end_dt, member_ids)
                if snapshot['most_improved'] is None and snapshot['most_active'] is None:
                    continue
                write_history_entry(history_table, players_table, scope_label, group_id, period_name,
                                     period_start.isoformat(), period_end.isoformat(), snapshot)
                written += 1
                print(f"{period_name} {period_start.isoformat()} [{scope_label}]: "
                      f"most improved = {snapshot['most_improved']}, most active = {snapshot['most_active']}")

    print(f"\nDone. Wrote {written} history entries.")


if __name__ == '__main__':
    main()
