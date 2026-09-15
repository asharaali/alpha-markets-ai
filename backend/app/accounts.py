"""Account administration, run from a shell on the box.

    python -m app.accounts list
    python -m app.accounts reset <username>      # prompts for the new password
    python -m app.accounts create <username>     # prompts for the password
    python -m app.accounts disable <username>
    python -m app.accounts disable-others <username>

Passwords are typed at a hidden prompt, never passed as arguments, so they do not land in
shell history or process listings.
"""
from __future__ import annotations

import getpass
import sys

from app import auth
from app.core.errors import AlphaError
from app.tracking import store


def _prompt_password() -> str:
    first = getpass.getpass("New password (8+ characters): ")
    if first != getpass.getpass("Repeat it: "):
        raise SystemExit("passwords did not match; nothing changed")
    return first


def main(argv: list[str]) -> int:
    store.init()
    auth.migrate_legacy_users()
    if not argv or argv[0] == "list":
        for name in sorted(auth.all_usernames()):
            status = "disabled" if auth.get_user(name)["hash"] == auth.DISABLED_HASH else "active"
            print(f"{name:24} {status}")
        return 0
    command, name = argv[0], (argv[1] if len(argv) > 1 else "")
    if not name:
        raise SystemExit(__doc__)
    try:
        if command == "reset":
            auth.set_password(name, _prompt_password())
            print(f"password for {name} changed; all its other sessions are signed out")
        elif command == "create":
            created, error = auth.create_user(name, _prompt_password())
            print(error or f"created {created}")
        elif command == "disable":
            print(f"disabled {name}" if auth.disable_user(name) else f"no account named {name}")
        elif command == "disable-others":
            if not auth.get_user(name):
                raise SystemExit(f"no account named {name}; refusing to disable everyone")
            for other in auth.all_usernames():
                if other != name.strip().lower():
                    auth.disable_user(other)
                    print(f"disabled {other}")
        else:
            raise SystemExit(__doc__)
    except AlphaError as exc:
        raise SystemExit(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
