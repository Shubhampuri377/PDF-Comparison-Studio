"""Generate an Ed25519 signing keypair for licence issuing.

    python tools/keygen.py                 # print a new keypair
    python tools/keygen.py --write-dev     # also drop a dev seed next to this

The PRIVATE SEED must never be committed to a public repo, bundled into the
executable, or emailed.  Only the public key goes into
``pdfdiff/licensing.py``.  Anyone holding the seed can mint licences.
"""
from __future__ import annotations

import argparse
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pdfdiff import _ed25519            # noqa: E402
from pdfdiff.licensing import public_key_from_seed   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEV_SEED_PATH = os.path.join(HERE, "signing_seed.txt")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write-dev", action="store_true",
                    help="write tools/signing_seed.txt")
    ap.add_argument("--out", help="write the private seed to this path")
    args = ap.parse_args()

    seed = _ed25519.generate_seed()
    seed_b64 = base64.b64encode(seed).decode()
    pub_b64 = base64.b64encode(public_key_from_seed(seed)).decode()

    print("Private seed (KEEP SECRET, never ship):")
    print(f"  {seed_b64}")
    print("\nPublic key — paste into pdfdiff/licensing.py PUBLIC_KEY_B64:")
    print(f"  {pub_b64}")

    target = args.out or (DEV_SEED_PATH if args.write_dev else None)
    if target:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(seed_b64 + "\n")
        try:                              # best effort on POSIX
            os.chmod(target, 0o600)
        except OSError:
            pass
        print(f"\nSeed written to {target}")
        print("Add it to .gitignore if it is not already there.")


if __name__ == "__main__":
    main()
