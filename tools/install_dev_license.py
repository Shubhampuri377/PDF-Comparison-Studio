"""Issue a development licence and install it for the current user.

    python tools/install_dev_license.py
    python tools/install_dev_license.py --days 30 --bind

Signs with your own seed (``tools/signing_seed.txt``), so the result is a
real licence -- just self-issued rather than sold.

Run with ``--diagnose`` alone to print where the app looks for the licence and
why it is or is not being accepted.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pdfdiff.licensing import (               # noqa: E402
    ANY_MACHINE, CLOCK_FILE, LICENCE_FILE, PUBLIC_KEY_B64, current_status,
    issue_licence, load_saved_licence, machine_id, public_key_from_seed,
    save_licence, short_machine_id, verify_licence)

HERE = os.path.dirname(os.path.abspath(__file__))
# Production seed first, development seed second. Both are gitignored via
# the tools/*_seed.txt rule.
SEED_CANDIDATES = (os.path.join(HERE, "signing_seed.txt"),
                   os.path.join(HERE, "dev_seed.txt"))
DEV_SEED_PATH = SEED_CANDIDATES[0]


def find_seed() -> str | None:
    """First existing seed file, or None."""
    for path in SEED_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def diagnose() -> int:
    import base64
    import getpass
    import platform

    print("environment")
    print(f"  user                {getpass.getuser()}")
    print(f"  python              {sys.executable}")
    print(f"  platform            {platform.platform()}")
    print(f"  LOCALAPPDATA        {os.environ.get('LOCALAPPDATA', '<unset>')}")
    print(f"  APPDATA             {os.environ.get('APPDATA', '<unset>')}")
    print()
    print("licence storage")
    print(f"  licence file        {LICENCE_FILE}")
    print(f"  exists              {os.path.exists(LICENCE_FILE)}")
    if os.path.exists(LICENCE_FILE):
        print(f"  size                {os.path.getsize(LICENCE_FILE)} bytes")
        try:
            with open(LICENCE_FILE, encoding="utf-8") as fh:
                raw = fh.read()
            print(f"  readable            yes ({len(raw.strip())} chars)")
        except OSError as exc:
            print(f"  readable            NO -- {exc}")
    print(f"  clock file          {os.path.exists(CLOCK_FILE)}")
    print()
    print("machine")
    print(f"  machine id          {short_machine_id()}")
    print()
    print("keys")
    print(f"  app public key      {PUBLIC_KEY_B64}")
    seed_path = find_seed()
    if seed_path:
        with open(seed_path, encoding="utf-8") as fh:
            seed_b64 = fh.read().strip()
        derived = base64.b64encode(
            public_key_from_seed(base64.b64decode(seed_b64))).decode()
        print(f"  seed derives        {derived}")
        print(f"  seed matches app    {derived == PUBLIC_KEY_B64}")
    else:
        print("  signing seed        MISSING (tools/signing_seed.txt)")
    print()

    saved = load_saved_licence()
    if not saved:
        print("verdict: no licence key on disk for this user")
        print("         run this script without --diagnose to install one")
        return 1

    # Report the clock check separately: it is the one reason a previously
    # working licence starts failing without anything else changing.
    without_clock = verify_licence(saved, check_clock=False)
    with_clock = current_status()
    print(f"signature + expiry  {'OK' if without_clock.valid else without_clock.reason}")
    print(f"including clock     {'OK' if with_clock.valid else with_clock.reason}")
    if without_clock.valid:
        print(f"licensed to         {without_clock.licensee}")
        print(f"expires             {without_clock.expires_at:%d %b %Y %H:%M} UTC"
              f"  ({without_clock.days_left} days)")
        print(f"machine bound       {without_clock.machine_bound}")
    return 0 if with_clock.valid else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--name", default="Development")
    ap.add_argument("--bind", action="store_true",
                    help="bind the licence to this machine")
    ap.add_argument("--diagnose", action="store_true",
                    help="report why the licence is or is not accepted")
    args = ap.parse_args()

    if args.diagnose:
        return diagnose()

    seed_path = find_seed()
    if seed_path is None:
        print(f"No signing seed in {HERE}")
        print("Run: python tools/keygen.py --write-dev")
        print("Then paste the printed public key into "
              "pdfdiff/licensing.py PUBLIC_KEY_B64")
        return 1

    with open(seed_path, encoding="utf-8") as fh:
        seed_b64 = fh.read().strip()

    licence = issue_licence(seed_b64, args.name, days=args.days,
                            machine=machine_id() if args.bind else ANY_MACHINE)
    save_licence(licence)

    st = current_status()
    if st.valid:
        print(f"Installed: {st.licensee}, expires "
              f"{st.expires_at:%d %b %Y} ({st.days_left} days)")
        print(f"Written to {LICENCE_FILE}")
        return 0

    print(f"Installed but rejected: {st.reason}")
    print("Run with --diagnose for details.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
