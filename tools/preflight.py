"""Checks that run before building a distributable executable.

Each of these guards against a mistake that is cheap to make and expensive to
discover after the .exe has already gone out to a customer.
"""
from __future__ import annotations

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")

_failures: list[str] = []
_warnings: list[str] = []


def ok(msg: str):
    print(f"      [ok]   {msg}")


def fail(msg: str, fix: str = ""):
    print(f"      [FAIL] {msg}")
    if fix:
        print(f"             -> {fix}")
    _failures.append(msg)


def warn(msg: str, fix: str = ""):
    print(f"      [warn] {msg}")
    if fix:
        print(f"             -> {fix}")
    _warnings.append(msg)


def check_dependencies():
    missing = []
    for mod, label in (("PySide6", "PySide6"), ("pymupdf", "PyMuPDF"),
                       ("PIL", "Pillow")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(label)
    if missing:
        fail(f"missing runtime dependencies: {', '.join(missing)}",
             "python -m pip install -r requirements.txt")
    else:
        ok("runtime dependencies present")


def check_signing_key():
    """The build must trust a key whose private half you actually hold.

    Shipping an executable whose public key does not match your signing seed
    produces a binary you can never issue a licence for -- and you only find
    out when the first customer's key is rejected.
    """
    from pdfdiff.licensing import PUBLIC_KEY_B64, public_key_from_seed

    seed_path = None
    for name in ("signing_seed.txt", "dev_seed.txt"):
        candidate = os.path.join(TOOLS, name)
        if os.path.exists(candidate):
            seed_path = candidate
            break

    if seed_path is None:
        warn("no signing seed found in tools/",
             "python tools/keygen.py --write-dev  (then update PUBLIC_KEY_B64)")
        return

    try:
        with open(seed_path, encoding="utf-8") as fh:
            seed = base64.b64decode(fh.read().strip())
        derived = base64.b64encode(public_key_from_seed(seed)).decode()
    except Exception as exc:
        fail(f"could not read the signing seed: {exc}")
        return

    if derived != PUBLIC_KEY_B64:
        fail("the app's public key does not match your signing seed",
             f"set PUBLIC_KEY_B64 in pdfdiff/licensing.py to {derived}")
    else:
        ok(f"signing key matches the build ({os.path.basename(seed_path)})")


def check_seed_not_bundled():
    """The private seed must never end up inside the executable."""
    spec = os.path.join(ROOT, "app.spec")
    try:
        with open(spec, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        warn("app.spec not readable")
        return
    for needle in ("signing_seed", "dev_seed", "_seed.txt"):
        if needle in text:
            fail(f"app.spec references {needle!r} - the private key would be "
                 f"bundled into the .exe",
                 "remove it from the spec's datas before building")
            return
    ok("private signing seed is not referenced by app.spec")


def check_no_stray_imports():
    """The spec excludes heavy packages; make sure nothing actually needs them."""
    import ast

    excluded = {"numpy", "pandas", "scipy", "matplotlib", "tkinter",
                "streamlit", "PyQt5", "PyQt6"}
    offenders = []
    for base, dirs, files in os.walk(os.path.join(ROOT, "pdfdiff")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(base, f)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read(), path)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                for n in names:
                    if n in excluded:
                        offenders.append(
                            f"{os.path.relpath(path, ROOT)} imports {n}")
    if offenders:
        for o in sorted(set(offenders)):
            fail(o, "app.spec excludes this package; the build will crash")
    else:
        ok("no imports of packages the spec excludes")


def check_entry_guard():
    """Windows workers re-import __main__; without a guard the app forks itself."""
    path = os.path.join(ROOT, "app.py")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        fail("app.py not found")
        return
    if 'if __name__ == "__main__"' not in text:
        fail("app.py has no __main__ guard",
             "multiprocessing workers would relaunch the whole app")
    elif "freeze_support" not in text:
        fail("app.py does not call multiprocessing.freeze_support()",
             "a frozen build spawns a new window per worker without it")
    else:
        ok("entry point guards multiprocessing correctly")


def main() -> int:
    check_dependencies()
    check_entry_guard()
    check_signing_key()
    check_seed_not_bundled()
    check_no_stray_imports()

    print()
    if _failures:
        print(f"      {len(_failures)} check(s) failed - build aborted")
        return 1
    if _warnings:
        print(f"      passed with {len(_warnings)} warning(s)")
    else:
        print("      all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
