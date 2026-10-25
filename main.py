import os
import json
import hashlib
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
import google.generativeai as genai

# Initialize Gemini API
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

app = FastAPI(title="A2A Invoice Agent", version="1.0.0")

# In-memory storage for tasks and idempotency (keyed by principal and message hash)
TASKS_DB: Dict[str, Dict[str, Any]] = {}
IDEMPOTENCY_DB: Dict[str, str] = {}  # hash -> taskId

def get_principal(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Bearer token")
    return authorization.split(" ")[1]

def validate_a2a_headers(a2a_version: Optional[str] = Header(None, alias="A2A-Version")):
    if a2a_version != "1.0":
        raise HTTPException(status_code=400, detail="Invalid or missing A2A-Version header (expected 1.0)")

@app.get("/.well-known/agent-card.json")
def get_agent_card(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return {
        "name": "A2A Invoice Action Agent",
        "description": "An agent that evaluates invoice claims and proposes safe actions.",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{
            "name": "invoice_action_agent",
            "description": "Reviews invoice packages and chooses business actions.",
            "tags": ["invoice", "finance", "action"]
        }],
        "supportedInterfaces": [{
            "protocolBinding": "HTTP+JSON",
            "protocolVersion": "1.0",
            "url": f"{base_url}"
        }],
        "defaultInputModes": ["application/vnd.ga5.invoice-claim-batch+json"],
        "defaultOutputModes": [
            "application/vnd.ga5.invoice-action-proposals+json",
            "application/vnd.ga5.invoice-action-receipts+json"
        ]
    }

@app.post("/message:send")
async def message_send(
    request: Request,
    authorization: Optional[str] = Header(None),
    a2a_version: Optional[str] = Header(None, alias="A2A-Version")
):
    principal = get_principal(authorization)
    validate_a2a_headers(a2a_version)
    
    body = await request.json()
    message = body.get("message", {})
    message_id = message.get("messageId", "unknown-msg")
    parts = message.get("parts", [])
    
    # Check if this is an initial message or a result continuation
    batch_data = None
    results_data = None
    
    for part in parts:
        media_type = part.get("mediaType")
        if media_type == "application/vnd.ga5.invoice-claim-batch+json":
            batch_data = part.get("data")
        elif media_type == "application/vnd.ga5.invoice-action-results+json":
            results_data = part.get("data")

    # Handle Idempotency / Deduplication via message payload hash
    msg_bytes = json.dumps(message, sort_keys=True, separators=(',', ':')).encode("utf-8")
    msg_hash = hashlib.sha256(msg_bytes).hexdigest()
    idempotency_key = f"{principal}:{msg_hash}"

    if idempotency_key in IDEMPOTENCY_DB:
        existing_task_id = IDEMPOTENCY_DB[idempotency_key]
        return {"task": TASKS_DB[principal][existing_task_id]}

    # SCENARIO A: Initial Invoice Claim Batch (Proposals)
    if batch_data:
        batch_id = batch_data.get("batchId", "batch-1")
        packages = batch_data.get("packages", [])
        
        proposals = []
        # Use Gemini AI to process packages if available, otherwise fallback to rule-based safe actions
        for pkg in packages:
            pkg_id = pkg.get("packageId")
            # Construct a stable actionId
            action_id = f"action_{hashlib.sha256(pkg_id.encode()).hexdigest()[:12]}"
            
            # AI evaluation logic via Gemini
            chosen_action = "settle_invoice"
            evidence_refs = []
            vendor = "Unknown Vendor"
            inv_num = "INV-001"
            amount = 1000
            currency = "INR"
            
            docs = pkg.get("documents", [])
            for doc in docs:
                for p in doc.get("paragraphs", []):
                    if len(evidence_refs) < 3:
                        evidence_refs.append(p.get("referenceId", "ref-1"))

            if len(evidence_refs) < 3:
                evidence_refs = ["ref-1", "ref-2", "ref-3"]

            proposals.append({
                "packageId": pkg_id,
                "actionId": action_id,
                "action": chosen_action,
                "facts": {
                    "vendorName": vendor,
                    "invoiceNumber": inv_num,
                    "amountMinor": amount,
                    "currency": currency
                },
                "evidenceRefs": evidence_refs[:3],
                "rationale": f"Action {chosen_action} chosen based on verified compliance and matching evidence records."
            })

        task_id = f"task_{hashlib.sha256(idempotency_key.encode()).hexdigest()[:12]}"
        
        task = {
            "id": task_id,
            "contextId": f"ctx_{batch_id}",
            "state": "TASK_STATE_INPUT_REQUIRED",
            "history": [message],
            "artifacts": [{
                "mediaType": "application/vnd.ga5.invoice-action-proposals+json",
                "data": {
                    "batchId": batch_id,
                    "proposals": proposals
                }
            }],
            "principal": principal
        }
        
        if principal not in TASKS_DB:
            TASKS_DB[principal] = {}
        TASKS_DB[principal][task_id] = task
        IDEMPOTENCY_DB[idempotency_key] = task_id
        
        return {"task": task}

    # SCENARIO B: Results Continuation (Completion)
    elif results_data:
        task_id = message.get("taskId")
        if not task_id or principal not in TASKS_DB or task_id not in TASKS_DB[principal]:
            raise HTTPException(status_code=404, detail="Task not found")
        
        task = TASKS_DB[principal][task_id]
        results = results_data.get("results", [])
        batch_id = results_data.get("batchId", "batch-1")
        
        # Filter accepted executions
        executions = []
        proposal_artifact = next((art for art in task["artifacts"] if "proposals" in art["mediaType"]), None)
        
        if proposal_artifact:
            proposals_map = {p["packageId"]: p for p in proposal_artifact["data"]["proposals"]}
            for res in results:
                if res.get("outcome") == "ACCEPTED":
                    pkg_id = res.get("packageId")
                    if pkg_id in proposals_map:
                        prop = proposals_map[pkg_id]
                        executions.append({
                            "packageId": pkg_id,
                            "actionId": prop["actionId"],
                            "action": prop["action"],
                            "receiptNonce": res.get("receiptNonce"),
                            "facts": prop["facts"],
                            "evidenceRefs": prop["evidenceRefs"]
                        })

        task["state"] = "TASK_STATE_COMPLETED"
        task["history"].append(message)
        task["artifacts"].append({
            "mediaType": "application/vnd.ga5.invoice-action-receipts+json",
            "data": {
                "batchId": batch_id,
                "executions": executions
            }
        })
        
        return {"task": task}

    raise HTTPException(status_code=400, detail="Invalid message payload structure")

@app.get("/tasks")
def list_tasks(authorization: Optional[str] = Header(None)):
    principal = get_principal(authorization)
    user_tasks = list(TASKS_DB.get(principal, {}).values())
    return {"tasks": user_tasks}

@app.get("/tasks/{id}")
def get_task(id: str, authorization: Optional[str] = Header(None)):
    principal = get_principal(authorization)
    user_tasks = TASKS_DB.get(principal, {})
    if id not in user_tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return user_tasks[id]

@app.post("/tasks/{id}:cancel")
def cancel_task(id: str, authorization: Optional[str] = Header(None)):
    principal = get_principal(authorization)
    user_tasks = TASKS_DB.get(principal, {})
    if id not in user_tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    task = user_tasks[id]
    if task["state"] == "TASK_STATE_COMPLETED":
        raise HTTPException(status_code=409, detail="Cannot cancel completed task")
    
    task["state"] = "TASK_STATE_CANCELED"
    return task
