"""Enterprise REST API — auth + RBAC + metrics + logs + RCA, on top of the store.

Endpoints (all JSON):

    POST /api/login            {username, password}      -> {token}
    GET  /api/me                                         -> current user profile
    GET  /api/metrics                                    -> dashboard metrics
    GET  /api/logs/audit|actions|errors    (logs.read)   -> log rows
    POST /api/logs/errors                (logs.write)    -> record an error
    GET  /api/rca                                        -> root-cause report
    GET  /api/providers                                  -> multi-cloud registry
    GET  /api/users                        (users.manage) -> user list
    POST /api/users                        (users.manage) -> create user
    PUT  /api/users/<username>/role                    -> set role
    PUT  /api/users/<username>/permissions             -> set per-user permissions

Every protected endpoint checks the caller's role-derived permission set and
writes an audit entry on mutation. Auth is an HMAC-signed token from ``store``.
"""

from __future__ import annotations

import json
from typing import Callable, Dict, Optional

from .providers import list_providers
from .rca import analyze_report
from .store import ROLE_PERMISSIONS, ROLES, Store


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Api:
    """Permission-checked JSON API bound to a ``Store``."""

    def __init__(self, store: Store):
        self.store = store

    # -- auth helpers -----------------------------------------------------
    def _token_from(self, headers: Dict[str, str]) -> Optional[str]:
        auth = headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return None

    def _require(self, headers: Dict[str, str], permission: str) -> str:
        """Return the authenticated username, or raise ApiError."""
        from .store import verify_token
        token = self._token_from(headers)
        who = verify_token(token) if token else None
        if not who:
            raise ApiError(401, "authentication required")
        username, _role = who
        if not self.store.has_permission(username, permission):
            raise ApiError(403, f"missing permission: {permission}")
        return username

    # -- login ------------------------------------------------------------
    def login(self, body: dict) -> dict:
        username = body.get("username", "")
        password = body.get("password", "")
        token = self.store.authenticate(username, password)
        if not token:
            raise ApiError(401, "invalid credentials")
        user = self.store.get_user(username)
        return {"token": token, "user": user.to_dict()}

    # -- me ---------------------------------------------------------------
    def me(self, headers: Dict[str, str]) -> dict:
        username = self._require(headers, "workload.read")
        user = self.store.get_user(username)
        now_perms = sorted(ROLE_PERMISSIONS.get(user.role, set()) | set(user.permissions))
        return {"username": username, "role": user.role,
                "permissions": now_perms, "roles": ROLES}

    # -- metrics ----------------------------------------------------------
    def metrics(self, headers: Dict[str, str]) -> dict:
        self._require(headers, "metrics.read")
        s = self.store
        actions = s.list_actions(10000)
        errors = s.list_errors(10000)
        audit = s.list_audit(10000)

        by_outcome: Dict[str, int] = {}
        by_action: Dict[str, int] = {}
        namespaces = set()
        for a in actions:
            by_outcome[a["outcome"]] = by_outcome.get(a["outcome"], 0) + 1
            by_action[a["action"]] = by_action.get(a["action"], 0) + 1
            namespaces.add(a["namespace"])

        by_level: Dict[str, int] = {}
        for e in errors:
            by_level[e["level"]] = by_level.get(e["level"], 0) + 1

        by_actor: Dict[str, int] = {}
        for au in audit:
            by_actor[au["actor"]] = by_actor.get(au["actor"], 0) + 1

        applied = by_outcome.get("applied", 0) + by_outcome.get("blocked", 0) \
            + by_outcome.get("vetoed", 0) + by_outcome.get("failed", 0)
        success_rate = (by_outcome.get("applied", 0) / applied) if applied else 1.0

        return {
            "summary": {
                "actions_total": len(actions),
                "applied": by_outcome.get("applied", 0),
                "blocked_or_vetoed": by_outcome.get("blocked", 0) + by_outcome.get("vetoed", 0),
                "failed": by_outcome.get("failed", 0),
                "errors_total": len(errors),
                "audit_events": len(audit),
                "namespaces": len(namespaces),
                "success_rate": round(success_rate, 3),
            },
            "actions_by_outcome": by_outcome,
            "actions_by_type": by_action,
            "errors_by_level": by_level,
            "audit_by_actor": by_actor,
        }

    # -- logs -------------------------------------------------------------
    def logs(self, headers: Dict[str, str], kind: str, limit: int) -> dict:
        self._require(headers, "logs.read")
        s = self.store
        rows = {"audit": s.list_audit, "actions": s.list_actions,
                "errors": s.list_errors}[kind](limit if limit else 100)
        return {"kind": kind, "rows": rows}

    def log_error(self, headers: Dict[str, str], body: dict) -> dict:
        self._require(headers, "logs.write")
        self.store.log_error(body.get("source", "api"), body.get("message", ""),
                             body.get("traceback", ""), body.get("level", "error"))
        return {"recorded": True}

    # -- rca --------------------------------------------------------------
    def rca(self, headers: Dict[str, str]) -> dict:
        self._require(headers, "rca.read")
        return analyze_report(self.store)

    # -- providers --------------------------------------------------------
    def providers(self, headers: Dict[str, str]) -> dict:
        self._require(headers, "metrics.read")
        return {"providers": list_providers()}

    # -- users ------------------------------------------------------------
    def list_users(self, headers: Dict[str, str]) -> dict:
        self._require(headers, "users.manage")
        return {"users": self.store.list_users()}

    def create_user(self, headers: Dict[str, str], body: dict) -> dict:
        actor = self._require(headers, "users.manage")
        username = body.get("username", "")
        if not username:
            raise ApiError(400, "username required")
        role = body.get("role", "viewer")
        u = self.store.create_user(username, body.get("password", ""), role,
                                   body.get("permissions"))
        self.store.audit(actor, "user.create", f"user:{username}", f"role={role}")
        return {"user": u.to_dict()}

    def set_role(self, headers: Dict[str, str], username: str, role: str) -> dict:
        actor = self._require(headers, "users.manage")
        self.store.set_role(username, role, actor=actor)
        return {"username": username, "role": role}

    def set_permissions(self, headers: Dict[str, str], username: str, perms: list) -> dict:
        actor = self._require(headers, "users.manage")
        self.store.set_permissions(username, perms, actor=actor)
        return {"username": username, "permissions": perms}


def build_api(store: Store) -> Api:
    return Api(store)