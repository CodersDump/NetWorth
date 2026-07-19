"""
NetWorth - list all matches in a clean, human-readable, date-sorted format.
Use this to identify which match_ids belong to a specific real-world event
(e.g. the "July 19" tournament) before tagging them.

Usage:
    pip install boto3 --break-system-packages   (if not already installed)
    python list_matches.py --region us-east-1
    python list_matches.py --date 2026-07-19    (filter to one calendar day)
"""
import argparse
import boto3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--table', default='networth-matches')
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--date', help='Filter to matches on this date, e.g. 2026-07-19')
    args = parser.parse_args()

    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    table = dynamodb.Table(args.table)

    items = table.scan().get('Items', [])
    if args.date:
        items = [i for i in items if i.get('date', '').startswith(args.date)]
    items.sort(key=lambda i: i.get('date', ''))

    for i in items:
        team_a = ' & '.join(i.get('team_a_names', []))
        team_b = ' & '.join(i.get('team_b_names', []))
        tag = f" [tournament_id={i.get('tournament_id')}, stage={i.get('stage')}]" if i.get('tournament_id') else ""
        print(f"{i.get('date')}  {i.get('match_id')}")
        print(f"    {team_a}  vs  {team_b}   {i.get('score_a')}-{i.get('score_b')}{tag}")

    print(f"\nTotal: {len(items)} match(es)")


if __name__ == '__main__':
    main()
