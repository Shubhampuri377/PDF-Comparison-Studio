"""Issue a signed licence key.

    python tools/issue_license.py "Acme Legal" --days 365
    python tools/issue_license.py "Acme Legal" --days 30 --machine 7f3a...c1
    python tools/issue_license.py "Trial User" --days 7 --features compare,report

``--machine`` binds the licence to one computer.  Ask the customer for the
Machine ID shown in the app's Licence dialog and paste it here.  Omit it to
issue an unbound licence.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pdfdiff.licensing import (          # noqa: E402
    ANY_MACHINE, issue_licence, verify_licence, public_key_from_seed)
import base64                            # noqa: E402

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


def load_seed(path: str | None) -> str:
    candidate = (path or os.environ.get("PDFDIFF_SEED_FILE")
                 or find_seed() or DEV_SEED_PATH)
    if os.path.exists(candidate):
        with open(candidate, encoding="utf-8") as fh:
            return fh.read().strip()
    env = os.environ.get("PDFDIFF_SEED")
    if env:
        return env.strip()
    raise SystemExit(
        f"No signing seed found.\n"
        f"  Looked for: {candidate}\n"
        f"  Run 'python tools/keygen.py --write-dev' first, or set "
        f"PDFDIFF_SEED / PDFDIFF_SEED_FILE.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("licensee")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--machine", default=ANY_MACHINE,
                    help="machine ID to bind to, or ANY")
    ap.add_argument("--seats", type=int, default=1)
    ap.add_argument("--features", default="all",
                    help="comma separated, or 'all'")
    ap.add_argument("--note", default="")
    ap.add_argument("--seed-file", default=None)
    args = ap.parse_args()

    seed_b64 = load_seed(args.seed_file)
    features = [f.strip() for f in args.features.split(",") if f.strip()]

    licence = issue_licence(seed_b64, args.licensee, days=args.days,
                            machine=args.machine, seats=args.seats,
                            features=features, note=args.note)

    # Verify against the key derived from this seed so a mismatch with the
    # public key baked into the app is caught here rather than by a customer.
    pub = base64.b64encode(
        public_key_from_seed(base64.b64decode(seed_b64))).decode()
    st = verify_licence(licence, public_key_b64=pub, check_clock=False)

    print(f"Licensee   {st.licensee}")
    print(f"Expires    {st.expires_at:%d %b %Y %H:%M} UTC  ({st.days_left} days)")
    print(f"Machine    {'bound to ' + args.machine if st.machine_bound else 'any'}")
    print(f"Features   {', '.join(st.features)}")
    print(f"Signed by  {pub}")
    print("\nLicence key:\n")
    print(licence)

    from pdfdiff.licensing import PUBLIC_KEY_B64
    if pub != PUBLIC_KEY_B64:
        print("\n!! WARNING: this seed does not match PUBLIC_KEY_B64 in "
              "pdfdiff/licensing.py — the app will reject this key.")


if __name__ == "__main__":
    main()
