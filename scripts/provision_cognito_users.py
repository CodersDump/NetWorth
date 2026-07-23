"""
NetWorth - Epic 2: bulk-provision a Cognito account for every player already
on the roster, pre-linked to their existing player_id (custom:player_id),
so their first login is already tied to their real match history and
ratings - no separate "claim" step needed for people who are already
registered (that claim flow is Epic 6, for genuinely NEW people only).

Runs LOCALLY using your own AWS credentials (same pattern as every other
script in this folder - repair_ratings_after.py, seed_finance_from_excel.py,
etc.) via AdminCreateUser, which needs cognito-idp:Admin* permissions on
your IAM user. It does NOT go through any Lambda, and does NOT require any
change to LambdaExecutionRole.

Your PlayersTable has no email field today, so this script needs a mapping
you fill in once: a CSV of `name,email`. A template is printed if the file
doesn't exist yet.

Usage:
    # 1. Generate a template CSV to fill in
    python provision_cognito_users.py --write-template

    # 2. Fill in scripts/player_emails.csv with real emails, then dry-run:
    python provision_cognito_users.py --pool-id us-east-1_XXXXXXXXX

    # 3. Actually create the Cognito accounts:
    python provision_cognito_users.py --pool-id us-east-1_XXXXXXXXX --apply

    # 4. Put yourself in the SuperAdmin group (see Epic 3):
    python provision_cognito_users.py --pool-id us-east-1_XXXXXXXXX \\
        --grant-super-admin you@example.com --apply

Safe to re-run: existing Cognito users (matched by email) are skipped, not
recreated or overwritten. The temporary password is generated fresh each
run and printed once - it is never written to disk or committed anywhere.
"""
import argparse
import csv
import os
import secrets
import string

import boto3

CSV_PATH = os.path.join(os.path.dirname(__file__), 'player_emails.csv')
TEMPLATE = """name,email
Sourabh C,
Suraj,
Ramchander,
Pradeep,
Mirgank,
Mohit,
Abhishek K,
Gangaram Ghadi,
Mayank,
Adi,
Sambhit,
Sandeep,
Suren,
Bibhu,
Aditya,
Sohan,
"""


def generate_temp_password():
    # Meets the pool's policy (8+, lowercase, number) with a symbol added
    # for good measure - shared across this run's batch, single-use per
    # account since Cognito forces a change on first login regardless.
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(10)) + '!7'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pool-id', help='Cognito User Pool ID (from the UserPoolId stack output)')
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--apply', action='store_true', help='actually create accounts (default: dry run)')
    parser.add_argument('--write-template', action='store_true', help='write a template CSV and exit')
    parser.add_argument('--grant-super-admin', metavar='EMAIL',
                         help='add this (already-provisioned) email to the SuperAdmin Cognito group')
    args = parser.parse_args()

    if args.write_template:
        if os.path.exists(CSV_PATH):
            print(f"{CSV_PATH} already exists - not overwriting. Delete it first if you really want a fresh template.")
            return
        with open(CSV_PATH, 'w', newline='') as f:
            f.write(TEMPLATE)
        print(f"Wrote a template to {CSV_PATH}.")
        print("Fill in the email column (leave a row blank to skip that player), then re-run without --write-template.")
        return

    if not args.pool_id:
        print("ERROR: --pool-id is required (find it in the UserPoolId CloudFormation output).")
        return

    client = boto3.client('cognito-idp', region_name=args.region)

    if args.grant_super_admin:
        if not args.apply:
            print(f"DRY RUN - would add {args.grant_super_admin} to the SuperAdmin group. Re-run with --apply.")
            return
        client.admin_add_user_to_group(UserPoolId=args.pool_id, Username=args.grant_super_admin,
                                        GroupName='SuperAdmin')
        print(f"Added {args.grant_super_admin} to SuperAdmin \u2713")
        return

    if not os.path.exists(CSV_PATH):
        print(f"No {CSV_PATH} found. Run with --write-template first to generate one, fill in emails, then re-run.")
        return

    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    players_table = dynamodb.Table('networth-players')
    players_by_name = {p['name'].strip().lower(): p for p in players_table.scan().get('Items', [])}

    rows = []
    with open(CSV_PATH, newline='') as f:
        for row in csv.DictReader(f):
            name, email = row.get('name', '').strip(), row.get('email', '').strip()
            if not name or not email:
                continue
            rows.append((name, email))

    if not rows:
        print(f"No complete name,email rows found in {CSV_PATH} - fill in the email column first.")
        return

    temp_password = generate_temp_password()
    to_create, skipped, not_found = [], [], []

    for name, email in rows:
        player = players_by_name.get(name.strip().lower())
        if not player:
            not_found.append(name)
            continue
        try:
            client.admin_get_user(UserPoolId=args.pool_id, Username=email)
            skipped.append((name, email))
            continue
        except client.exceptions.UserNotFoundException:
            to_create.append((name, email, player['player_id']))

    print(f"Roster match: {len(to_create)} to create, {len(skipped)} already exist, {len(not_found)} name(s) not found in PlayersTable")
    if not_found:
        print("  NOT FOUND (check spelling against the roster):", ', '.join(not_found))
    if skipped:
        print("  Already provisioned, skipping:", ', '.join(n for n, _ in skipped))

    if not to_create:
        print("Nothing to create.")
        return

    if not args.apply:
        print(f"\nDRY RUN - would create {len(to_create)} account(s) with a shared temporary password.")
        print("Re-run with --apply to actually create them in Cognito.")
        for name, email, pid in to_create:
            print(f"  {name:<16} {email:<30} -> player_id {pid}")
        return

    print(f"\nCreating {len(to_create)} account(s)...")
    for name, email, player_id in to_create:
        client.admin_create_user(
            UserPoolId=args.pool_id,
            Username=email,
            UserAttributes=[
                {'Name': 'email', 'Value': email},
                {'Name': 'email_verified', 'Value': 'true'},
                {'Name': 'custom:player_id', 'Value': player_id},
            ],
            TemporaryPassword=temp_password,
            MessageAction='SUPPRESS',  # you're distributing the password yourself
        )
        # Also store the email on the player's own record - this is what
        # lets the login screen accept a name/nickname/player_id instead
        # of requiring the email up front (see lookup_email_for_login in
        # the players Lambda). Cognito's own copy stays the source of
        # truth for auth; this is purely for that lookup.
        players_table.update_item(
            Key={'player_id': player_id},
            UpdateExpression='SET email = :e',
            ExpressionAttributeValues={':e': email}
        )
        print(f"  Created: {name} <{email}>")

    print(f"\nDone. Shared temporary password for this batch: {temp_password}")
    print("Distribute this to the people listed above (WhatsApp, in person, etc.) - never commit it.")
    print("Cognito forces each account to set its own password on first login, so this string is single-use per account.")
    print("\nTell them to log in at the app; their first login will prompt a password change automatically.")


if __name__ == '__main__':
    main()
