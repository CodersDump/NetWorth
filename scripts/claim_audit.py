#!/usr/bin/env python3
"""
<<<<<<< HEAD
Audit (and optionally repair) NetWorth claim linkage.

Two facts define ownership, in two systems:
  * a player row's `email`      -> "claimed by this address" (bool(email) == claimed)
  * a Cognito user's custom:player_id -> "this account owns that player"

A healthy claim has BOTH, pointing at each other. Historic bugs broke that
in two different directions:

  1. Linkage bug: a claim wrote the player `email` but the Cognito
     custom:player_id never got set (fragile client-side write). The account
     owns nothing; the row looks claimed. -> we LINK it.

  2. Recorder mis-stamp: /register-and-join used to write the *recorder's*
     email onto every player they added from the match screen. That marked
     someone ELSE claimed-by-recorder and hid them from their own claim
     picker. The email's account actually owns a different player (or no
     account exists at all). -> we STRIP the email.

Buckets this reports:
  healthy          email <-> account point at each other. Leave alone.
  claimed_unlinked email set, but that account owns nothing.  (--fix-links)
  misstamp         email set, but that account owns a DIFFERENT player, or
                   no account has that email at all.          (--fix-misstamps)
  unclaimed        no email - genuinely open to claim.
  unlinked_account Cognito user with no custom:player_id - a person with no
                   profile yet (or a victim of the above).

Usage:
  python scripts/claim_audit.py                  # report every bucket, change nothing
  python scripts/claim_audit.py --fix-links      # set custom:player_id for claimed_unlinked
  python scripts/claim_audit.py --fix-misstamps  # strip the wrong email off misstamp rows

POOL_ID auto-resolves from the `networth-app` stack. Region from the default
profile/env. Overrides: NETWORTH_STACK, NETWORTH_POOL_ID, NETWORTH_TABLE.

After --fix-links, affected users must log out/in (ID token is baked at login).
After --fix-misstamps, the freed profiles show up in the claim picker again.
=======
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
>>>>>>> 19cfaf1e885a0dd4b17b392a1464d0cf69a64da4
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
<<<<<<< HEAD
    sys.exit(f"No UserPoolId output on stack '{STACK}'. Set NETWORTH_POOL_ID.")
=======
    sys.exit(f"Could not find UserPoolId output on stack '{STACK}'. "
             f"Set NETWORTH_POOL_ID to override.")
>>>>>>> 19cfaf1e885a0dd4b17b392a1464d0cf69a64da4


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


<<<<<<< HEAD
def classify(players, users):
    """Returns dict of bucket -> list of rows, plus the lookup maps."""
    # config rows aren't players
    players = [p for p in players if not str(p.get('player_id', '')).startswith('__')]
    player_ids = {p['player_id'] for p in players}

    user_by_email = {}
    pid_by_email = {}
    for u in users:
        e = (attr(u, 'email') or '').lower()
        if not e:
            continue
        user_by_email[e] = u
        pid_by_email[e] = attr(u, 'custom:player_id')

    buckets = {'healthy': [], 'claimed_unlinked': [], 'misstamp': [], 'unclaimed': []}
    for p in players:
        email = (p.get('email') or '').lower()
        if not email:
            buckets['unclaimed'].append(p)
            continue
        owner_pid = pid_by_email.get(email)          # what that email's account owns
        if email not in user_by_email:
            # email on the row but no Cognito account has it -> orphan stamp
            buckets['misstamp'].append((p, email, '(no account)'))
        elif owner_pid == p['player_id']:
            buckets['healthy'].append(p)
        elif owner_pid is None:
            buckets['claimed_unlinked'].append((p, email))
        else:
            buckets['misstamp'].append((p, email, owner_pid))

    # accounts owning no player
    unlinked = [(attr(u, 'email'), u['Username']) for u in users
                if not attr(u, 'custom:player_id')]
    buckets['unlinked_account'] = unlinked
    return buckets


def label(p):
    return f"{p.get('name')} ({p.get('nickname')})"


def report(buckets):
    print(f"\nhealthy links ............. {len(buckets['healthy'])}")
    print(f"claimed but unlinked ...... {len(buckets['claimed_unlinked'])}   (--fix-links)")
    print(f"recorder mis-stamps ....... {len(buckets['misstamp'])}   (--fix-misstamps)")
    print(f"unclaimed profiles ........ {len(buckets['unclaimed'])}")
    print(f"accounts with no profile .. {len(buckets['unlinked_account'])}")

    if buckets['claimed_unlinked']:
        print("\n-- claimed but unlinked (their own claim didn't link) --")
        for p, email in buckets['claimed_unlinked']:
            print(f"   {label(p):28} email {email} owns nothing")
    if buckets['misstamp']:
        print("\n-- recorder mis-stamps (wrong email on the row) --")
        for p, email, owner in buckets['misstamp']:
            print(f"   {label(p):28} stamped by {email} (that account owns {owner})")
    if buckets['unclaimed']:
        print("\n-- unclaimed profiles (open to claim) --")
        for p in buckets['unclaimed']:
            print(f"   {label(p)}")
    if buckets['unlinked_account']:
        print("\n-- accounts with no profile yet --")
        for email, _ in buckets['unlinked_account']:
            print(f"   {email}")


def fix_links(idp, pool_id, buckets):
    if not buckets['claimed_unlinked']:
        print("Nothing to link.")
        return
    for p, email in buckets['claimed_unlinked']:
        u = None
        # find the username for this email
        r = idp.list_users(UserPoolId=pool_id, Filter=f'email = "{email}"', Limit=1)
        if r['Users']:
            u = r['Users'][0]['Username']
        if not u:
            print(f"   skip {label(p)}: no account for {email}")
            continue
        idp.admin_update_user_attributes(
            UserPoolId=pool_id, Username=u,
            UserAttributes=[{'Name': 'custom:player_id', 'Value': p['player_id']}])
        print(f"   linked {email} -> {label(p)}")
    print("\nDone. Those users must log out and back in.")


def fix_misstamps(table, buckets):
    if not buckets['misstamp']:
        print("Nothing to strip.")
        return
    for p, email, owner in buckets['misstamp']:
        table.update_item(Key={'player_id': p['player_id']}, UpdateExpression='REMOVE email')
        print(f"   freed {label(p)} (removed {email})")
    print("\nDone. Those profiles are now open in the claim picker.")


def main(args):
    pool_id = resolve_pool_id()
    table = boto3.resource('dynamodb').Table(TABLE)
    idp = boto3.client('cognito-idp')
    buckets = classify(scan_all(table), users_all(idp, pool_id))

    if args.fix_links:
        fix_links(idp, pool_id, buckets)
    elif args.fix_misstamps:
        fix_misstamps(table, buckets)
    else:
        report(buckets)
=======
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
>>>>>>> 19cfaf1e885a0dd4b17b392a1464d0cf69a64da4


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Audit/repair NetWorth claim linkage.')
<<<<<<< HEAD
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--fix-links', action='store_true',
                   help='set custom:player_id for claimed-but-unlinked accounts')
    g.add_argument('--fix-misstamps', action='store_true',
                   help='strip the wrong recorder email off mis-stamped player rows')
    main(ap.parse_args())
=======
    ap.add_argument('--fix', action='store_true',
                    help='write the missing custom:player_id (default: report only)')
    main(ap.parse_args().fix)
>>>>>>> 19cfaf1e885a0dd4b17b392a1464d0cf69a64da4
