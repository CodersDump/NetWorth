#!/usr/bin/env python3
"""
Audit (and optionally repair) NetWorth claim-linkage drift.

Background
----------
A player profile counts as "claimed" the moment its DynamoDB row has an
`email` (players Lambda: `'claimed': bool(i.get('email'))`). The account is
actually usable only when the matching Cognito user carries `custom:player_id`.
Those are two separate writes in two separate systems.

The old self-service /claim-player wrote the DynamoDB email server-side but
left the Cognito `custom:player_id` to a client-side updateAttributes call.
When that client call didn't land, the profile was left claimed-but-unlinked
and un-retryable (the "email already set" guard blocks a second attempt).

This script finds those accounts: a Cognito user whose email matches a
player row, but whose custom:player_id is empty. With --fix it writes the
missing custom:player_id via the admin API.

The backend fix (claim_player now links both sides server-side) stops NEW
drift; this script cleans up anyone already stranded.

Usage
-----
    python scripts/claim_audit.py          # report only, changes nothing
    python scripts/claim_audit.py --fix    # repair the accounts it finds

POOL_ID auto-resolves from the `networth-app` CloudFormation stack outputs.
Region comes from your default AWS profile/env (AWS_REGION or ~/.aws/config).
Override either with the env vars below if you need to.

    NETWORTH_STACK      default: networth-app
    NETWORTH_POOL_ID    skip stack lookup and use this pool id directly
    NETWORTH_TABLE      default: networth-players

Repaired users must log out and back in for the restored link to appear in
their session (ID tokens are baked at login).
"""
import argparse
import os
import sys
import boto3

STACK = os.environ.get('NETWORTH_STACK', 'networth-app')
TABLE = os.environ.get('NETWORTH_TABLE', 'networth-players')


def resolve_pool_id():
    override = os.environ.get('NETWORTH_POOL_ID')
    if override:
        return override
    cfn = boto3.client('cloudformation')
    outs = cfn.describe_stacks(StackName=STACK)['Stacks'][0].get('Outputs', [])
    for o in outs:
        if o['OutputKey'] == 'UserPoolId':
            return o['OutputValue']
    sys.exit(f"Could not find UserPoolId output on stack '{STACK}'. "
             f"Set NETWORTH_POOL_ID to override.")


def scan_all(table):
    items, kw = [], {}
    while True:
        r = table.scan(**kw)
        items += r['Items']
        if 'LastEvaluatedKey' not in r:
            return items
        kw['ExclusiveStartKey'] = r['LastEvaluatedKey']


def users_all(idp, pool_id):
    users, kw = [], {'UserPoolId': pool_id}
    while True:
        r = idp.list_users(**kw)
        users += r['Users']
        if 'PaginationToken' not in r:
            return users
        kw['PaginationToken'] = r['PaginationToken']


def attr(user, name):
    return next((a['Value'] for a in user['Attributes'] if a['Name'] == name), None)


def main(fix):
    pool_id = resolve_pool_id()
    ddb = boto3.resource('dynamodb').Table(TABLE)
    idp = boto3.client('cognito-idp')

    by_email = {p['email'].lower(): p for p in scan_all(ddb) if p.get('email')}

    stuck = []
    for u in users_all(idp, pool_id):
        email = (attr(u, 'email') or '').lower()
        pid = attr(u, 'custom:player_id')
        p = by_email.get(email)
        if p and not pid:                     # claimed in DDB, unlinked in Cognito
            stuck.append((u['Username'], email, p['player_id'], p.get('nickname')))

    if not stuck:
        print('No drift: every claimed profile has a linked account.')
        return

    print(f'{len(stuck)} claimed-but-unlinked account(s):\n')
    for username, email, pid, nick in stuck:
        print(f'  {email:32} -> player {pid}  ({nick})')
        if fix:
            idp.admin_update_user_attributes(
                UserPoolId=pool_id,
                Username=username,
                UserAttributes=[{'Name': 'custom:player_id', 'Value': pid}])
            print('      linked.')

    if fix:
        print('\nDone. Tell these users to log out and back in so the link '
              'shows up in their session.')
    else:
        print('\nRe-run with --fix to write custom:player_id for these.')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Audit/repair NetWorth claim linkage.')
    ap.add_argument('--fix', action='store_true',
                    help='write the missing custom:player_id (default: report only)')
    main(ap.parse_args().fix)
