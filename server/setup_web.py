"""Generate the owner's browser password hash without putting a password in shell history."""
import getpass
from argon2 import PasswordHasher


def main():
    password = getpass.getpass('Airkeep browser password: ')
    if not 12 <= len(password) <= 1024:
        raise SystemExit('Choose a password with 12 to 1024 characters.')
    if password != getpass.getpass('Confirm password: '):
        raise SystemExit('Passwords did not match.')
    print('Add this line to your private relay environment file:')
    print("AIRKEEP_WEB_PASSWORD_HASH='" + PasswordHasher().hash(password) + "'")
    print('Set AIRKEEP_WEB_ORIGIN to the HTTPS address used by your browser.')


if __name__ == '__main__':
    main()
