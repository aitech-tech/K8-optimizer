"""Tests for the enterprise store: RBAC, auth, and append-only logging."""

import pytest

from k8optimizer.store import (
    ROLE_PERMISSIONS,
    Store,
    hash_password,
    issue_token,
    verify_password,
    verify_token,
)


@pytest.fixture()
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_password_hashing_roundtrips():
    salt, phash = hash_password("hunter2")
    assert verify_password("hunter2", salt, phash)
    assert not verify_password("wrong", salt, phash)


def test_seed_admin_created():
    s = Store(":memory:")
    assert s.get_user("admin") is not None
    assert s.get_user("admin").role == "admin"


def test_authenticate_returns_valid_token():
    s = Store(":memory:")
    tok = s.authenticate("admin", "admin")
    assert tok is not None
    assert verify_token(tok) == ("admin", "admin")


def test_authenticate_rejects_bad_password():
    s = Store(":memory:")
    assert s.authenticate("admin", "nope") is None


def test_role_permissions_are_inherited():
    s = Store(":memory:")
    s.create_user("bob", "pw", "viewer")
    assert s.has_permission("bob", "workload.read") is True
    assert s.has_permission("bob", "cluster.apply") is False


def test_permission_override_grants_access():
    s = Store(":memory:")
    s.create_user("carol", "pw", "viewer", permissions=["cluster.apply"])
    assert s.has_permission("carol", "cluster.apply") is True


def test_unknown_user_has_no_permission():
    s = Store(":memory:")
    assert s.has_permission("ghost", "workload.read") is False


def test_set_role_is_audited():
    s = Store(":memory:")
    s.create_user("dave", "pw", "viewer")
    s.set_role("dave", "optimizer", actor="admin")
    assert s.get_user("dave").role == "optimizer"
    assert any(e["action"] == "user.set_role" for e in s.list_audit())


def test_audit_log_is_append_only_recording_actor():
    s = Store(":memory:")
    s.audit("alice", "cluster.apply", "workload:prod/svc", "resized")
    rows = s.list_audit()
    assert rows[0]["actor"] == "alice"
    assert rows[0]["resource"] == "workload:prod/svc"


def test_action_and_error_logs_capture():
    s = Store(":memory:")
    s.log_action("svc", "prod", "create VPA", "VPA -> prod/svc", "applied")
    s.log_error("controller", "boom", "Traceback...")
    assert s.list_actions()[0]["workload"] == "svc"
    assert s.list_errors()[0]["message"] == "boom"


def test_tampered_token_rejected():
    tok = issue_token("admin", "admin")
    forged = tok[:-4] + "AAAA"
    assert verify_token(forged) is None


def test_every_role_has_a_permission_set():
    for role in ("admin", "optimizer", "viewer", "auditor"):
        assert isinstance(ROLE_PERMISSIONS[role], set)
        assert ROLE_PERMISSIONS[role]