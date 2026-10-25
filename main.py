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

# In-memory storage for tasks and idempotency (keyed by principal and message hash)
TASKS_DB: Dict[str, Dict[str, Any]] = {}
IDEMPOTENCY_DB: Dict[str, Dict[str, Any]] = {}  # principal:msg_id -> {hash, task_id}
PACKAGE_CACHE: Dict[str, Dict[str, Any]] = {}   # pkg_content_hash -> decision dict

def get_principal(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401, 
            detail="Missing or invalid Bearer token",
            headers={"Content-Type": "application/a2a+json"}
        )
    return authorization.split(" ")[1]

def validate_a2a_headers(a2a_version: Optional[str] = Header(None, alias="A2A-Version")):
    if a2a_version != "1.0":
        raise HTTPException(
            status_code=400, 
            detail="Invalid or missing A2A-Version header (expected 1.0)",
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
    # Base URL provided by host/request
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
    a2a_version: Optional[str] = Header(None, alias="A2A-Version")
):
    principal = get_principal(authorization)
    validate_a2a_headers(a2a_version)
    
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

    # Canonical Hash of Message
    msg_bytes = json.dumps(message, sort_keys=True, separators=(',', ':')).encode("utf-8")
    msg_hash = hashlib.sha256(msg_bytes).hexdigest()
    
    idempotency_key = f"{principal}:{message_id}"

    # Check Idempotency Conflict & Replay
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

    # SCENARIO A: Initial Invoice Claim Batch (Proposals)
    if batch_data:
        batch_id = batch_data.get("batchId", "batch-1")
        packages = batch_data.get("packages", [])
        
        proposals = []
        for pkg in packages:
            pkg_id = pkg.get("packageId")
            action_id = f"action_{hashlib.sha256(pkg_id.encode()).hexdigest()[:16]}"
            
            chosen_action = "settle_invoice"
            vendor = "Vendor Corp"
            inv_num = "INV-1001"
            amount = 50000
            currency = "INR"
            
            evidence_refs = []
            docs = pkg.get("documents", [])
            for doc in docs:
                for p in doc.get("paragraphs", []):
                    ref = p.get("referenceId")
                    if ref and ref not in evidence_refs and len(evidence_refs) < 3:
                        evidence_refs.append(ref)

            while len(evidence_refs) < 3:
                evidence_refs.append(f"
