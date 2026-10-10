"""Sign in to Miris from a terminal, exactly the way the add-on does.

    python tools/miris_login.py            sign in (opens your browser to approve)
    python tools/miris_login.py --check    test the saved sign-in
    python tools/miris_login.py --logout   revoke the token on Miris and delete it locally

Tokens go straight into the OS credential vault and are never printed. This is how the
"Authorization: Bearer" scheme for OAuth tokens was confirmed against the live API.
"""

import argparse
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stream_ready"))
from core import oauth  # noqa: E402
from core.http import Transport  # noqa: E402
from core.miris import DEFAULT_BASE_URL, MirisClient, MirisError  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--logout", action="store_true")
    parser.add_argument("--api-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()
    transport = Transport(timeout=20, log=print)
    base = args.api_url

    if args.logout:
        tokens = oauth.load_tokens()
        if tokens and tokens.get("refresh_token"):
            try:
                oauth.revoke(transport, base, tokens)
                print("token revoked on Miris")
            except MirisError as exc:
                print(f"revoke failed ({exc}); remove it under Account > OAuth tokens in the portal")
        oauth.forget_tokens()
        print("local tokens deleted")
        return

    if args.check:
        tokens = oauth.load_tokens()
        if not tokens:
            sys.exit("no saved sign-in")
        if not tokens.get("base_url"):  # saved by an early version of this script
            tokens["base_url"] = base.rstrip("/")
            oauth.save_tokens(tokens)
    else:
        grant = oauth.start(transport, base)
        print("\nApprove this sign-in in your browser:")
        print(f"  {grant['verification_uri']}")
        print(f"  code: {grant['user_code']}  (expires in {int(grant.get('expires_in', 600)) // 60} min)\n")
        webbrowser.open(grant["verification_uri"])
        tokens = oauth.wait_for_approval(transport, base, grant)
        oauth.save_tokens(tokens)
        print(f"approved: tokens saved to the OS credential vault (values not shown)")

    session = oauth.OAuthSession(transport, base, tokens)
    info = MirisClient(transport, session, base).customer_info()
    print(f"OK: signed in to {info.get('name', 'Miris')}")


if __name__ == "__main__":
    main()
