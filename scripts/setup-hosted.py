"""Generate private hosted configuration without printing secrets."""
import argparse
import base64
import os
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('server/.env.hosted'))
    parser.add_argument('--origin', default='https://dashboard.airkeep.io')
    arguments = parser.parse_args()
    origin = arguments.origin.rstrip('/')
    from urllib.parse import urlsplit
    url = urlsplit(origin)
    if url.scheme != 'https' or not url.hostname or url.path or url.query or url.fragment or url.username:
        parser.error('Choose a plain HTTPS origin.')
    values = {
        'AIRKEEP_HOST': url.hostname,
        'AIRKEEP_WEB_ORIGIN': origin,
        'AIRKEEP_IMAGE_TAG': 'hosted-staging',
        'AIRKEEP_DEPLOYMENT': 'hosted',
        'AIRKEEP_MIGRATION_PASSWORD': secrets.token_hex(32),
        'AIRKEEP_RUNTIME_PASSWORD': secrets.token_hex(32),
        'AIRKEEP_JWT_SIGNING_KEY': secrets.token_hex(48),
        'AIRKEEP_EMAIL_LINK_KEY': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        'AIRKEEP_PUBLIC_SIGNUP': 'false',
        'AIRKEEP_TWILIO_API_KEY': '',
        'AIRKEEP_TWILIO_API_SECRET': '',
        'AIRKEEP_EMAIL_FROM': '',
        'AIRKEEP_EMAIL_FROM_NAME': 'Airkeep',
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(arguments.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(''.join(f'{key}={value}\n' for key, value in values.items()))
    (arguments.output.parent / 'private' / 'apns').mkdir(parents=True, exist_ok=True, mode=0o700)
    print(f'Created {arguments.output} with mode 0600. Configure Twilio Email before enabling signup.')


if __name__ == '__main__':
    main()
