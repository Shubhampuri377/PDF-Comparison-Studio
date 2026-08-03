"""Confirm a checkout is intact and self-consistent.

Useful after moving or copying the project, or on a new machine: it checks the
things that break silently rather than loudly -- a signing key that no longer
matches the build, a virtual environment left behind, a path the tooling
cannot handle.
"""
from __future__ import annotations

import base64
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_fail = 0


def ok(msg: str):
    print(f"  [ok]   {msg}")


def bad(msg: str, fix: str = ""):
    global _fail
    _fail += 1
    print(f"  [FAIL] {msg}")
    if fix:
        print(f"         -> {fix}")


def warn(msg: str):
    print(f"  [warn] {msg}")


def main() -> int:
    print("project")
    print(f"  root        {ROOT}")
    print(f"  python      {sys.executable}")
    if " " in ROOT:
        # Not a problem in itself, but it is the reason plain shell one-liners
        # need quoting; the .bat files already use %~dp0 with quotes.
        warn("the path contains spaces — always quote it in shell commands")
    print()

    print("layout")
    for rel in ("app.py", "app.spec", "run.bat", "build.bat",
                "pdfdiff", "tests", "tools", "assets/icon.ico",
                "requirements.txt", ".gitignore"):
        path = os.path.join(ROOT, rel)
        (ok if os.path.exists(path) else bad)(rel)
    print()

    print("environment")
    venv = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    if os.path.exists(venv):
        ok(".venv present")
        # sys.prefix is <root>/.venv, so one dirname reaches the project root.
        if os.path.normcase(os.path.dirname(sys.prefix)) \
                != os.path.normcase(ROOT):
            warn(f"running under {sys.prefix}, not the project's .venv")
        else:
            ok("running under the project's own .venv")
    else:
        warn(".venv not found — run.bat will fall back to the system Python")
    missing = []
    for mod, label in (("PySide6", "PySide6"), ("pymupdf", "PyMuPDF"),
                       ("PIL", "Pillow")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(label)
    if missing:
        bad(f"missing: {', '.join(missing)}",
            "python -m pip install -r requirements.txt")
    else:
        ok("PySide6, PyMuPDF and Pillow import")
    print()

    print("licensing")
    seed_path = None
    for name in ("signing_seed.txt", "dev_seed.txt"):
        cand = os.path.join(HERE, name)
        if os.path.exists(cand):
            seed_path = cand
            break
    if seed_path is None:
        bad("no signing seed in tools/",
            "python tools/keygen.py --write-dev")
    else:
        try:
            seed = base64.b64decode(open(seed_path, encoding="utf-8").read().strip())
            fp = hashlib.blake2b(seed, digest_size=6).hexdigest().upper()
            grouped = "-".join(fp[i:i + 4] for i in range(0, 12, 4))
            ok(f"signing seed present, fingerprint {grouped}")

            from pdfdiff.licensing import PUBLIC_KEY_B64, public_key_from_seed
            derived = base64.b64encode(public_key_from_seed(seed)).decode()
            if derived == PUBLIC_KEY_B64:
                ok("seed matches the public key compiled into the app")
            else:
                bad("seed does NOT match PUBLIC_KEY_B64",
                    f"set it to {derived} in pdfdiff/licensing.py")
        except Exception as exc:
            bad(f"could not read the seed: {exc}")

    try:
        from pdfdiff.licensing import current_status, short_machine_id
        st = current_status()
        ok(f"machine id {short_machine_id()}")
        if st.valid:
            ok(f"licence active — {st.licensee}, {st.days_left} days left")
        else:
            warn(f"no valid licence: {st.reason}")
    except Exception as exc:
        bad(f"licence check failed: {exc}")
    print()

    print("engine")
    try:
        from pdfdiff.engine import ComparisonEngine  # noqa: F401
        from pdfdiff.ui.main import MainWindow       # noqa: F401
        ok("all modules import")
    except Exception as exc:
        bad(f"import failed: {type(exc).__name__}: {exc}")

    print()
    if _fail:
        print(f"{_fail} problem(s) found")
        return 1
    print("Everything checks out. Run the app with run.bat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
