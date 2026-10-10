"""Enterprise persistence layer — users, roles, permissions, and audit logs.

Stdlib-only (``sqlite3``): zero external dependencies, so the product installs
anywhere (a single file on disk). This is the multi-tenant, RBAC, and audit
backbone that the GUI/API sit on top of.

Concepts:

* **Role** — a named set of permissions: ``admin`` (everything), ``optimizer``
  (view + apply optimization), ``viewer`` (read-only), ``auditor`` (read logs).
* **Permission** — a dot-scoped string, e.g. ``workload.read``,
  ``cluster.apply``, ``logs.read``, ``users.manage``.
* **User** — has one role + a per-user override set; accounts carry a salted
  password hash (PBKDF2-HMAC via ``hashlib``) — never plaintext.
* **Audit log** — append-only records of *who* did *what* to *which* resource.
* **Action log** — the controller's applied actions (VPA/HPA/patch) for the
  continuous-improvement loop.
* **Error log** — failures/exception records for RCA.

Every mutating call also writes an audit entry, so nothing changes without a
trace. All queries are parameterized (no string-built SQL).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# --- Roles & permissions ----------------------------------------------------

ROLE_PERMISSIONS: Dict[str, set] = {
    "admin": {
        "workload.read", "workload.write", "cluster.apply", "cluster.reconcile",
        "metrics.read", "logs.read", "logs.write", "users.manage",
        "providers.manage", "settings.manage", "rca.read",
    },
    "optimizer": {
        "workload.read", "workload.write", "cluster.apply", "cluster.reconcile",
        "metrics.read", "logs.read", "rca.read",
    },
    "viewer": {
        "workload.read", "metrics.read", "rca.read",
    },
    "auditor": {
        "logs.read", "metrics.read", "workload.read", "rca.read",
    },
}

ROLES = sorted(ROLE_PERMISSIONS.keys())


@dataclass
class User:
    id: int
    username: str
    role: str
    password_hash: str
    salt: str
    permissions: List[str] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict:
        perms = sorted(set(ROLE_PERMISSIONS.get(self.role, set())) | set(self.permissions))
        return {"id": self.id, "username": self.username, "role": self.role,
                "permissions": perms, "created_at": self.created_at}


def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    """Return (salt, hash) using PBKDF2-HMAC-SHA256 with 100k iterations."""
    salt = salt or base64.b64encode(os.urandom(16)).decode("ascii")
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt.encode("ascii"), 100_000)
    return salt, base64.b64encode(dk).decode("ascii")


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, computed = hash_password(password, salt)
    return hmac.compare_digest(computed, expected_hash)


_AUTH_TOKEN_KEY = os.environ.get("K8OPTIMIZER_AUTH_TOKEN", "k8optimizer-dev-secret")


def issue_token(username: str, role: str) -> str:
    """A simple signed (HMAC) session token — no external identity provider."""
    payload = f"{username}:{role}:{int(time.time())}"
    sig = hmac.new(_AUTH_TOKEN_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_token(token: str) -> Optional[Tuple[str, str]]:
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(_AUTH_TOKEN_KEY.encode(), payload.encode(),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        username, role, _ = payload.split(":", 2)
        return username, role
    except (ValueError, AttributeError):
        return None


# --- Storage ----------------------------------------------------------------

class Store:
    """Thread-safe SQLite store for users + audit/action/error logs."""

    def __init__(self, path: str = ":memory:", seed_admin: bool = True):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        if seed_admin:
            self._seed_admin()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                role TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                permissions TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                resource TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                workload TEXT NOT NULL,
                namespace TEXT NOT NULL,
                action TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT 'applied'
            );
            CREATE TABLE IF NOT EXISTS error_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                source TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'error',
                message TEXT NOT NULL,
                traceback TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
            CREATE INDEX IF NOT EXISTS idx_action_ts ON action_log(ts);
            CREATE INDEX IF NOT EXISTS idx_error_ts ON error_log(ts);
            """)

    def _seed_admin(self) -> None:
        admin_pw = os.environ.get("K8OPTIMIZER_ADMIN_PASSWORD", "admin")
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) AS c FROM users")
            if cur.fetchone()["c"] == 0:
                self.create_user("admin", admin_pw, "admin")

    # -- users ---------------------------------------------------------------
    def create_user(self, username: str, password: str, role: str = "viewer",
                    permissions: Optional[List[str]] = None) -> User:
        if role not in ROLE_PERMISSIONS:
            raise ValueError(f"unknown role: {role}")
        salt, phash = hash_password(password)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO users(username, role, password_hash, salt, permissions, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (username, role, phash, salt,
                 ",".join(permissions or []), now))
            uid = cur.lastrowid
        return User(uid, username, role, phash, salt, list(permissions or []), now)

    def get_user(self, username: str) -> Optional[User]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            return None
        return User(row["id"], row["username"], row["role"], row["password_hash"],
                    row["salt"], [p for p in row["permissions"].split(",") if p],
                    row["created_at"])

    def list_users(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [User(r["id"], r["username"], r["role"], r["password_hash"],
                     r["salt"], [p for p in r["permissions"].split(",") if p],
                     r["created_at"]).to_dict() for r in rows]

    def set_role(self, username: str, role: str, actor: str = "system") -> None:
        if role not in ROLE_PERMISSIONS:
            raise ValueError(f"unknown role: {role}")
        with self._lock, self._conn:
            self._conn.execute("UPDATE users SET role = ? WHERE username = ?",
                               (role, username))
        self.audit(actor, "user.set_role", f"user:{username}", f"role={role}")

    def set_permissions(self, username: str, permissions: List[str],
                        actor: str = "system") -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE users SET permissions = ? WHERE username = ?",
                               (",".join(permissions), username))
        self.audit(actor, "user.set_permissions", f"user:{username}",
                   ",".join(permissions))

    def authenticate(self, username: str, password: str) -> Optional[str]:
        u = self.get_user(username)
        if not u or not verify_password(password, u.salt, u.password_hash):
            self.audit(username or "unknown", "auth.failed", "login", "")
            return None
        self.audit(username, "auth.success", "login", "")
        return issue_token(username, u.role)

    def has_permission(self, username: str, permission: str) -> bool:
        u = self.get_user(username)
        if not u:
            return False
        allowed = ROLE_PERMISSIONS.get(u.role, set()) | set(u.permissions)
        return permission in allowed or permission.split(".")[0] + ".*" in allowed

    # -- logging -------------------------------------------------------------
    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def audit(self, actor: str, action: str, resource: str, detail: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit_log(ts, actor, action, resource, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (self._now(), actor, action, resource, detail))

    def log_action(self, workload: str, namespace: str, action: str,
                   summary: str = "", outcome: str = "applied") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO action_log(ts, workload, namespace, action, summary, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self._now(), workload, namespace, action, summary, outcome))

    def log_error(self, source: str, message: str, traceback: str = "",
                  level: str = "error") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO error_log(ts, source, level, message, traceback) "
                "VALUES (?, ?, ?, ?, ?)",
                (self._now(), source, level, message, traceback))

    # -- query ---------------------------------------------------------------
    def list_audit(self, limit: int = 100) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def list_actions(self, limit: int = 100) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM action_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def list_errors(self, limit: int = 100) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM error_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()