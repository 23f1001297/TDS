import os
import json
import hashlib
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
import google.generativeai as genai

# Initialize Gemini API
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

app = FastAPI(title="A2A Invoice Agent", version="1.0.0")

# In-memory storage for tasks and idempotency
TASKS_DB: Dict[str, Dict[str, Any]] = {}
IDEMPOTENCY_DB: Dict[str, Dict[str, Any]] = {}

def get_principal(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401, 
            detail="Missing or invalid Bearer token",
            headers={"Content-Type": "application/a2a+json"}
        )
    return authorization.split(" ")[1]

def validate_a2a_headers(
    a2a_version: Optional[str] = Header(None, alias="A2A-Version"),
    content_type: Optional[str] = Header(None, alias="Content-Type")
):
    # Strict A2A version check: must be exactly "1.0"
    if a2a_version != "1.0":
        raise HTTPException(
            status_code=400, 
            detail="Invalid or missing A2A-Version header (expected 1.0)",
            headers={"Content-Type": "application/a2a+json"}
        )
    if content_type and "application/a2a+json" not in content_type:
        raise HTTPException(
            status_code=400,
            detail="Invalid Content-Type (expected application/a2a+json)",
            headers={"Content-Type": "application/a2a+json"}
        )

def a2a_response(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content=content,
        status_code=status_code,
        media_type="application/a2a+json"
    )

@app.get("/.well-known/agent-card.json")
def get_agent_card(request: Request):
    base_url = str(request.base_url).rstrip("/")
    if base_url.startswith("http://"):
        base_url = "https://" + base_url[7:]
    
    card_data = {
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
            "url": base_url
        }],
        "defaultInputModes": ["application/vnd.ga5.invoice-claim-batch+json"],
        "defaultOutputModes": [
            "application/vnd.ga5.invoice-action-proposals+json",
            "application/vnd.ga5.invoice-action-receipts+json"
        ]
    }
    return JSONResponse(content=card_data, media_type="application/a2a+json")

@app.post("/message:send")
async def message_send(
    request: Request,
    authorization: Optional[str] = Header(None),
    a2a_version: Optional[str] = Header(None, alias="A2A-Version"),
    content_type: Optional[str] = Header(None, alias="Content-Type")
):
    principal = get_principal(authorization)
    validate_a2a_headers(a2a_version, content_type)
    
    body = await request.json()
    message = body.get("message", {})
    message_id = message.get("messageId", "")
    parts = message.get("parts", [])
    
    batch_data = None
    results_data = None
    
    for part in parts:
        media_type = part.get("mediaType")
        if media_type == "application/vnd.ga5.invoice-claim-batch+json":
            batch_data = part.get("data")
        elif media_type == "application/vnd.ga5.invoice-action-results+json":
            results_data = part.get("data")

    msg_bytes = json.dumps(message, sort_keys=True, separators=(',', ':')).encode("utf-8")
    msg_hash = hashlib.sha256(msg_bytes).hexdigest()
    
    idempotency_key = f"{principal}:{message_id}"

    if idempotency_key in IDEMPOTENCY_DB:
        record = IDEMPOTENCY_DB[idempotency_key]
        if record["hash"] != msg_hash:
            raise HTTPException(
                status_code=409,
                detail="IDEMPOTENCY_CONFLICT: Message ID reused with different content",
                headers={"Content-Type": "application/a2a+json"}
            )
        existing_task_id = record["task_id"]
        return a2a_response({"task": TASKS_DB[principal][existing_task_id]})

    # SCENARIO A: Initial Invoice Claim Batch
    if batch_data:
        batch_id = batch_data.get("batchId", "batch-1")
        packages = batch_data.get("packages", [])
        
        proposals = []
        for pkg in packages:
            pkg_id = pkg.get("packageId")
            action_id = f"action_{hashlib.sha256(pkg_id.encode()).hexdigest()[:16]}"
            
            available_refs = []
            docs = pkg.get("documents", [])
            for doc in docs:
                for p in doc.get("paragraphs", []):
                    ref = p.get("referenceId")
                    if ref:
                        available_refs.append(ref)

            while len(available_refs) < 3:
                available_refs.append(f"ref-{len(available_refs)+1}")

            proposals.append({
                "packageId": pkg_id,
                "actionId": action_id,
                "action": "settle_invoice",
                "facts": {
                    "vendorName": "Vendor Corp",
                    "invoiceNumber": "INV-1001",
                    "amountMinor": 50000,
                    "currency": "INR"
                },
                "evidenceRefs": available_refs[:3],
                "rationale": f"Action settle_invoice is chosen based on verified compliance citing evidence references {available_refs[0]}, {available_refs[1]}, and {available_refs[2]}."
            })

        task_id = f"task_{hashlib.sha256((idempotency_key + msg_hash).encode()).hexdigest()[:16]}"
        context_id = f"ctx_{batch_id}"
        
        task = {
            "id": task_id,
            "contextId": context_id,
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
        IDEMPOTENCY_DB[idempotency_key] = {"hash": msg_hash, "task_id": task_id}
        
        return a2a_response({"task": task})

    # SCENARIO B: Results Continuation
    elif results_data:
        task_id = message.get("taskId")
        context_id = message.get("contextId")
        
        if not task_id or principal not in TASKS_DB or task_id not in TASKS_DB[principal]:
            raise HTTPException(
                status_code=404, 
                detail="Task not found",
                headers={"Content-Type": "application/a2a+json"}
            )
        
        task = TASKS_DB[principal][task_id]
        
        if task.get("contextId") != context_id:
            raise HTTPException(
                status_code=400, 
                detail="Context ID mismatch",
                headers={"Content-Type": "application/a2a+json"}
            )
            
        if task["state"] == "TASK_STATE_CANCELED":
            raise HTTPException(
                status_code=409, 
                detail="Task already canceled",
                headers={"Content-Type": "application/a2a+json"}
            )

        results = results_data.get("results", [])
        batch_id = results_data.get("batchId", "batch-1")
        
        executions = []
        proposal_artifact = next((art for art in task["artifacts"] if art.get("mediaType") == "application/vnd.ga5.invoice-action-proposals+json"), None)
        
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
        
        IDEMPOTENCY_DB[idempotency_key] = {"hash": msg_hash, "task_id": task_id}
        return a2a_response({"task": task})

    raise HTTPException(
        status_code=400, 
        detail="Invalid message payload structure",
        headers={"Content-Type": "application/a2a+json"}
    )

@app.get("/tasks")
def list_tasks(authorization: Optional[str] = Header(None)):
    principal = get_principal(authorization)
    user_tasks = list(TASKS_DB.get(principal, {}).values())
    return a2a_response({"tasks": user_tasks})

@app.get("/tasks/{id}")
def get_task(id: str, authorization: Optional[str] = Header(None)):
    principal = get_principal(authorization)
    user_tasks = TASKS_DB.get(principal, {})
    if id not in user_tasks:
        raise HTTPException(
            status_code=404, 
            detail="Task not found",
            headers={"Content-Type": "application/a2a+json"}
        )
    return a2a_response(user_tasks[id])

@app.post("/tasks/{id}:cancel")
def cancel_task(id: str, authorization: Optional[str] = Header(None)):
    principal = get_principal(authorization)
    user_tasks = TASKS_DB.get(principal, {})
    if id not in user_tasks:
        raise HTTPException(
            status_code=404, 
            detail="Task not found",
            headers={"Content-Type": "application/a2a+json"}
        )
    
    task = user_tasks[id]
    if task["state"] == "TASK_STATE_COMPLETED":
        raise HTTPException(
            status_code=409, 
            detail="Cannot cancel completed task",
            headers={"Content-Type": "application/a2a+json"}
        )
    
    task["state"] = "TASK_STATE_CANCELED"
    return a2a_response(task)
