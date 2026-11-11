"""Restricted operator commands, run on the server with runtime credentials."""
import argparse
import json
import time

from accounts import revoke_user
from config import Settings
from postgres import PostgresStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('health')
    suspend = commands.add_parser('suspend')
    suspend.add_argument('account_id')
    arguments = parser.parse_args()
    settings = Settings()
    store = PostgresStore(settings.database_url.get_secret_value())
    try:
        if arguments.command == 'health':
            counts = {name: store.rows(f'SELECT COUNT(*) FROM {name}')[0][0]
                      for name in ('accounts', 'relay_credentials', 'hosted_email_outbox')}
            counts['serverTime'] = time.time()
            print(json.dumps(counts))
        else:
            with store.tenant(arguments.account_id), store.transaction():
                result = store.execute('UPDATE accounts SET disabled=1,version=version+1 WHERE id=?',
                                       (arguments.account_id,))
                revoke_user(store, arguments.account_id)
                store.execute('UPDATE relay_credentials SET revoked=1 WHERE "user"=?', (arguments.account_id,))
            print(json.dumps({'suspended': result.rowcount == 1}))
    finally:
        store.close()


if __name__ == '__main__':
    main()
