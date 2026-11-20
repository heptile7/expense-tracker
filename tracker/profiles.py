"""People who use the app: one private space each, behind a PIN.

Each person ("profile") has a data folder of their own: database, statements, Gmail sign-in,
saved PDF passwords. The person who set the app up (the owner) keeps the original `data/`
folder, so nothing moves; everyone else gets `data/profiles/<slug>/`.

Every web request runs as the signed-in person: the middleware in app.py activates their
profile, which points `db.connect()` and the file paths at their folder (a ContextVar, so
requests and background threads never mix). Without an active profile (tests, the MCP
server) everything uses the owner's folder, as before.

Sign-in is a 4-8 digit PIN, stored only as a salted scrypt hash. Five wrong PINs in a row
lock that person out for 10 minutes. A session is a random token in an HttpOnly cookie; the
registry keeps only its SHA-256, so a copy of the registry can't be used to sign in.
"""

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from tracker import db

log = logging.getLogger(__name__)

SESSION_DAYS = 30
MAX_FAILED = 5
LOCK_SECONDS = 10 * 60
PIN_RE = re.compile(r"^\d{4,8}$")
COOKIE = "et_session"

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    slug          TEXT NOT NULL UNIQUE,
    is_owner      INTEGER NOT NULL DEFAULT 0,
    data_dir      TEXT NOT NULL,                  -- relative to the data folder; '' for the owner
    pin_hash      TEXT NOT NULL,
    pin_salt      TEXT NOT NULL,
    failed        INTEGER NOT NULL DEFAULT 0,
    locked_until  REAL NOT NULL DEFAULT 0,
    language      TEXT NOT NULL DEFAULT 'en',
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    profile_id  INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    created     REAL NOT NULL,
    last_seen   REAL NOT NULL,
    expires     REAL NOT NULL
);
"""


class AuthError(Exception):
    """Wrong PIN, locked out, or not allowed."""


@dataclass(frozen=True)
class Profile:
    id: int
    name: str
    slug: str
    is_owner: bool
    data_dir: Path
    language: str = "en"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "expenses.db" if not self.is_owner else db.get_default_db_path()

    @property
    def inbox(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def gmail_token(self) -> Path:
        return self.data_dir / "gmail_token.json"

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "is_owner": self.is_owner, "language": self.language}


_current: ContextVar[Profile | None] = ContextVar("profile", default=None)


# ── The active person ─────────────────────────────────────────────────────────


def current() -> Profile | None:
    return _current.get()


def is_owner_or_none() -> bool:
    """True for the owner, and outside any request (tests, MCP server): those use the owner's data."""
    p = current()
    return p is None or p.is_owner


@contextmanager
def use(profile: Profile | None) -> Iterator[None]:
    """Run as `profile`: database and file paths point at their folder. Also for background threads."""
    token = _current.set(profile)
    with db.use_path(profile.db_path if profile else None):
        try:
            yield
        finally:
            _current.reset(token)


def base_dir() -> Path:
    """The data folder: the owner's space, the registry, and profiles/ for everyone else."""
    return db.get_default_db_path().parent


def data_dir() -> Path:
    p = current()
    return p.data_dir if p else base_dir()


def inbox_dir() -> Path:
    return data_dir() / "inbox"


def gmail_token_path() -> Path:
    return data_dir() / "gmail_token.json"


# ── Registry ──────────────────────────────────────────────────────────────────


def registry_path() -> Path:
    return base_dir() / "profiles.db"


def _connect() -> sqlite3.Connection:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def _registry() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _profile(row: sqlite3.Row) -> Profile:
    folder = base_dir() / row["data_dir"] if row["data_dir"] else base_dir()
    return Profile(row["id"], row["name"], row["slug"], bool(row["is_owner"]), folder, row["language"])


def setup_needed() -> bool:
    with _registry() as conn:
        return conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0


def list_profiles() -> list[Profile]:
    with _registry() as conn:
        return [_profile(r) for r in conn.execute("SELECT * FROM profiles ORDER BY is_owner DESC, name")]


def get(profile_id: int) -> Profile:
    with _registry() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if not row:
        raise AuthError("No such person")
    return _profile(row)


def init_all() -> None:
    """Create or upgrade every person's database (run at start-up, after the owner's)."""
    for p in list_profiles():
        if not p.is_owner:
            with use(p):
                db.init_db()


# ── PINs ──────────────────────────────────────────────────────────────────────


def _check_pin_format(pin: str) -> str:
    pin = (pin or "").strip()
    if not PIN_RE.match(pin):
        raise ValueError("A PIN is 4 to 8 digits")
    return pin


def _hash(pin: str, salt: bytes) -> str:
    return hashlib.scrypt(pin.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32).hex()


def _new_pin(pin: str) -> tuple[str, str]:
    salt = secrets.token_bytes(16)
    return _hash(_check_pin_format(pin), salt), salt.hex()


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not 1 <= len(name) <= 40:
        raise ValueError("A name is 1 to 40 characters")
    return name


def _slug(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "person"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def create_owner(name: str, pin: str) -> Profile:
    """First set-up: the person running the app. Their space is the existing data folder."""
    pin_hash, salt = _new_pin(pin)
    with _registry() as conn:
        if conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]:
            raise AuthError("The app is already set up")
        name = _clean_name(name)
        cur = conn.execute(
            "INSERT INTO profiles (name, slug, is_owner, data_dir, pin_hash, pin_salt) VALUES (?, ?, 1, '', ?, ?)",
            (name, _slug(name, set()), pin_hash, salt),
        )
        profile_id = cur.lastrowid
    log.info("Set up sign-in: owner profile %s", profile_id)
    return get(profile_id)


def create_profile(name: str, pin: str) -> Profile:
    """Someone else: a new, empty space under data/profiles/."""
    pin_hash, salt = _new_pin(pin)
    name = _clean_name(name)
    with _registry() as conn:
        if conn.execute("SELECT 1 FROM profiles WHERE name = ?", (name,)).fetchone():
            raise ValueError(f"There's already someone called {name}")
        taken = {r[0] for r in conn.execute("SELECT slug FROM profiles")}
        slug = _slug(name, taken)
        cur = conn.execute(
            "INSERT INTO profiles (name, slug, is_owner, data_dir, pin_hash, pin_salt) VALUES (?, ?, 0, ?, ?, ?)",
            (name, slug, f"profiles/{slug}", pin_hash, salt),
        )
        profile_id = cur.lastrowid
    profile = get(profile_id)
    profile.data_dir.mkdir(parents=True, exist_ok=True)
    with use(profile):
        db.init_db()
    log.info("Added profile %s", profile_id)
    return profile


def update_profile(profile_id: int, name: str | None = None, pin: str | None = None, language: str | None = None) -> Profile:
    sets, params = [], []
    if name is not None:
        sets.append("name = ?")
        params.append(_clean_name(name))
    if pin is not None:
        pin_hash, salt = _new_pin(pin)
        sets += ["pin_hash = ?", "pin_salt = ?", "failed = 0", "locked_until = 0"]
        params += [pin_hash, salt]
    if language is not None:
        if language not in ("en", "ta"):
            raise ValueError("language must be en or ta")
        sets.append("language = ?")
        params.append(language)
    if sets:
        with _registry() as conn:
            try:
                conn.execute(f"UPDATE profiles SET {', '.join(sets)} WHERE id = ?", (*params, profile_id))
            except sqlite3.IntegrityError as exc:
                raise ValueError("There's already someone with that name") from exc
            if pin is not None:  # a new PIN signs out every other device
                conn.execute("DELETE FROM sessions WHERE profile_id = ?", (profile_id,))
    return get(profile_id)


def check_pin(profile_id: int, pin: str) -> Profile:
    """The PIN is right -> the profile. Counts failures and locks after MAX_FAILED in a row."""
    now = time.time()
    error = None
    with _registry() as conn:  # the failure count must be saved, so the error is raised after the commit
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if not row:
            error = "Wrong name or PIN"
        elif row["locked_until"] > now:
            minutes = max(1, round((row["locked_until"] - now) / 60))
            error = f"Too many wrong PINs. Try again in {minutes} minute{'s' if minutes != 1 else ''}."
        elif not (PIN_RE.match((pin or "").strip()) and hmac.compare_digest(
                _hash(pin.strip(), bytes.fromhex(row["pin_salt"])), row["pin_hash"])):
            failed = row["failed"] + 1
            locked = now + LOCK_SECONDS if failed >= MAX_FAILED else 0
            conn.execute("UPDATE profiles SET failed = ?, locked_until = ? WHERE id = ?",
                         (0 if locked else failed, locked, profile_id))
            log.warning("Wrong PIN for profile %s (%d in a row)%s", profile_id, failed, "; locked for 10 minutes" if locked else "")
            left = MAX_FAILED - failed
            error = ("Too many wrong PINs. Try again in 10 minutes." if locked
                     else f"Wrong PIN. {left} tr{'ies' if left != 1 else 'y'} left.")
        else:
            conn.execute("UPDATE profiles SET failed = 0, locked_until = 0 WHERE id = ?", (profile_id,))
    if error:
        raise AuthError(error)
    return _profile(row)


# ── Sessions ──────────────────────────────────────────────────────────────────


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(profile: Profile) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _registry() as conn:
        conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        conn.execute(
            "INSERT INTO sessions (token_hash, profile_id, created, last_seen, expires) VALUES (?, ?, ?, ?, ?)",
            (_token_hash(token), profile.id, now, now, now + SESSION_DAYS * 86400),
        )
    log.info("Signed in: profile %s", profile.id)
    return token


def from_session(token: str | None) -> Profile | None:
    if not token:
        return None
    now = time.time()
    with _registry() as conn:
        row = conn.execute(
            """SELECT p.*, s.last_seen AS s_seen FROM sessions s JOIN profiles p ON p.id = s.profile_id
               WHERE s.token_hash = ? AND s.expires > ?""",
            (_token_hash(token), now),
        ).fetchone()
        if not row:
            return None
        if now - row["s_seen"] > 300:  # sliding expiry, written at most every 5 minutes
            conn.execute("UPDATE sessions SET last_seen = ?, expires = ? WHERE token_hash = ?",
                         (now, now + SESSION_DAYS * 86400, _token_hash(token)))
    return _profile(row)


def end_session(token: str | None) -> None:
    if token:
        with _registry() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
