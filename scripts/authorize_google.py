"""One-time local authorization for GA4 + Search Console access.

Run this ONCE on a machine with a browser (your laptop, not the VPS) to get
a Google OAuth refresh token. Not part of the Docker image, not run
automatically - this is a manual setup step, see README section 2.

Needs google-auth-oauthlib, which is intentionally NOT in requirements.txt
(the running container never needs it, only this one-off script does):

    pip install google-auth-oauthlib

Usage:
    python scripts/authorize_google.py <client_id> <client_secret>

A browser window opens - log in with the Google account that has access to
DataLayer's GA4 property and Search Console property, and approve access.
The script then prints a refresh token: paste it into .env as
GOOGLE_OAUTH_REFRESH_TOKEN on EVERY host (local dev AND the VPS use the
same value - unlike a service-account key file, this isn't per-host).
"""
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    'https://www.googleapis.com/auth/analytics.readonly',
    'https://www.googleapis.com/auth/webmasters.readonly',
]


def main():
    if len(sys.argv) != 3:
        print('Usage: python scripts/authorize_google.py <client_id> <client_secret>')
        sys.exit(1)

    client_id, client_secret = sys.argv[1], sys.argv[2]

    client_config = {
        'installed': {
            'client_id': client_id,
            'client_secret': client_secret,
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token',
            'redirect_uris': ['http://localhost'],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    credentials = flow.run_local_server(port=0)

    print()
    print('Authorization complete.')
    print()
    print('Paste this into .env as GOOGLE_OAUTH_REFRESH_TOKEN on EVERY host')
    print('(same value works everywhere - not per-host like a key file):')
    print()
    print(credentials.refresh_token)


if __name__ == '__main__':
    main()
