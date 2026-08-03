"""Licence issuing and verification.

Design note, because the previous scheme had a real hole in it: the old build
embedded one HMAC secret that both signed *and* verified licences, and shipped
it inside the executable.  Anybody who unpacked the .exe could mint unlimited
keys.

Here the application holds only an Ed25519 **public** key.  The private seed
lives in ``tools/`` on the issuer's machine and is never distributed.  Cracking
the binary now reveals nothing that helps forge a licence -- the worst case is
someone patching out the check, which no client-side scheme can prevent.

Licence format (JWT-ish, deliberately inspectable):

    base64url(payload_json) . base64url(ed25519_signature)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# Prefer the audited implementation when it happens to be installed.
try:                                    # pragma: no cover
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey)
    from cryptography.exceptions import InvalidSignature

    _HAVE_CRYPTOGRAPHY = True
except ImportError:                     # pragma: no cover
    _HAVE_CRYPTOGRAPHY = False

from . import _ed25519

# --------------------------------------------------------------------------
# The distribution public key.
#
# Regenerate with `python tools/keygen.py --write-dev` and paste the printed
# public key here. The matching private seed lives in tools/signing_seed.txt,
# which is gitignored and must never be distributed.
# --------------------------------------------------------------------------
PUBLIC_KEY_B64 = "nKrOE/mVq+mZhHU54UKQWNANZ5grc32SC59EoZMyekw="

LICENCE_VERSION = 1
ANY_MACHINE = "ANY"

APP_DIR_NAME = "PDFComparisonStudio"


def _app_data_dir() -> str:
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            or os.environ.get("XDG_CONFIG_HOME")
            or os.path.expanduser("~/.config"))
    path = os.path.join(base, APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


LICENCE_FILE = os.path.join(_app_data_dir(), "licence.key")
CLOCK_FILE = os.path.join(_app_data_dir(), "clock.dat")


# --------------------------------------------------------------------------
# base64url without padding
# --------------------------------------------------------------------------
def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


# --------------------------------------------------------------------------
# Hardware identity
# --------------------------------------------------------------------------
def _windows_machine_guid() -> str:
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        try:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value)
        finally:
            winreg.CloseKey(key)
    except Exception:
        return ""


def _windows_volume_serial() -> str:
    try:
        import ctypes

        serial = ctypes.c_ulong(0)
        root = os.environ.get("SystemDrive", "C:") + "\\"
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), None, 0, ctypes.byref(serial),
            None, None, None, 0)
        return f"{serial.value:08X}" if ok else ""
    except Exception:
        return ""


def machine_id() -> str:
    """Stable per-machine identifier.

    The old implementation hashed ``platform.node() + system + processor``.
    ``processor`` returns the same generic string on thousands of machines and
    ``node`` is just the hostname, which a user can change in Settings -- so
    the binding was close to meaningless.  Windows MachineGuid plus the system
    volume serial is far harder to collide with or edit by accident.
    """
    parts: list[str] = []
    if platform.system() == "Windows":
        parts.append(_windows_machine_guid())
        parts.append(_windows_volume_serial())
    else:
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                with open(path) as fh:
                    parts.append(fh.read().strip())
                    break
            except OSError:
                continue
    parts = [p for p in parts if p]
    if not parts:                     # last resort, better than nothing
        parts = [platform.node(), platform.system()]
    raw = "|".join(parts)
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=16).hexdigest()


def short_machine_id() -> str:
    """Grouped form to read out over the phone when requesting a licence."""
    mid = machine_id().upper()
    return "-".join(mid[i:i + 5] for i in range(0, 20, 5))


# Minimum length for a bound identifier. The displayed short form is 20 hex
# characters (80 bits) which is far beyond collision risk; this only guards
# against a truncated or mistyped value matching half the world.
_MIN_MACHINE_ID_CHARS = 16


def canonical_machine_id(value: str) -> str:
    """Strip grouping and case so the two display forms compare equal."""
    return "".join(ch for ch in (value or "") if ch.isalnum()).lower()


def machine_matches(bound: str, local: str | None = None) -> bool:
    """Does a bound machine identifier refer to this computer?

    The licence dialog shows a shortened, dash-grouped, upper-case form of the
    machine hash, and that is the string customers copy and suppliers paste
    into ``--machine``.  Comparing it literally against the full lower-case
    hash never matches, which silently made machine binding unusable.  Both
    forms are accepted here, the short one as a prefix of the full hash.
    """
    if not bound or bound == ANY_MACHINE:
        return True
    wanted = canonical_machine_id(bound)
    if len(wanted) < _MIN_MACHINE_ID_CHARS:
        return False
    actual = canonical_machine_id(local if local is not None else machine_id())
    return actual == wanted or actual.startswith(wanted)


# --------------------------------------------------------------------------
# Signing / verifying
# --------------------------------------------------------------------------
def _sign_bytes(seed: bytes, message: bytes) -> bytes:
    if _HAVE_CRYPTOGRAPHY:
        return Ed25519PrivateKey.from_private_bytes(seed).sign(message)
    return _ed25519.sign(seed, message)


def _verify_bytes(pub: bytes, message: bytes, signature: bytes) -> bool:
    if _HAVE_CRYPTOGRAPHY:
        try:
            Ed25519PublicKey.from_public_bytes(pub).verify(signature, message)
            return True
        except (InvalidSignature, ValueError):
            return False
    return _ed25519.verify(pub, message, signature)


def public_key_from_seed(seed: bytes) -> bytes:
    if _HAVE_CRYPTOGRAPHY:
        return Ed25519PrivateKey.from_private_bytes(seed).public_key(
        ).public_bytes_raw()
    return _ed25519.public_key(seed)


def issue_licence(seed_b64: str, licensee: str, days: int = 365,
                  machine: str = ANY_MACHINE, seats: int = 1,
                  features: list[str] | None = None,
                  note: str = "") -> str:
    """Create a signed licence string.  Issuer side only."""
    seed = base64.b64decode(seed_b64)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = {
        "v": LICENCE_VERSION,
        "sub": licensee,
        "iat": now.isoformat(),
        "exp": (now + timedelta(days=days)).isoformat(),
        "mid": machine or ANY_MACHINE,
        "seats": seats,
        "feat": features or ["all"],
        "id": _b64e(os.urandom(9)),
    }
    if note:
        payload["note"] = note
    body = json.dumps(payload, separators=(",", ":"),
                      sort_keys=True).encode("utf-8")
    signature = _sign_bytes(seed, body)
    return f"{_b64e(body)}.{_b64e(signature)}"


# --------------------------------------------------------------------------
# Clock rollback detection
# --------------------------------------------------------------------------
def _read_last_seen() -> datetime | None:
    """Highest timestamp we have previously recorded.

    Written to two places -- a file and HKCU -- and the newer of the two wins.
    This is deliberately described as tamper *evident*, not tamper proof: a
    determined user can clear both.  It stops the casual "set the clock back a
    month" bypass, which is what it is for.
    """
    stamps: list[datetime] = []
    try:
        with open(CLOCK_FILE, "r") as fh:
            stamps.append(datetime.fromisoformat(fh.read().strip()))
    except Exception:
        pass
    if platform.system() == "Windows":
        try:
            import winreg

            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 rf"Software\{APP_DIR_NAME}", 0, winreg.KEY_READ)
            try:
                value, _ = winreg.QueryValueEx(key, "LastSeen")
                stamps.append(datetime.fromisoformat(str(value)))
            finally:
                winreg.CloseKey(key)
        except Exception:
            pass
    stamps = [s for s in stamps if s.tzinfo is not None]
    return max(stamps) if stamps else None


def _write_last_seen(when: datetime):
    text = when.isoformat()
    try:
        with open(CLOCK_FILE, "w") as fh:
            fh.write(text)
    except OSError:
        pass
    if platform.system() == "Windows":
        try:
            import winreg

            key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                     rf"Software\{APP_DIR_NAME}", 0,
                                     winreg.KEY_WRITE)
            try:
                winreg.SetValueEx(key, "LastSeen", 0, winreg.REG_SZ, text)
            finally:
                winreg.CloseKey(key)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Result type
# --------------------------------------------------------------------------
@dataclass
class LicenceStatus:
    valid: bool = False
    reason: str = ""
    licensee: str = ""
    expires_at: datetime | None = None
    issued_at: datetime | None = None
    machine_bound: bool = False
    features: list[str] = field(default_factory=list)
    licence_id: str = ""
    note: str = ""

    @property
    def seconds_left(self) -> float:
        if not self.expires_at:
            return 0.0
        return (self.expires_at - datetime.now(timezone.utc)).total_seconds()

    @property
    def days_left(self) -> int:
        return max(0, int(self.seconds_left // 86400))

    def countdown(self) -> str:
        secs = int(max(0, self.seconds_left))
        days, rem = divmod(secs, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        if days:
            return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def has(self, feature: str) -> bool:
        return "all" in self.features or feature in self.features


def verify_licence(licence: str, public_key_b64: str | None = None,
                   check_clock: bool = True) -> LicenceStatus:
    """Validate a licence string and explain precisely why if it fails."""
    st = LicenceStatus()
    if not licence or "." not in licence:
        st.reason = "Licence key is empty or malformed"
        return st

    try:
        body_b64, sig_b64 = licence.strip().split(".", 1)
        body = _b64d(body_b64)
        signature = _b64d(sig_b64)
    except Exception:
        st.reason = "Licence key is not valid base64"
        return st

    pub = base64.b64decode(public_key_b64 or PUBLIC_KEY_B64)
    if not _verify_bytes(pub, body, signature):
        st.reason = "Signature does not match — licence was not issued by us"
        return st

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        st.reason = "Licence payload is corrupt"
        return st

    if payload.get("v") != LICENCE_VERSION:
        st.reason = f"Unsupported licence version {payload.get('v')!r}"
        return st

    st.licensee = payload.get("sub", "")
    st.features = list(payload.get("feat") or [])
    st.licence_id = payload.get("id", "")
    st.note = payload.get("note", "")

    try:
        st.issued_at = datetime.fromisoformat(payload["iat"])
        st.expires_at = datetime.fromisoformat(payload["exp"])
    except Exception:
        st.reason = "Licence dates are unreadable"
        return st

    # Everything below compares in UTC.  The old build used naive local
    # datetimes, so changing the machine timezone shifted the expiry.
    if st.expires_at.tzinfo is None:
        st.expires_at = st.expires_at.replace(tzinfo=timezone.utc)
    if st.issued_at.tzinfo is None:
        st.issued_at = st.issued_at.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    if now > st.expires_at:
        st.reason = f"Licence expired on {st.expires_at:%d %b %Y}"
        return st

    bound = payload.get("mid", ANY_MACHINE)
    st.machine_bound = bound != ANY_MACHINE
    if st.machine_bound and not machine_matches(bound):
        st.reason = ("Licence is registered to a different machine "
                     f"(this one is {short_machine_id()})")
        return st

    if check_clock:
        last = _read_last_seen()
        if last is not None and now < last - timedelta(hours=6):
            st.reason = (f"System clock was moved backwards "
                         f"(last run {last:%d %b %Y %H:%M} UTC)")
            return st
        _write_last_seen(max(now, last) if last else now)

    st.valid = True
    st.reason = "OK"
    return st


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
def load_saved_licence() -> str:
    try:
        with open(LICENCE_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def save_licence(licence: str):
    with open(LICENCE_FILE, "w", encoding="utf-8") as fh:
        fh.write(licence.strip())


def clear_licence():
    try:
        os.remove(LICENCE_FILE)
    except OSError:
        pass


def current_status(check_clock: bool = True) -> LicenceStatus:
    saved = load_saved_licence()
    if not saved:
        st = LicenceStatus()
        st.reason = "No licence installed"
        return st
    return verify_licence(saved, check_clock=check_clock)
