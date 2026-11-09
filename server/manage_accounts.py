"""Bootstrap or recover an administrator from the relay host."""
import argparse
import getpass
import warnings

from accounts import PASSWORD, bootstrap, recover
from config import Settings
from storage import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('bootstrap', 'recover'))
    parser.add_argument('username')
    parser.add_argument('--import-owner-password', action='store_true')
    args = parser.parse_args()
    settings = Settings()
    if not settings.jwt_signing_key:
        parser.error('Set AIRKEEP_JWT_SIGNING_KEY before managing accounts.')
    if args.import_owner_password:
        if args.action != 'bootstrap' or not settings.web_password_hash:
            parser.error('Import is only available during bootstrap with the old hash configured.')
        password_hash = settings.web_password_hash
    else:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                password = getpass.getpass('Administrator password: ')
                if not 12 <= len(password) <= 1024:
                    parser.error('Use 12 to 1024 characters.')
                if password != getpass.getpass('Confirm password: '):
                    parser.error('Passwords did not match.')
        except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
            parser.exit(1, 'Use an interactive terminal to enter a password.\n')
        password_hash = PASSWORD.hash(password)
    store = Store(settings.db_path)
    try:
        operation = bootstrap if args.action == 'bootstrap' else recover
        operation(store, args.username, password_hash)
    except ValueError as error:
        parser.exit(1, f'{error}\n')
    finally:
        store.close()
    print('Administrator is ready.')


if __name__ == '__main__':
    main()
