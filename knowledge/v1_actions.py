"""
V1 Action Lifecycle & Audit Export — backend-first migration slice.

Provides durable action-request workflow:
    create → approve / reject → execute

Persistence: flat JSON files under ./data/actions/
Audit export: returns full action log as JSON array.
"""

import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

DATA_DIR = Path("data/actions")
DATA_DIR.mkdir(parents=True, exist_ok=True)

INDEX_PATH = DATA_DIR / "index.json"


def _load_index() -> list[dict]:
    if INDEX_PATH.exists():
        with open(INDEX_PATH) as f:
            return json.load(f)
    return []


def _save_index(items: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(INDEX_PATH, "w") as f:
        json.dump(items, f, indent=2, default=str)


def _find_request(request_id: str) -> tuple[int, dict]:
    items = _load_index()
    for i, item in enumerate(items):
        if item["id"] == request_id:
            return i, item
    raise HTTPException(status_code=404, detail=f"Action request not found: {request_id}")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ActionStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    executing = "executing"
    completed = "completed"
    failed = "failed"


class BulkTarget(BaseModel):
    """Single target inside a library.bulk operation."""
    item_id: str
    operation: str = Field(..., description="delete | retag | reindex")
    params: dict[str, Any] = Field(default_factory=dict, description="Operation-specific params (e.g. tags, kb_id)")


class ActionPayload(BaseModel):
    """Describes what the action will do."""
    type: str = Field(..., description="Operation type, e.g. 'library.bulk'")
    targets: list[BulkTarget] = Field(default_factory=list, description="Targets for bulk operations")
    params: dict[str, Any] = Field(default_factory=dict, description="Top-level params")


class ActionRequestCreate(BaseModel):
    """Body for POST /v1/actions/requests."""
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="")
    payload: ActionPayload
    requested_by: str = Field(default="system")


class ActionRequestResponse(BaseModel):
    id: str
    title: str
    description: str
    payload: dict[str, Any]
    status: ActionStatus
    requested_by: str
    reviewed_by: Optional[str] = None
    review_note: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    created_at: str
    updated_at: str


class ReviewBody(BaseModel):
    reviewed_by: str = Field(default="admin")
    note: str = Field(default="")


# ---------------------------------------------------------------------------
# Library-operation executor (reuses server.py helpers at runtime)
# ---------------------------------------------------------------------------

def _get_library_helpers():
    """Late-import to avoid circular dependency with server.py."""
    from server import load_library, save_library
    return load_library, save_library


async def _execute_library_bulk(targets: list[BulkTarget]) -> dict[str, Any]:
    """Execute a list of library bulk operations, returning per-target results."""
    load_library, save_library = _get_library_helpers()
    results: list[dict[str, Any]] = []

    for target in targets:
        op = target.operation
        item_id = target.item_id
        try:
            if op == "delete":
                items = load_library()
                item = next((it for it in items if it.get("id") == item_id), None)
                if not item:
                    results.append({"item_id": item_id, "operation": op, "ok": False, "error": "not found"})
                    continue
                # Delete files
                job_dir = None
                for output_info in item.get("output_files", {}).values():
                    if isinstance(output_info, dict) and "path" in output_info:
                        p = Path(output_info["path"])
                        if p.exists():
                            job_dir = p.parent
                            break
                if not job_dir and "path" in item:
                    p = Path(item["path"])
                    if p.exists():
                        job_dir = p.parent
                library_dir = Path("library")
                if job_dir and job_dir.exists() and job_dir != library_dir:
                    shutil.rmtree(job_dir)
                items = [i for i in items if i.get("id") != item_id]
                save_library(items)
                results.append({"item_id": item_id, "operation": op, "ok": True})

            elif op == "retag":
                items = load_library()
                item = next((it for it in items if it.get("id") == item_id), None)
                if not item:
                    results.append({"item_id": item_id, "operation": op, "ok": False, "error": "not found"})
                    continue
                tags = target.params.get("tags", {})
                if "tags" not in item:
                    item["tags"] = {}
                for key in ("organization", "course_code", "course_id", "module_id", "topic_ids", "custom_tags"):
                    if key in tags:
                        item["tags"][key] = tags[key]
                save_library(items)
                results.append({"item_id": item_id, "operation": op, "ok": True, "tags": item["tags"]})

            elif op == "reindex":
                kb_id = target.params.get("kb_id")
                if not kb_id:
                    results.append({"item_id": item_id, "operation": op, "ok": False, "error": "kb_id required"})
                    continue
                # Delegate to existing reingest endpoint logic
                from server import get_kb_registry, get_kb_pipeline
                items = load_library()
                item = next((it for it in items if it.get("id") == item_id), None)
                if not item:
                    results.append({"item_id": item_id, "operation": op, "ok": False, "error": "not found"})
                    continue
                output_files = item.get("output_files", {})
                pdf_path = None
                if "searchable_pdf" in output_files:
                    pdf_path = Path(output_files["searchable_pdf"].get("path", ""))
                if not pdf_path or not pdf_path.exists():
                    results.append({"item_id": item_id, "operation": op, "ok": False, "error": "no PDF available"})
                    continue
                registry = get_kb_registry()
                kb = registry.get(kb_id)
                if not kb:
                    results.append({"item_id": item_id, "operation": op, "ok": False, "error": f"KB not found: {kb_id}"})
                    continue
                from pipeline.pdf_extractor import PDFExtractor
                extractor = PDFExtractor(str(pdf_path))
                page_count = extractor.get_page_count()
                toc_chapters = extractor.detect_chapters_from_toc()
                chapters = extractor.detect_chapters_from_text(
                    {"chapters": toc_chapters} if toc_chapters else None
                )
                chapters_data = []
                if chapters:
                    for ci, ch in enumerate(chapters):
                        chapters_data.append({
                            "number": ci + 1,
                            "title": ch.title,
                            "text": ch.content,
                            "start_page": ch.page_start,
                        })
                else:
                    full_text = ""
                    for pn in range(page_count):
                        full_text += extractor.extract_page_text(pn) + "\n"
                    chapters_data = [{"number": 1, "title": item.get("title", "Full Document"), "text": full_text.strip(), "start_page": 0}]
                extractor.close()
                pipeline = get_kb_pipeline()
                doc = pipeline.ingest_document(
                    kb_id=kb_id,
                    title=item.get("title", "Unknown"),
                    author=item.get("author", "Unknown"),
                    chapters=chapters_data,
                    source_file=pdf_path.name,
                    total_pages=page_count,
                )
                output_files["knowledge_base"] = {"kb_id": kb_id, "document_id": doc.id}
                item["output_files"] = output_files
                save_library(items)
                results.append({"item_id": item_id, "operation": op, "ok": True, "document_id": doc.id, "kb_id": kb_id})

            else:
                results.append({"item_id": item_id, "operation": op, "ok": False, "error": f"unknown operation: {op}"})

        except Exception as e:
            logger.error(f"Bulk op {op} on {item_id} failed: {e}")
            results.append({"item_id": item_id, "operation": op, "ok": False, "error": str(e)})

    succeeded = sum(1 for r in results if r.get("ok"))
    return {"total": len(results), "succeeded": succeeded, "failed": len(results) - succeeded, "details": results}


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["v1-actions"])


@router.post("/v1/actions/requests", response_model=ActionRequestResponse, status_code=201)
async def create_action_request(body: ActionRequestCreate):
    """Create a new action request (status=pending)."""
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "id": str(uuid.uuid4()),
        "title": body.title,
        "description": body.description,
        "payload": body.payload.model_dump(),
        "status": ActionStatus.pending.value,
        "requested_by": body.requested_by,
        "reviewed_by": None,
        "review_note": None,
        "result": None,
        "created_at": now,
        "updated_at": now,
    }
    items = _load_index()
    items.insert(0, record)
    _save_index(items)
    logger.info(f"Action request created: {record['id']} — {body.title}")
    return record


@router.get("/v1/actions/requests", response_model=list[ActionRequestResponse])
async def list_action_requests(
    status: Optional[ActionStatus] = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List action requests with optional status filter."""
    items = _load_index()
    if status:
        items = [i for i in items if i["status"] == status.value]
    return items[offset : offset + limit]


@router.get("/v1/actions/requests/{request_id}", response_model=ActionRequestResponse)
async def get_action_request(request_id: str):
    """Retrieve a single action request by ID."""
    _, record = _find_request(request_id)
    return record


@router.post("/v1/actions/requests/{request_id}/approve", response_model=ActionRequestResponse)
async def approve_action_request(request_id: str, body: ReviewBody):
    """Approve a pending action request."""
    items = _load_index()
    idx, record = _find_request(request_id)
    if record["status"] != ActionStatus.pending.value:
        raise HTTPException(status_code=409, detail=f"Cannot approve request in status '{record['status']}'")
    record["status"] = ActionStatus.approved.value
    record["reviewed_by"] = body.reviewed_by
    record["review_note"] = body.note
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    items[idx] = record
    _save_index(items)
    logger.info(f"Action request approved: {request_id} by {body.reviewed_by}")
    return record


@router.post("/v1/actions/requests/{request_id}/reject", response_model=ActionRequestResponse)
async def reject_action_request(request_id: str, body: ReviewBody):
    """Reject a pending action request."""
    items = _load_index()
    idx, record = _find_request(request_id)
    if record["status"] != ActionStatus.pending.value:
        raise HTTPException(status_code=409, detail=f"Cannot reject request in status '{record['status']}'")
    record["status"] = ActionStatus.rejected.value
    record["reviewed_by"] = body.reviewed_by
    record["review_note"] = body.note
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    items[idx] = record
    _save_index(items)
    logger.info(f"Action request rejected: {request_id} by {body.reviewed_by}")
    return record


@router.post("/v1/actions/requests/{request_id}/execute", response_model=ActionRequestResponse)
async def execute_action_request(request_id: str):
    """Execute an approved action request.

    Currently supports payload type ``library.bulk`` which delegates to
    existing library operations (delete, retag, reindex).
    """
    items = _load_index()
    idx, record = _find_request(request_id)
    if record["status"] != ActionStatus.approved.value:
        raise HTTPException(status_code=409, detail=f"Cannot execute request in status '{record['status']}' (must be approved)")

    record["status"] = ActionStatus.executing.value
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    items[idx] = record
    _save_index(items)

    payload_type = record["payload"].get("type", "")
    try:
        if payload_type == "library.bulk":
            targets = [BulkTarget(**t) for t in record["payload"].get("targets", [])]
            result = await _execute_library_bulk(targets)
        else:
            raise ValueError(f"Unsupported payload type: {payload_type}")

        record["status"] = ActionStatus.completed.value
        record["result"] = result
    except Exception as e:
        logger.error(f"Action execution failed for {request_id}: {e}")
        record["status"] = ActionStatus.failed.value
        record["result"] = {"error": str(e)}

    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    items = _load_index()  # re-read in case concurrent writes
    for i, it in enumerate(items):
        if it["id"] == request_id:
            items[i] = record
            break
    _save_index(items)
    return record


# ---------------------------------------------------------------------------
# Audit export
# ---------------------------------------------------------------------------

@router.get("/v1/audit/actions/export")
async def export_audit_log(
    status: Optional[ActionStatus] = Query(None),
    since: Optional[str] = Query(None, description="ISO datetime lower-bound on created_at"),
    until: Optional[str] = Query(None, description="ISO datetime upper-bound on created_at"),
):
    """Export action audit log as JSON array.

    Supports optional filters by status and time range.
    """
    items = _load_index()
    if status:
        items = [i for i in items if i["status"] == status.value]
    if since:
        items = [i for i in items if i["created_at"] >= since]
    if until:
        items = [i for i in items if i["created_at"] <= until]

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "actions": items,
    }
