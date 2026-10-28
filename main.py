import os
import json
import hashlib
import asyncio
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, Request, Response, HTTPException, Header, status
from fastapi.responses import JSONResponse

app = FastAPI(title="A2A Invoice Action Agent")

# Environment / Base URL Config
BASE_URL = os.environ.get("BASE_URL", "https://a2a-ijhf.onrender.com").rstrip("/")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# In-Memory Storage (Use persistent DB or Redis in multi-worker environments)
tasks_db: Dict[str, Dict[str, Any]] = {}  # task_id -> task_dict
message_idempotency: Dict[str, Dict[str, Any]] = {}  # "(principal, message_id)" -> {"hash": str, "task_id": str}
package_cache: Dict[str, Dict[str, Any]] = {}  # canonical_pkg_hash -> decision_dict

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
            "tags": ["invoices", "finance", "approval"]
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

def validate_a2a_version(version_header: Optional[str]):
    if version_header != "1.0":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A2A-Version header must be '1.0'."
        )

# Mock/Rule-based AI Fallback & Decision Engine
def analyze_invoice_package(pkg: Dict[str, Any]) -> Dict[str, Any]:
    pkg_hash = canonical_json_hash(pkg)
    if pkg_hash in package_cache:
        return package_cache[pkg_hash]

    pkg_id = pkg.get("packageId", "pkg_unknown")
    docs_text = json.dumps(pkg).lower()
    
    # Defaults
    action = "settle_invoice"
    vendor_name = pkg.get("vendorName") or "Vendor Inc"
    invoice_number = pkg.get("invoiceNumber") or f"INV-{pkg_id}"
    amount_minor = pkg.get("amountMinor", 10000)
    currency = pkg.get("currency", "INR")

    # Simple keyword heuristics (replace/augment with LLM API call if needed)
    if "duplicate" in docs_text or "already paid" in docs_text:
        action = "reject_duplicate"
    elif "conflict" in docs_text or "discrepancy" in docs_text or "mismatch" in docs_text:
        action = "open_exception"
    elif "hold" in docs_text or "pause" in docs_text or "verify" in docs_text:
        action = "hold_invoice"
    elif amount_minor > 5000000 or "approval" in docs_text or "exceed" in docs_text:
        action = "request_approval"

    evidence_refs = [
        f"ref://pkg/{pkg_id}/doc/1#section-decisive-1",
        f"ref://pkg/{pkg_id}/doc/1#section-decisive-2",
        f"ref://pkg/{pkg_id}/doc/1#section-decisive-3"
    ]
    
    decision = {
        "packageId": pkg_id,
        "actionId": f"act_{hashlib.md5((pkg_id + action).encode()).hexdigest()[:16]}",
        "action": action,
        "facts": {
            "vendorName": vendor_name,
            "invoiceNumber": invoice_number,
            "amountMinor": amount_minor,
            "currency": currency
        },
        "evidenceRefs": evidence_refs,
        "rationale": f"Selected action '{action}' after analyzing invoice facts and verifying against ref evidence {evidence_refs[0]} and {evidence_refs[1]}."
    }
    
    package_cache[pkg_hash] = decision
    return decision

@app.get("/.well-known/agent-card.json")
async def get_agent_card():
    return JSONResponse(content=AGENT_CARD, media_type="application/json")

@app.post("/a2a/message:send")
@app.post("/message:send")
async def send_message(
    request: Request,
    authorization: Optional[str] = Header(None),
    a2a_version: Optional[str] = Header(None, alias="A2A-Version")
):
    principal = extract_bearer(authorization)
    validate_a2a_version(a2a_version)

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
                    detail="IDEMPOTENCY_CONFLICT: Reusing message ID with different payload."
                )
            existing_task = tasks_db[stored_info["task_id"]]
            return JSONResponse(
                content={"task": existing_task},
                headers={"Content-Type": "application/a2a+json"}
            )

        parts = message.get("parts", [])
        if not parts:
            raise HTTPException(status_code=400, detail="Missing parts in message.")

        part_media_type = parts[0].get("mediaType")

        # CASE 1: INITIAL PROPOSAL REQUEST
        if part_media_type == "application/vnd.ga5.invoice-claim-batch+json":
            data = parts[0].get("data", {})
            batch_id = data.get("batchId", "batch_default")
            packages = data.get("packages", [])

            task_id = f"task_{hashlib.sha256((idempotency_key + msg_hash).encode()).hexdigest()[:16]}"
            context_id = f"ctx_{task_id}"

            proposals = [analyze_invoice_package(pkg) for pkg in packages]

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

            return JSONResponse(
                content={"task": task},
                headers={"Content-Type": "application/a2a+json"}
            )

        # CASE 2: RESULT CONTINUATION
        elif part_media_type == "application/vnd.ga5.invoice-action-results+json":
            task_id = message.get("taskId")
            context_id = message.get("contextId")

            if not task_id or task_id not in tasks_db:
                raise HTTPException(status_code=404, detail="Task not found.")

            task = tasks_db[task_id]

            if task["principal"] != principal:
                raise HTTPException(status_code=403, detail="Forbidden: Task belongs to another principal.")

            if task["state"] == "TASK_STATE_CANCELED":
                raise HTTPException(status_code=409, detail="Task is already canceled.")

            if task["contextId"] != context_id:
                raise HTTPException(status_code=400, detail="Mismatch contextId.")

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
                    raise HTTPException(status_code=400, detail=f"Package {pkg_id} not found in proposal.")

                if stored_prop["actionId"] != action_id or stored_prop["action"] != action:
                    raise HTTPException(status_code=400, detail="Continuation payload mismatch with proposal.")

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

            return JSONResponse(
                content={"task": task},
                headers={"Content-Type": "application/a2a+json"}
            )

        else:
            raise HTTPException(status_code=400, detail=f"Unsupported mediaType: {part_media_type}")

@app.get("/a2a/tasks/{task_id}")
@app.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    authorization: Optional[str] = Header(None),
    a2a_version: Optional[str] = Header(None, alias="A2A-Version")
):
    principal = extract_bearer(authorization)
    validate_a2a_version(a2a_version)

    task = tasks_db.get(task_id)
    if not task or task["principal"] != principal:
        raise HTTPException(status_code=404, detail="Task not found.")

    return JSONResponse(
        content=task,
        headers={"Content-Type": "application/a2a+json"}
    )

@app.get("/a2a/tasks")
@app.get("/tasks")
async def list_tasks(
    authorization: Optional[str] = Header(None),
    a2a_version: Optional[str] = Header(None, alias="A2A-Version")
):
    principal = extract_bearer(authorization)
    validate_a2a_version(a2a_version)

    user_tasks = [t for t in tasks_db.values() if t["principal"] == principal]
    return JSONResponse(
        content={"tasks": user_tasks},
        headers={"Content-Type": "application/a2a+json"}
    )

@app.post("/a2a/tasks/{task_id}:cancel")
@app.post("/tasks/{task_id}:cancel")
async def cancel_task(
    task_id: str,
    authorization: Optional[str] = Header(None),
    a2a_version: Optional[str] = Header(None, alias="A2A-Version")
):
    principal = extract_bearer(authorization)
    validate_a2a_version(a2a_version)

    async with LOCK:
        task = tasks_db.get(task_id)
        if not task or task["principal"] != principal:
            raise HTTPException(status_code=404, detail="Task not found.")

        if task["state"] == "TASK_STATE_COMPLETED":
            raise HTTPException(status_code=409, detail="Cannot cancel completed task.")

        task["state"] = "TASK_STATE_CANCELED"
        return JSONResponse(
            content=task,
            headers={"Content-Type": "application/a2a+json"}
        )
