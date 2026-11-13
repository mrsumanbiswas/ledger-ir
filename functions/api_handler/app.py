"""HTTP API handler: demo seeder, execution status, dashboard reads, presigns.

Thin I/O shell; no business logic. Single-tenant demo deployment: tenant and
period strings are validated against strict patterns before ever reaching an
S3 key or DynamoDB key (no auth by design for the challenge demo).
"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key

_s3 = boto3.client("s3")
_sfn = boto3.client("stepfunctions")
_table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
_DOCS_BUCKET = os.environ["DOCS_BUCKET"]
_DOSSIER_BUCKET = os.environ["DOSSIER_BUCKET"]
_STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]

_SEED_DIR = Path(__file__).parent / "seed_data"
_SEED_TENANT, _SEED_PERIOD = "demo", "2026-10"

_TENANT_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")
_ARN_RE = re.compile(
    r"^arn:aws:states:[a-z0-9-]+:\d{12}:execution:[A-Za-z0-9._-]+:[A-Za-z0-9._-]+$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _response(status: int, body: dict) -> dict:
    return {"statusCode": status,
            "headers": {"content-type": "application/json",
                        "access-control-allow-origin": "*"},
            "body": json.dumps(body, default=str)}


def _bad_request(message: str) -> dict:
    return _response(400, {"error": message})


def _tenant_period(params: dict) -> tuple[str, str]:
    tenant = params.get("tenant_id", _SEED_TENANT)
    period = params.get("period", _SEED_PERIOD)
    if not _TENANT_RE.match(tenant) or not _PERIOD_RE.match(period):
        raise ValueError("invalid tenant_id or period")
    return tenant, period


def _demo_seed(_params: dict, _body: dict) -> dict:
    prefix = f"uploads/{_SEED_TENANT}/{_SEED_PERIOD}"
    keys = {}
    for name in ("invoice_882_tokens.json", "invoice_882_flawed.png",
                 "gstr2b_oct2026.json", "bank_oct2026.csv"):
        key = f"{prefix}/{name}"
        _s3.put_object(Bucket=_DOCS_BUCKET, Key=key,
                       Body=(_SEED_DIR / name).read_bytes())
        keys[name] = key

    execution_input = {
        "tenant_id": _SEED_TENANT,
        "period": _SEED_PERIOD,
        "invoice_keys": [keys["invoice_882_tokens.json"]],
        "gstr2b_key": keys["gstr2b_oct2026.json"],
        "bank_key": keys["bank_oct2026.csv"],
    }
    execution = _sfn.start_execution(
        stateMachineArn=_STATE_MACHINE_ARN,
        name=f"{_SEED_TENANT}-{_SEED_PERIOD}-{uuid.uuid4().hex[:8]}",
        input=json.dumps(execution_input))
    return _response(200, {
        "execution_arn": execution["executionArn"],
        "tenant_id": _SEED_TENANT,
        "period": _SEED_PERIOD,
        "invoice_image_key": keys["invoice_882_flawed.png"],
    })


def _status(params: dict, _body: dict) -> dict:
    arn = params.get("execution_arn", "")
    if not _ARN_RE.match(arn):
        return _bad_request("invalid execution_arn")
    desc = _sfn.describe_execution(executionArn=arn)
    out = {"status": desc["status"], "start_date": desc["startDate"]}
    if "stopDate" in desc:
        out["stop_date"] = desc["stopDate"]
    if desc.get("output"):
        out["output"] = json.loads(desc["output"])
    return _response(200, out)


def _dashboard(params: dict, _body: dict) -> dict:
    try:
        tenant, period = _tenant_period(params)
    except ValueError as exc:
        return _bad_request(str(exc))
    items, kwargs = [], {
        "KeyConditionExpression": Key("PK").eq(f"TENANT#{tenant}#{period}")}
    while True:
        page = _table.query(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    grouped: dict[str, list] = {"invoices": [], "gstr2b": [], "bank": [],
                                "edges": []}
    summary = None
    bucket_for = {"INVOICE_AST": "invoices", "GSTR2B": "gstr2b",
                  "BANK": "bank", "MATCH_EDGE": "edges"}
    for item in items:
        payload = json.loads(item["payload"])
        if item["entity"] == "SUMMARY":
            summary = payload
            summary["merkle_root"] = item.get("merkle_root")
            summary["dossier_key"] = item.get("dossier_key")
        elif item["entity"] in bucket_for:
            grouped[bucket_for[item["entity"]]].append(payload)
    return _response(200, {"tenant_id": tenant, "period": period,
                           **grouped, "summary": summary})


def _presign(_params: dict, body: dict) -> dict:
    filename = body.get("filename", "")
    if not _FILENAME_RE.match(filename):
        return _bad_request("invalid filename")
    try:
        tenant, period = _tenant_period(body)
    except ValueError as exc:
        return _bad_request(str(exc))
    key = f"uploads/{tenant}/{period}/{uuid.uuid4().hex[:8]}-{filename}"
    url = _s3.generate_presigned_url(
        "put_object", Params={"Bucket": _DOCS_BUCKET, "Key": key},
        ExpiresIn=900)
    return _response(200, {"upload_url": url, "key": key})


def _file(params: dict, _body: dict) -> dict:
    """Presigned GET for an uploaded document (e.g. the invoice image the
    dashboard displays). Restricted to the validated uploads/ prefix."""
    try:
        tenant, period = _tenant_period(params)
    except ValueError as exc:
        return _bad_request(str(exc))
    name = params.get("name", "")
    if not _FILENAME_RE.match(name):
        return _bad_request("invalid name")
    key = f"uploads/{tenant}/{period}/{name}"
    url = _s3.generate_presigned_url(
        "get_object", Params={"Bucket": _DOCS_BUCKET, "Key": key},
        ExpiresIn=900)
    return _response(200, {"url": url, "key": key})


def _dossier(params: dict, _body: dict) -> dict:
    try:
        tenant, period = _tenant_period(params)
    except ValueError as exc:
        return _bad_request(str(exc))
    item = _table.get_item(Key={"PK": f"TENANT#{tenant}#{period}",
                                "SK": "SUMMARY#MONTHLY"}).get("Item")
    if not item or "dossier_key" not in item:
        return _response(404, {"error": "no dossier yet for this period"})
    url = _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": _DOSSIER_BUCKET, "Key": item["dossier_key"]},
        ExpiresIn=900)
    return _response(200, {"dossier_url": url,
                           "merkle_root": item.get("merkle_root")})


_ROUTES = {
    ("POST", "/demo-seed"): _demo_seed,
    ("GET", "/status"): _status,
    ("GET", "/dashboard"): _dashboard,
    ("POST", "/presign"): _presign,
    ("GET", "/file"): _file,
    ("GET", "/dossier"): _dossier,
}


def lambda_handler(event, _context):
    http = event["requestContext"]["http"]
    route = (http["method"], event["rawPath"])
    handler = _ROUTES.get(route)
    if handler is None:
        return _response(404, {"error": f"no route {route[0]} {route[1]}"})
    params = event.get("queryStringParameters") or {}
    try:
        body = json.loads(event["body"]) if event.get("body") else {}
    except ValueError:
        return _bad_request("request body is not JSON")
    try:
        return handler(params, body)
    except Exception as exc:  # surface a clean JSON 500, log the rest
        print(f"ERROR {route}: {exc!r}")
        return _response(500, {"error": "internal error"})
