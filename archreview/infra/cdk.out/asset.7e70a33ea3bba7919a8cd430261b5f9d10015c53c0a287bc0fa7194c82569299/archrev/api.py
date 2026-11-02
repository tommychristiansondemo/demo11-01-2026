"""Public REST API Lambda (behind API Gateway + AWS WAF + Cognito authorizer).

Routes
  GET  /health                       public
  POST /auth/register                public  {username, password(>=6), role: architect|governance}
  POST /auth/login                   public  {username, password} -> Cognito ID token
  GET  /me                           auth
  POST /reviews                      auth    ReviewRequest -> 202 {review_id}
  GET  /reviews                      auth    own reviews; governance may pass ?scope=all
  GET  /reviews/{id}                 auth    owner or governance
"""
from __future__ import annotations

import json
import os
import time
import uuid
from decimal import Decimal
from typing import Any

import boto3
from botocore.config import Config
from pydantic import ValidationError

from . import obs
from .models import MAX_BODY_BYTES, RegisterRequest, ReviewRequest

ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "")
TABLE = os.environ.get("TABLE_NAME", "")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
CLIENT_ID = os.environ.get("USER_POOL_CLIENT_ID", "")
WORKER_FN = os.environ.get("WORKER_FUNCTION_NAME", "")
REVIEWS_PER_HOUR = int(os.environ.get("REVIEWS_PER_USER_PER_HOUR", "10"))
RETENTION_DAYS = int(os.environ.get("REVIEW_RETENTION_DAYS", "7"))

_cfg = Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 3, "mode": "standard"})
_ddb = None
_cognito = None
_lambda = None


def _clients():
    global _ddb, _cognito, _lambda
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", config=_cfg).Table(TABLE)
        _cognito = boto3.client("cognito-idp", config=_cfg)
        _lambda = boto3.client("lambda", config=_cfg)
    return _ddb, _cognito, _lambda


def _resp(status: int, body: Any, cid: str) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
            "Access-Control-Expose-Headers": "X-Correlation-Id",
            "Vary": "Origin",
            "X-Correlation-Id": cid,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body, default=_json_default),
    }


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return int(o) if o == int(o) else float(o)
    raise TypeError(type(o).__name__)


def _err(status: int, code: str, message: str, cid: str, details: Any = None) -> dict[str, Any]:
    body = {"error": {"code": code, "message": message, "correlation_id": cid}}
    if details:
        body["error"]["details"] = details
    return _resp(status, body, cid)


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        raise _HttpError(413, "payload_too_large", f"Request body must be at most {MAX_BODY_BYTES} bytes")
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        raise _HttpError(400, "invalid_json", "Body must be valid JSON")
    if not isinstance(data, dict):
        raise _HttpError(400, "invalid_json", "Body must be a JSON object")
    return data


class _HttpError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def _claims(event: dict[str, Any]) -> tuple[str, str]:
    claims = (event.get("requestContext", {}).get("authorizer") or {}).get("claims") or {}
    user = claims.get("cognito:username")
    if not user:
        raise _HttpError(401, "unauthorized", "Sign in required")
    groups = claims.get("cognito:groups") or ""
    if isinstance(groups, list):
        groups = ",".join(groups)
    role = "governance" if "governance" in groups else "architect"
    return user, role


def _validation_details(ve: ValidationError) -> list[dict[str, str]]:
    return [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in ve.errors()[:20]]


# ---------------- route handlers ----------------

def register(event, cid):
    try:
        req = RegisterRequest.model_validate(_body(event))
    except ValidationError as ve:
        raise _HttpError(400, "validation_error", "Invalid registration", _validation_details(ve))
    _, cognito, _ = _clients()
    username = req.username.lower()
    try:
        cognito.admin_create_user(UserPoolId=USER_POOL_ID, Username=username, MessageAction="SUPPRESS")
    except cognito.exceptions.UsernameExistsException:
        raise _HttpError(409, "username_taken", "That user ID is already registered. Choose another or sign in.")
    except cognito.exceptions.InvalidPasswordException:
        raise _HttpError(400, "invalid_password", "Password must be at least 6 characters")
    try:
        cognito.admin_set_user_password(UserPoolId=USER_POOL_ID, Username=username, Password=req.password, Permanent=True)
        cognito.admin_add_user_to_group(UserPoolId=USER_POOL_ID, Username=username, GroupName=req.role)
    except cognito.exceptions.InvalidPasswordException:
        cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=username)
        raise _HttpError(400, "invalid_password", "Password must be at least 6 characters")
    obs.log("user_registered", correlation_id=cid, role=req.role)
    obs.metric("Registrations", 1, Role=req.role)
    return _resp(201, {"username": username, "role": req.role}, cid)


def login(event, cid):
    data = _body(event)
    username = str(data.get("username", "")).strip().lower()[:32]
    password = str(data.get("password", ""))[:64]
    if not username or not password:
        raise _HttpError(400, "validation_error", "username and password are required")
    _, cognito, _ = _clients()
    try:
        res = cognito.initiate_auth(ClientId=CLIENT_ID, AuthFlow="USER_PASSWORD_AUTH",
                                    AuthParameters={"USERNAME": username, "PASSWORD": password})
    except (cognito.exceptions.NotAuthorizedException, cognito.exceptions.UserNotFoundException):
        raise _HttpError(401, "invalid_credentials", "Incorrect user ID or password")
    auth = res.get("AuthenticationResult") or {}
    groups = cognito.admin_list_groups_for_user(UserPoolId=USER_POOL_ID, Username=username).get("Groups", [])
    role = "governance" if any(g["GroupName"] == "governance" for g in groups) else "architect"
    return _resp(200, {"id_token": auth.get("IdToken"), "expires_in": auth.get("ExpiresIn"), "username": username,
                       "role": role}, cid)


def create_review(event, cid):
    user, role = _claims(event)
    try:
        req = ReviewRequest.model_validate(_body(event))
    except ValidationError as ve:
        raise _HttpError(400, "validation_error", "Please correct the highlighted fields", _validation_details(ve))
    table, _, lam = _clients()
    now = int(time.time())
    from boto3.dynamodb.conditions import Key
    recent = table.query(IndexName="owner-index", KeyConditionExpression=Key("owner").eq(user) & Key("created_at").gt(now - 3600),
                         Select="COUNT")
    if recent.get("Count", 0) >= REVIEWS_PER_HOUR:
        raise _HttpError(429, "rate_limited", f"Limit of {REVIEWS_PER_HOUR} reviews per hour reached. Try again later.")
    review_id = f"rv-{uuid.uuid4().hex[:20]}"
    item = {"review_id": review_id, "owner": user, "owner_role": role, "entity": "REVIEW", "created_at": now,
            "status": "queued", "correlation_id": cid, "title": req.title,
            "request": json.dumps(req.model_dump()), "ttl": now + RETENTION_DAYS * 86400}
    table.put_item(Item=item)
    lam.invoke(FunctionName=WORKER_FN, InvocationType="Event",
               Payload=json.dumps({"review_id": review_id, "correlation_id": cid}).encode())
    obs.log("review_queued", correlation_id=cid, review_id=review_id, role=role, classification=req.data_classification)
    obs.metric("ReviewsSubmitted", 1)
    return _resp(202, {"review_id": review_id, "status": "queued", "correlation_id": cid}, cid)


def _public_item(item: dict[str, Any], include_result: bool) -> dict[str, Any]:
    out = {k: item.get(k) for k in ("review_id", "owner", "created_at", "status", "correlation_id", "title", "error",
                                    "completed_at")}
    if include_result:
        out["request"] = json.loads(item["request"]) if item.get("request") else None
        out["result"] = json.loads(item["result"]) if item.get("result") else None
    return out


def get_review(event, cid):
    user, role = _claims(event)
    rid = (event.get("pathParameters") or {}).get("id", "")
    if not rid.startswith("rv-") or len(rid) > 40:
        raise _HttpError(400, "validation_error", "Invalid review id")
    table, _, _ = _clients()
    item = table.get_item(Key={"review_id": rid}).get("Item")
    if not item or (item["owner"] != user and role != "governance"):
        raise _HttpError(404, "not_found", "Review not found")
    return _resp(200, _public_item(item, True), cid)


def list_reviews(event, cid):
    user, role = _claims(event)
    scope = ((event.get("queryStringParameters") or {}).get("scope") or "mine")
    table, _, _ = _clients()
    from boto3.dynamodb.conditions import Key
    if scope == "all":
        if role != "governance":
            raise _HttpError(403, "forbidden", "Only governance reviewers can view all reviews")
        res = table.query(IndexName="all-index", KeyConditionExpression=Key("entity").eq("REVIEW"),
                          ScanIndexForward=False, Limit=50)
    else:
        res = table.query(IndexName="owner-index", KeyConditionExpression=Key("owner").eq(user),
                          ScanIndexForward=False, Limit=25)
    return _resp(200, {"scope": scope, "items": [_public_item(i, False) for i in res.get("Items", [])]}, cid)


def me(event, cid):
    user, role = _claims(event)
    return _resp(200, {"username": user, "role": role}, cid)


ROUTES = {
    ("GET", "/health"): lambda e, c: _resp(200, {"ok": True, "service": "archrev-api"}, c),
    ("POST", "/auth/register"): register,
    ("POST", "/auth/login"): login,
    ("GET", "/me"): me,
    ("POST", "/reviews"): create_review,
    ("GET", "/reviews"): list_reviews,
    ("GET", "/reviews/{id}"): get_review,
}


def handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    cid = obs.new_correlation_id(headers.get("x-correlation-id"))
    method, resource = event.get("httpMethod", ""), event.get("resource", "")
    start = time.perf_counter()
    status = 500
    try:
        fn = ROUTES.get((method, resource))
        if not fn:
            raise _HttpError(404, "not_found", "Route not found")
        resp = fn(event, cid)
    except _HttpError as he:
        resp = _err(he.status, he.code, he.message, cid, he.details)
    except Exception as exc:  # never leak internals
        obs.log("unhandled_error", level="ERROR", correlation_id=cid, route=resource, error=type(exc).__name__,
                detail=str(exc)[:300])
        resp = _err(500, "internal_error", "Something went wrong. Quote the correlation ID if you report it.", cid)
    status = resp["statusCode"]
    obs.log("api_request", correlation_id=cid, method=method, route=resource, status=status,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
            request_id=getattr(context, "aws_request_id", None))
    return resp
