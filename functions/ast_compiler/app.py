"""Lambda handler: S3 tokens JSON -> compile -> DynamoDB Invoice AST item.

Thin I/O shell only; all logic lives in core/ and is unit-tested offline.
Returns a tiny summary (claim-check pattern): the AST itself goes to DynamoDB,
never through Step Functions state.
"""
from __future__ import annotations

import json
import os

import boto3

from core.compiler import compile_invoice

_s3 = boto3.client("s3")
_table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
_BUCKET = os.environ["DOCS_BUCKET"]


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def lambda_handler(event, _context):
    tenant_id = event["tenant_id"]
    period = event["period"]
    tokens_key = event["tokens_key"]

    body = _s3.get_object(Bucket=_BUCKET, Key=tokens_key)["Body"].read()
    ast = compile_invoice(json.loads(body))

    _table.put_item(Item={
        "PK": f"TENANT#{tenant_id}#{period}",
        "SK": f"DOC#INV#{ast['inv_no']}",
        "entity": "INVOICE_AST",
        "status": ast["status"],
        "payload": _canonical(ast),
    })
    return {
        "inv_no": ast["inv_no"],
        "status": ast["status"],
        "repair_count": len(ast["repairs"]),
        "error_count": len(ast["errors"]),
    }
