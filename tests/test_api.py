import json

import pytest

from k8optimizer.api import Api, ApiError
from k8optimizer.store import Store


@pytest.fixture
def api():
    s = Store(seed_admin=True)
    return Api(s)


@pytest.fixture
def auth_headers(api):
    token = api.store.authenticate("admin", "admin")
    return {"Authorization": f"Bearer {token}"}


def test_login_issues_token(api):
    out = api.login({"username": "admin", "password": "admin"})
    assert out["token"]
    assert out["user"]["role"] == "admin"


def test_login_wrong_password_rejected(api):
    with pytest.raises(ApiError) as e:
        api.login({"username": "admin", "password": "nope"})
    assert e.value.status == 401


def test_me_requires_auth(api):
    with pytest.raises(ApiError) as e:
        api.me({})
    assert e.value.status == 401


def test_me_returns_profile_and_permissions(api, auth_headers):
    me = api.me(auth_headers)
    assert me["username"] == "admin"
    assert me["role"] == "admin"
    assert "users.manage" in me["permissions"]


def test_metrics_permission_enforced(api):
    with pytest.raises(ApiError) as e:
        api.metrics({})
    assert e.value.status == 401


def test_metrics_counts_actions(api, auth_headers):
    s = api.store
    s.log_action("wg", "ns", "patch", "dv", "applied")
    s.log_action("wg", "ns", "patch", "dv", "blocked")
    s.log_action("wg", "ns", "patch", "dv", "applied")
    s.log_error("c", "oom kill", "", "error")
    m = api.metrics(auth_headers)
    assert m["summary"]["actions_total"] == 3
    assert m["summary"]["applied"] == 2
    assert m["summary"]["blocked_or_vetoed"] == 1
    assert m["summary"]["errors_total"] == 1
    assert 0 < m["summary"]["success_rate"] < 1


def test_logs_and_rca(api, auth_headers):
    s = api.store
    s.log_error("c", "recurring oom kill", "", "error")
    s.log_error("c", "recurring oom kill", "", "error")
    l = api.logs(auth_headers, "errors", 10)
    assert l["kind"] == "errors"
    assert len(l["rows"]) == 2
    r = api.rca(auth_headers)
    assert r["count"] >= 1
    assert any(c["category"] == "recurring-error" for c in r["causes"])


def test_providers_list(api, auth_headers):
    p = api.providers(auth_headers)
    ids = {x["id"] for x in p["providers"]}
    assert ids == {"aws", "gcp", "azure", "oci", "onprem"}


def test_user_management_rbac(api, auth_headers):
    u = api.create_user(auth_headers, {"username": "bob", "password": "pw",
                                       "role": "viewer"})
    assert u["user"]["role"] == "viewer"
    assert len(api.list_users(auth_headers)["users"]) == 2
    api.set_role(auth_headers, "bob", "optimizer")
    me = api.store.get_user("bob")
    assert me.role == "optimizer"
    api.set_permissions(auth_headers, "bob", ["users.manage"])
    assert api.store.has_permission("bob", "users.manage")


def test_viewer_cannot_manage_users(api, auth_headers):
    api.create_user(auth_headers, {"username": "alice", "password": "pw",
                                   "role": "viewer"})
    token = api.store.authenticate("alice", "pw")
    hdr = {"Authorization": f"Bearer {token}"}
    with pytest.raises(ApiError) as e:
        api.list_users(hdr)
    assert e.value.status == 403