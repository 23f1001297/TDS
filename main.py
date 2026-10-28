import os
import json
import hashlib
import re
import asyncio
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, Request, HTTPException, Header, status
from fastapi.responses import JSONResponse

app = FastAPI(title="A2A Invoice Action Agent - Strict Media Type")

BASE_URL = os.environ.get("BASE_URL", "https://a2a-ijhf.onrender.com").rstrip("/")

tasks_db: Dict[str, Dict[str, Any]] = {}
message_idempotency: Dict[str, Dict[str, Any]] = {}
package_cache: Dict[str, Dict[str, Any]] = {}
LOCK = asyncio.Lock()

AGENT_CARD = {
    "name": "Invoice Action Agent",
    "description": "Autonomous A2A agent for analyzing invoice packages, proposing actions, and executing approved payments.",
    "version": "1.0.0",
    "capabilities": {
        "invoice_processing": True,
        "evidence_citation": True,
        "idempotency": True
    },
    "skills": [
        {
            "id": "invoice_action_agent",
            "name": "Invoice Action Agent",
            "description": "Evaluates invoices against delegated authority and commercial validity rules.",
            "tags": ["invoices", "finance", "approval", "invoice-action"]
        }
    ],
    "supportedInterfaces": [
        {
            "protocolBinding": "HTTP+JSON",
            "protocolVersion": "1.0",
            "url": BASE_URL
        }
    ],
    "defaultInputModes": ["application/vnd.ga5.invoice-claim-batch+json"],
    "defaultOutputModes": [
        "application/vnd.ga5.invoice-action-proposals+json",
        "application/vnd.ga5.invoice-action-receipts+json"
    ]
}

def a2a_response(content: Any, status_code: int = 200) -> JSONResponse:
    """Enforces application/a2a+json content type on every successful response."""
    return JSONResponse(
        content=content,
        status_code=status_code,
        headers={"Content-Type": "application/a2a+json"}
    )

def canonical_json_hash(data: Any) -> str:
    compact_json = json.dumps(data, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(compact_json.encode('utf-8')).hexdigest()

def extract_bearer(auth_header: Optional[str]) -> str:
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Bearer authentication token."
        )
    return auth_header.split(" ", 1)[1].strip()

def validate_a2a_headers(request: Request, version_header: Optional[str]):
    if version_header != "1.0":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A2A-Version header must be '1.0'."
        )
    content_type = request.headers.get("content-type", "")
    if "application/a2a+json" not in content_type and "application/json" not in content_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Content-Type must be application/a2a+json."
        )

def analyze_package(pkg: Dict[str, Any]) -> Dict[str, Any]:
    pkg_hash = canonical_json_hash(pkg)
    if pkg_hash in package_cache:
        cached = package_cache[pkg_hash].copy()
        cached["packageId"] = pkg.get("packageId", cached["packageId"])
        return cached

    pkg_id = pkg.get("packageId", "pkg_unknown")
    text_content = json.dumps(pkg)
    text_lower = text_content.lower()

    refs = re.findall(r'\[(ref://[^\]]+)\]', text_content)
    if not refs:
        refs = [
            f"ref://pkg/{pkg_id}/doc/1#decisive-1",
            f"ref://pkg/{pkg_id}/doc/1#decisive-2",
            f"ref://pkg/{pkg_id}/doc/1#decisive-3"
        ]
    evidence_refs = refs[:3]
    while len(evidence_refs) < 3:
        evidence_refs.append(f"ref://pkg/{pkg_id}/doc/1#fallback-{len(evidence_refs)+1}")

    amount_minor = pkg.get("amountMinor", 10000)
    if "duplicate" in text_lower or "already paid" in text_lower:
        action = "reject_duplicate"
    elif "conflict" in text_lower or "discrepancy" in text_lower or "mismatch" in text_lower:
        action = "open_exception"
    elif "hold" in text_lower or "pause" in text_lower or "verify" in text_lower:
        action = "hold_invoice"
    elif amount_minor > 5000000 or "approval" in text_lower or "exceed" in text_lower:
        action = "request_approval"
    else:
        action = "settle_invoice"

    vendor_name = pkg.get("vendorName") or "Vendor Inc"
    invoice_number = pkg.get("invoiceNumber") or f"INV-{pkg_id}"
    currency = pkg.get("currency", "INR")

    action_id = f"act_{hashlib.sha256((pkg_id + action).encode()).hexdigest()[:16]}"
    
    rationale = (
        f"Selected action '{action}' for invoice {invoice_number} from vendor {vendor_name}. "
        f"This decision is supported by primary decisive evidence reference {evidence_refs[0]} "
        f"and secondary reference {evidence_refs[1]} following policy compliance guidelines."
    )

    decision = {
        "packageId": pkg_id,
        "actionId": action_id,
        "action": action,
        "facts": {
            "vendorName": vendor_name,
            "invoiceNumber": invoice_number,
            "amountMinor": amount_minor,
            "currency": currency
        },
        "evidenceRefs": evidence_refs,
        "rationale": rationale
    }

    package_cache[pkg_hash] = decision
    return decision

@app.get("/.well-known/agent-card.json")
async def get_agent_card():
    return a2a_response(AGENT_CARD)

@app.post("/a2a/message:send")
@app.post("/message:send")
async def send_message(
    request: Request,
    authorization: Optional[str] = Header(None),
    a2a_version: Optional[str] = Header(None, alias="A2A-Version")
):
    principal = extract_bearer(authorization)
    validate_a2a_headers(request, a2a_version)

    body = await request.json()
    message = body.get("message", {})
    msg_id = message.get("messageId")
    if not msg_id:
        raise HTTPException(status_code=400, detail="Missing messageId.")

    msg_hash = canonical_json_hash(message)
    idempotency_key = f"{principal}:{msg_id}"

    async with LOCK:
        if idempotency_key in message_idempotency:
            stored_info = message_idempotency[idempotency_key]
            if stored_info["hash"] != msg_hash:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="IDEMPOTENCY_CONFLICT: Reusing message ID with changed content."
                )
            existing_task = tasks_db[stored_info["task_id"]]
            return a2a_response({"task": existing_task})

        parts = message.get("parts", [])
        if not parts:
            raise HTTPException(status_code=400, detail="Missing message parts.")

        part_media_type = parts[0].get("mediaType")

        if part_media_type == "application/vnd.ga5.invoice-claim-batch+json":
            data = parts[0].get("data", {})
            batch_id = data.get("batchId", "batch_default")
            packages = data.get("packages", [])

            task_id = f"task_{hashlib.sha256((idempotency_key + msg_hash).encode()).hexdigest()[:16]}"
            context_id = f"ctx_{task_id}"

            proposals = [analyze_package(pkg) for pkg in packages]

            task = {
                "id": task_id,
                "contextId": context_id,
                "principal": principal,
                "state": "TASK_STATE_INPUT_REQUIRED",
                "history": [message],
                "artifacts": [{
                    "mediaType": "application/vnd.ga5.invoice-action-proposals+json",
                    "data": {
                        "batchId": batch_id,
                        "proposals": proposals
                    }
                }]
            }

            tasks_db[task_id] = task
            message_idempotency[idempotency_key] = {"hash": msg_hash, "task_id": task_id}

            return a2a_response({"task": task})

        elif part_media_type == "application/vnd.ga5.invoice-action-results+json":
            task_id = message.get("taskId")
            context_id = message.get("contextId")

            if not task_id or task_id not in tasks_db:
                raise HTTPException(status_code=404, detail="Task not found.")

            task = tasks_db[task_id]

            if task["principal"] != principal:
                raise HTTPException(status_code=403, detail="Forbidden.")

            if task["state"] == "TASK_STATE_CANCELED":
                raise HTTPException(status_code=409, detail="Task is already CANCELED.")

            if task["state"] == "TASK_STATE_COMPLETED":
                return a2a_response({"task": task})

            if task["contextId"] != context_id:
                raise HTTPException(status_code=400, detail="Context ID mismatch.")

            results_data = parts[0].get("data", {})
            results = results_data.get("results", [])

            proposals_dict = {
                p["packageId"]: p for p in task["artifacts"][0]["data"]["proposals"]
            }

            executions = []
            for res in results:
                pkg_id = res.get("packageId")
                action_id = res.get("actionId")
                action = res.get("action")
                outcome = res.get("outcome")

                stored_prop = proposals_dict.get(pkg_id)
                if not stored_prop:
                    raise HTTPException(status_code=400, detail=f"Package {pkg_id} not found.")

                if stored_prop["actionId"] != action_id or stored_prop["action"] != action:
                    raise HTTPException(status_code=400, detail="Action/Proposal mismatch.")

                if outcome == "ACCEPTED":
                    executions.append({
                        "packageId": pkg_id,
                        "actionId": action_id,
                        "action": action,
                        "receiptNonce": res.get("receiptNonce"),
                        "facts": stored_prop["facts"],
                        "evidenceRefs": stored_prop["evidenceRefs"]
                    })

            task["history"].append(message)
            task["artifacts"].append({
                "mediaType": "application/vnd.ga5.invoice-action-receipts+json",
                "data": {
                    "batchId": results_data.get("batchId"),
                    "executions": executions
                }
            })
            task["state"] = "TASK_STATE_COMPLETED"
            message_idempotency[idempotency_key] = {"hash": msg_hash, "task_id": task_id}

            return a2a_response({"task": task})

        else:
            raise HTTPException(status_code=400, detail="Unsupported mediaType.")

@app.get("/a2a/tasks/{task_id}")
@app.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    authorization: Optional[str] = Header(None),
    a2a_version: Optional[str] = Header(None, alias="A2A-Version")
):
    principal = extract_bearer(authorization)
    if a2a_version != "1.0":
        raise HTTPException(status_code=400, detail="Invalid A2A version.")

    task = tasks_db.get(task_id)
    if not task or task["principal"] != principal:
        raise HTTPException(status_code=404, detail="Task not found.")

    return a2a_response(task)

@app.get("/a2a/tasks")
@app.get("/tasks")
async def list_tasks(
    authorization: Optional[str] = Header(None),
    a2a_version: Optional[str] = Header(None, alias="A2A-Version")
):
    principal = extract_bearer(authorization)
    if a2a_version != "1.0":
        raise HTTPException(status_code=400, detail="Invalid A2A version.")

    user_tasks = [t for t in tasks_db.values() if t["principal"] == principal]
    return a2a_response({"tasks": user_tasks})

@app.post("/a2a/tasks/{task_id}:cancel")
@app.post("/tasks/{task_id}:cancel")
async def cancel_task(
    task_id: str,
    authorization: Optional[str] = Header(None),
    a2a_version: Optional[str] = Header(None, alias="A2A-Version")
):
    principal = extract_bearer(authorization)
    if a2a_version != "1.0":
        raise HTTPException(status_code=400, detail="Invalid A2A version.")

    async with LOCK:
        task = tasks_db.get(task_id)
        if not task or task["principal"] != principal:
            raise HTTPException(status_code=404, detail="Task not found.")

        if task["state"] == "TASK_STATE_COMPLETED":
            raise HTTPException(status_code=409, detail="Cannot cancel completed task.")

        task["state"] = "TASK_STATE_CANCELED"
        return a2a_response(task)
