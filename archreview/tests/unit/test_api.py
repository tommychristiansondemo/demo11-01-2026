"""API handler tests with stubbed AWS clients."""
import json

import pytest

from archrev import api


class _Ex:
    class UsernameExistsException(Exception): ...
    class InvalidPasswordException(Exception): ...
    class NotAuthorizedException(Exception): ...
    class UserNotFoundException(Exception): ...


class FakeCognito:
    exceptions = _Ex

    def __init__(self):
        self.users, self.groups = {}, {}

    def admin_create_user(self, UserPoolId, Username, MessageAction):
        if Username in self.users:
            raise _Ex.UsernameExistsException()
        self.users[Username] = None

    def admin_set_user_password(self, UserPoolId, Username, Password, Permanent):
        self.users[Username] = Password

    def admin_add_user_to_group(self, UserPoolId, Username, GroupName):
        self.groups[Username] = GroupName

    def admin_delete_user(self, **kw): ...


@pytest.fixture
def cognito(monkeypatch):
    c = FakeCognito()
    monkeypatch.setattr(api, "_clients", lambda: (None, c, None))
    monkeypatch.setattr(api, "ALLOWED_ORIGIN", "https://archreview.awsteach.com")
    return c


def ev(method, resource, body=None, claims=None, headers=None):
    e = {"httpMethod": method, "resource": resource, "headers": headers or {}, "body": json.dumps(body) if body else None}
    if claims:
        e["requestContext"] = {"authorizer": {"claims": claims}}
    return e


def test_register_six_char_password_and_role(cognito):
    r = api.handler(ev("POST", "/auth/register", {"username": "Student1", "password": "abcdef", "role": "governance"}), None)
    assert r["statusCode"] == 201
    assert cognito.groups["student1"] == "governance"
    assert r["headers"]["Access-Control-Allow-Origin"] == "https://archreview.awsteach.com"


def test_register_duplicate_and_validation(cognito):
    body = {"username": "dup", "password": "123456", "role": "architect"}
    api.handler(ev("POST", "/auth/register", body), None)
    assert api.handler(ev("POST", "/auth/register", body), None)["statusCode"] == 409
    short = api.handler(ev("POST", "/auth/register", {**body, "username": "x2", "password": "12345"}), None)
    assert short["statusCode"] == 400
    badrole = api.handler(ev("POST", "/auth/register", {**body, "username": "x3", "role": "admin"}), None)
    assert badrole["statusCode"] == 400


def test_body_size_limit(cognito):
    r = api.handler(ev("POST", "/auth/register", {"username": "a" * 9000}), None)
    assert r["statusCode"] == 413


def test_auth_required_and_unknown_route(cognito):
    assert api.handler(ev("GET", "/me"), None)["statusCode"] == 401
    assert api.handler(ev("GET", "/nope"), None)["statusCode"] == 404


def test_correlation_id_propagated(cognito):
    r = api.handler(ev("GET", "/health", headers={"X-Correlation-Id": "abc12345-test"}), None)
    assert r["headers"]["X-Correlation-Id"] == "abc12345-test"
    r2 = api.handler(ev("GET", "/health", headers={"X-Correlation-Id": "bad id; drop"}), None)
    assert r2["headers"]["X-Correlation-Id"].startswith("cr-")


def test_internal_errors_do_not_leak(monkeypatch, cognito):
    def boom(e, c):
        raise RuntimeError("secret internal detail")
    monkeypatch.setitem(api.ROUTES, ("GET", "/health"), boom)
    r = api.handler(ev("GET", "/health"), None)
    assert r["statusCode"] == 500 and "secret" not in r["body"]


def test_logs_do_not_contain_proposal_text(capsys):
    from archrev import obs
    obs.log("x", description="TOP SECRET PROPOSAL", nested={"title": "hidden"})
    out = capsys.readouterr().out
    assert "TOP SECRET" not in out and "hidden" not in out and "redacted" in out
