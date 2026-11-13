"""Lambda handler for the graph_reconciler stages, dispatched on ``action``:

  load_gstr2b  S3 JSON  -> DOC#GSTR2B#<inv_no> items
  load_bank    S3 CSV   -> DOC#BANK#<txn_id> items
  reconcile    DynamoDB partition -> MATCH#<edge_id> + SUMMARY#MONTHLY items
  merkle       DynamoDB partition -> SHA-256 root + Object-Locked S3 dossier

Thin I/O shell only; all logic lives in core/ and is unit-tested offline.
Every record is stored as its canonical JSON string in the ``payload``
attribute, so hashes computed later are byte-identical to what was written
(no float/Decimal round-trips through DynamoDB types).
"""
from __future__ import annotations

import hashlib
import json
import os

import boto3
from boto3.dynamodb.conditions import Key

from core.ims import triage
from core.merkle import canonical_json, merkle_root
from core.reconciler import parse_bank_csv, reconcile

_s3 = boto3.client("s3")
_table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
_DOCS_BUCKET = os.environ["DOCS_BUCKET"]
_DOSSIER_BUCKET = os.environ["DOSSIER_BUCKET"]


def _canonical_str(obj) -> str:
    return canonical_json(obj).decode("utf-8")


def _pk(tenant_id: str, period: str) -> str:
    return f"TENANT#{tenant_id}#{period}"


def _query_partition(pk: str) -> list[dict]:
    items, kwargs = [], {"KeyConditionExpression": Key("PK").eq(pk)}
    while True:
        page = _table.query(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _s3_read(key: str) -> bytes:
    return _s3.get_object(Bucket=_DOCS_BUCKET, Key=key)["Body"].read()


def _load_gstr2b(tenant_id: str, period: str, key: str) -> dict:
    doc = json.loads(_s3_read(key))
    with _table.batch_writer() as batch:
        for record in doc["records"]:
            batch.put_item(Item={
                "PK": _pk(tenant_id, period),
                "SK": f"DOC#GSTR2B#{record['inv_no']}",
                "entity": "GSTR2B",
                "payload": _canonical_str(record),
            })
    return {"loaded": len(doc["records"])}


def _load_bank(tenant_id: str, period: str, key: str) -> dict:
    entries = parse_bank_csv(_s3_read(key).decode("utf-8"))
    with _table.batch_writer() as batch:
        for entry in entries:
            batch.put_item(Item={
                "PK": _pk(tenant_id, period),
                "SK": f"DOC#BANK#{entry['txn_id']}",
                "entity": "BANK",
                "payload": _canonical_str(entry),
            })
    return {"loaded": len(entries)}


def _payloads(items: list[dict], entity: str) -> list[dict]:
    return [json.loads(i["payload"]) for i in items if i["entity"] == entity]


def _reconcile(tenant_id: str, period: str) -> dict:
    pk = _pk(tenant_id, period)
    items = _query_partition(pk)
    records = _payloads(items, "GSTR2B")
    bank = _payloads(items, "BANK")
    asts = {a["inv_no"]: a for a in _payloads(items, "INVOICE_AST")}

    edges = reconcile(records, bank)

    seen: set[tuple[str, str]] = set()
    duplicates: set[str] = set()
    for rec in records:
        ident = (rec["supplier_gstin"], rec["inv_no"])
        if ident in seen:
            duplicates.add(rec["inv_no"])
        seen.add(ident)

    ims_results = {
        rec["inv_no"]: triage(asts.get(rec["inv_no"]), rec, edges,
                              duplicate=rec["inv_no"] in duplicates)
        for rec in records
    }
    summary = {
        "period": period,
        "ims": ims_results,
        "total_holdback_paise": sum(r["holdback_paise"]
                                    for r in ims_results.values()),
    }

    with _table.batch_writer() as batch:
        for edge in edges:
            batch.put_item(Item={
                "PK": pk, "SK": f"MATCH#{edge['edge_id']}",
                "entity": "MATCH_EDGE", "payload": _canonical_str(edge),
            })
        batch.put_item(Item={
            "PK": pk, "SK": "SUMMARY#MONTHLY",
            "entity": "SUMMARY", "payload": _canonical_str(summary),
        })
    actions = [r["action"] for r in ims_results.values()]
    return {
        "edges": len(edges),
        "ims_counts": {a: actions.count(a) for a in sorted(set(actions))},
        "total_holdback_paise": summary["total_holdback_paise"],
    }


def _merkle(tenant_id: str, period: str) -> dict:
    pk = _pk(tenant_id, period)
    items = _query_partition(pk)
    records = [(item["SK"], json.loads(item["payload"])) for item in items]
    root = merkle_root(records)

    dossier = {
        "tenant_id": tenant_id,
        "period": period,
        "merkle_root": root,
        "leaves": [
            {"sk": sk,
             "leaf_sha256": hashlib.sha256(canonical_json(rec)).hexdigest()}
            for sk, rec in sorted(records, key=lambda kv: kv[0])
        ],
    }
    # key contains the root: same input -> same key -> idempotent re-runs
    dossier_key = f"dossiers/{tenant_id}/{period}/{root}.json"
    _s3.put_object(Bucket=_DOSSIER_BUCKET, Key=dossier_key,
                   Body=_canonical_str(dossier).encode("utf-8"),
                   ContentType="application/json")
    _table.update_item(
        Key={"PK": pk, "SK": "SUMMARY#MONTHLY"},
        UpdateExpression="SET merkle_root = :r, dossier_key = :k",
        ExpressionAttributeValues={":r": root, ":k": dossier_key})
    return {"merkle_root": root, "dossier_key": dossier_key,
            "leaf_count": len(records)}


_ACTIONS = {"load_gstr2b": _load_gstr2b, "load_bank": _load_bank,
            "reconcile": _reconcile, "merkle": _merkle}


def lambda_handler(event, _context):
    action = event["action"]
    if action not in _ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    args = [event["tenant_id"], event["period"]]
    if action in ("load_gstr2b", "load_bank"):
        args.append(event["key"])
    return _ACTIONS[action](*args)
