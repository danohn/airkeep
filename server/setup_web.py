"""Set the owner's browser password or print its Argon2id setting."""
import argparse
import getpass
from pathlib import Path
import warnings

from argon2 import PasswordHasher
from dotenv import set_key


PASSWORD_KEY = 'AIRKEEP_WEB_PASSWORD_HASH'


def write_password_hash(path: Path, password_hash: str):
    import fcntl

    if path.is_symlink() or not path.is_file():
        raise SystemExit('Choose an existing regular relay environment file.')
    with path.with_name(f'{path.name}.web-password-reset.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another password reset is running. Try again in a moment.') from None
        set_key(path, PASSWORD_KEY, password_hash)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-env', type=Path, help='Save the hash in this relay environment file')
    args = parser.parse_args()

    try:
        with warnings.catch_warnings():
            if args.write_env is not None:
                warnings.simplefilter('error', getpass.GetPassWarning)
            password = getpass.getpass('Airkeep browser password: ')
            if not 12 <= len(password) <= 1024:
                raise SystemExit('Choose a password with 12 to 1024 characters.')
            if password != getpass.getpass('Confirm password: '):
                raise SystemExit('Passwords did not match.')
    except getpass.GetPassWarning:
        raise SystemExit('Run this command in an interactive terminal for masked input.') from None
    except (EOFError, KeyboardInterrupt):
        raise SystemExit('\nPassword entry canceled.') from None

    password_hash = PasswordHasher().hash(password)
    if args.write_env is not None:
        try:
            write_password_hash(args.write_env, password_hash)
        except OSError as error:
            raise SystemExit(f'Could not update the relay environment file: {error.strerror}.') from None
        print('Password saved. Recreate the relay to apply it.')
        return

    print('Add this line to your private relay environment file:')
    print(f"{PASSWORD_KEY}='{password_hash}'")
    print('Set AIRKEEP_WEB_ORIGIN to the HTTPS address used by your browser.')


if __name__ == '__main__':
    main()
