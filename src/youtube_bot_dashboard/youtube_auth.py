"""One-time local OAuth setup for reading the owner's YouTube live chat."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

YOUTUBE_READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"


def main() -> None:
    load_dotenv()
    secrets_file = Path(os.getenv("YOUTUBE_CLIENT_SECRETS_FILE", "client_secret.json"))
    token_file = Path(os.getenv("YOUTUBE_TOKEN_FILE", ".youtube-oauth-token.json"))

    if not secrets_file.is_file():
        raise SystemExit(
            f"OAuth client file not found: {secrets_file}. Create a Desktop OAuth client "
            "for YouTube Data API v3 and set YOUTUBE_CLIENT_SECRETS_FILE."
        )

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise SystemExit('Install the YouTube extras with: pip install -e ".[youtube]"') from exc

    flow = InstalledAppFlow.from_client_secrets_file(
        str(secrets_file),
        scopes=[YOUTUBE_READ_SCOPE],
    )
    credentials = flow.run_local_server(port=0, open_browser=True)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")
    try:
        token_file.chmod(0o600)
    except OSError:
        # Some Windows filesystems do not support POSIX permissions.
        pass
    print(f"YouTube read-only authorization saved to {token_file}. Keep this file secret.")


if __name__ == "__main__":
    main()
