import os
import json
import hashlib
import sqlite3
import threading
import asyncio
import uuid
import re
import datetime
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import traceback

BASE_URL = os.environ.get("BASE_URL", "https://invoice-agent-nycj.onrender.com").rstrip("/") + "/a2a/"
LLM_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
LLM_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSy...") 
LLM_MODEL = "gemini-2.5-flash"
MEDIA = "application/a2a+json"
BATCH_T = "application/vnd.ga5.invoice-claim-batch+json"
PROP_T = "application/vnd.ga5.invoice-action-proposals+json"
RES_T = "application/vnd.ga5.invoice-action-results+json"
REC_T = "application/vnd.ga5.invoice-action-receipts+json"
ACTIONS = {"settle_invoice", "request_approval", "hold_invoice", "reject_duplicate", "open_exception"}
TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED"}

app = FastAPI()

db = sqlite3.connect("state.db", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, principal TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS msgs(principal TEXT, mid TEXT, hash TEXT, task_id TEXT, PRIMARY KEY(principal, mid));
CREATE TABLE IF NOT EXISTS cache(h TEXT PRIMARY KEY, body TEXT);
""")
dblock = threading.RLock()
key_locks = {}

def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def canon(o): return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
def sha(s): return hashlib.sha256(s.encode()).hexdigest()
def reply(obj, code=200): return JSONResponse(obj, status_code=code, media_type=MEDIA)
def err(code, name, msg="Request failed"): return reply({"error": {"code": name, "message": msg}}, code)

# ---------- gatekeeping ----------
def gate(req: Request, post=False):
    auth = req.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or not auth[7:].strip():
        return None, err(401, "UNAUTHENTICATED", "Authentication required")
    if req.headers.get("a2a-version") != "1.0":
        return None, err(400, "VERSION_NOT_SUPPORTED", "Unsupported version")
    if post and not req.headers.get("content-type", "").startswith(MEDIA):
        return None, err(415, "UNSUPPORTED_MEDIA_TYPE", "Use application/a2a+json")
    return sha(auth[7:].strip()), None

# ---------- storage ----------
def load(tid, principal):
    r = db.execute("SELECT body FROM tasks WHERE id=? AND principal=?", (tid, principal)).fetchone()
    return json.loads(r[0]) if r else None

def save(task, principal):
    db.execute("INSERT OR REPLACE INTO tasks VALUES(?,?,?)", (task["id"], principal, json.dumps(task)))

# ---------- public Agent Card ----------
@app.get("/.well-known/agent-card.json")
@app.get("/a2a/.well-known/agent-card.json")
def card():
    return reply({
        "name": "Invoice Action Agent",
        "description": "Reads invoice packages, proposes safe business actions with evidence, executes only approved ones.",
        "version": "1.0.0",
        "supportedInterfaces": [{"url": BASE_URL, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}],
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": [BATCH_T, RES_T],
        "defaultOutputModes": [PROP_T, REC_T],
        "skills": [{"id": "invoice_action_agent", "name": "Invoice action agent",
                    "description": "Choose settle/approve/hold/reject/exception for each invoice.",
                    "tags": ["invoice", "finance", "approval"]}],
    })

# ---------- AI layer ----------
PROMPT = """You are an accounts-payable analyst. For EACH package choose exactly one action:
settle_invoice, request_approval, hold_invoice, reject_duplicate, open_exception.

Decision rules, apply in this order:
1. The SAME invoice (same vendor AND same invoice number) was already paid -> reject_duplicate.
   A payment for a DIFFERENT invoice number is NOT a duplicate.
2. Vendor, amount, PO or receipt records materially conflict -> open_exception.
3. A named verification (bank details, tax ID, goods receipt, etc.) must finish before payment -> hold_invoice.
4. Amount exceeds the autonomous approval limit that applies to this case -> request_approval.
5. ONLY if none of the above apply -> settle_invoice.

How to read the documents:
- Find the ONE paragraph that states the controlling facts of THIS case. Return its three bracketed references,
  copied exactly, brackets included, all from that single paragraph.
- Ignore the cover sheet, archive examples, training material and decoys.
- Negated statements ("not a duplicate", "no hold required") mean that action does NOT apply.
- When unsure between settle_invoice and anything else, do NOT choose settle_invoice.

Also return vendorName, invoiceNumber, amountMinor (integer, smallest currency unit, e.g. 1234.50 -> 123450),
currency, and "reason": one sentence on why the evidence supports the action.
Reply ONLY JSON: {"decisions":[{"packageId":"","action":"","vendorName":"","invoiceNumber":"","amountMinor":0,
"currency":"","evidenceRefs":["","",""],"reason":""}]}"""

CHECKS = ["valid_and_reconciled", "within_authority", "no_pending_verification",
          "not_already_paid", "no_conflicting_records"]

VERIFY = """A colleague proposed settle_invoice for the invoice package below. Be a skeptical reviewer.
Read the paragraph that states the controlling facts of THIS case (ignore cover sheets, archive examples,
training decoys, and negated statements). Answer each check with true or false:
- valid_and_reconciled: invoice, PO and receipt records agree
- within_authority: amount is within the autonomous approval limit that applies
- no_pending_verification: nothing must be verified before payment
- not_already_paid: this exact invoice (same vendor AND invoice number) has not already been paid
- no_conflicting_records: no material conflict between records
Then give the correct action (settle_invoice, request_approval, hold_invoice, reject_duplicate or open_exception)
and the three exact bracketed references from the single controlling paragraph.
Reply ONLY JSON: {"checks":{"valid_and_reconciled":true,"within_authority":true,"no_pending_verification":true,
"not_already_paid":true,"no_conflicting_records":true},"action":"","evidenceRefs":["","",""],"reason":""}"""

def refs_ok(refs, pkg_text):
    return (isinstance(refs, list) and len(refs) == 3 and len(set(refs)) == 3
            and all(isinstance(r, str) and re.fullmatch(r"\[[^\[\]]+\]", r) and r in pkg_text for r in refs))

def valid(d, pkg_text):
    return (d.get("action") in ACTIONS and refs_ok(d.get("evidenceRefs"), pkg_text)
            and isinstance(d.get("amountMinor"), int)
            and isinstance(d.get("vendorName"), str) and d["vendorName"]
            and isinstance(d.get("invoiceNumber"), str) and d["invoiceNumber"])

def settle_confirmed(v, pkg_text):
    c = v.get("checks") or {}
    return (v.get("action") == "settle_invoice"
            and all(c.get(k) is True for k in CHECKS)
            and refs_ok(v.get("evidenceRefs"), pkg_text))

async def verify_settle(policy, p, sem):
    async with sem:
        try:
            return await call_llm(VERIFY, f"Policy revision: {policy}\nPackage:\n{canon(p)}", 15)
        except Exception:
            traceback.print_exc()
            return None

def log_line(obj):
    with open("decisions.jsonl", "a") as f:
        f.write(canon(obj) + "\n")

async def call_llm(system, user, timeout):
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(
            LLM_URL.format(model=LLM_MODEL),
            headers={"Content-Type": "application/json", "X-goog-api-key": LLM_KEY},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{
                    "role": "user", 
                    "parts": [{"text": user}]
                }],
                "generationConfig": {
                    "temperature": 0, 
                    "responseMimeType": "application/json"
                }
            })
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    s, e = text.find("{"), text.rfind("}")
    return json.loads(text[s:e + 1])

async def ask_llm(policy, pkgs):
    res = await call_llm(PROMPT, f"Policy revision: {policy}\nPackages:\n{canon(pkgs)}", 25)
    return res["decisions"]

async def ask_llm_safe(policy, pkgs):
    try:
        return [d for d in await ask_llm(policy, pkgs) if isinstance(d, dict)]
    except Exception:
        traceback.print_exc()
        return []

CACHE_VERSION = "v3"

def pkg_key(p):
    body = {k: v for k, v in p.items() if k != "packageId"}
    return sha(CACHE_VERSION + canon(body))

async def decide(policy, packages):
    out, todo, fresh = {}, [], {}
    for p in packages:
        r = db.execute("SELECT body FROM cache WHERE h=?", (pkg_key(p),)).fetchone()
        if r: out[p["packageId"]] = json.loads(r[0])
        else: todo.append(p)
    pending = list(todo)

    for attempt in range(2):
        if not todo: break
        got = {d.get("packageId"): d for d in await ask_llm_safe(policy, todo)}
        still = []
        for p in todo:
            d = got.get(p["packageId"])
            if d and valid(d, canon(p)): fresh[p["packageId"]] = d
            else: still.append(p)
        todo = still

    by_id = {p["packageId"]: p for p in pending}
    settles = [by_id[i] for i, d in fresh.items() if d["action"] == "settle_invoice"]
    sem = asyncio.Semaphore(4)
    verdicts = await asyncio.gather(*[verify_settle(policy, p, sem) for p in settles])
    verdict_of = {p["packageId"]: v for p, v in zip(settles, verdicts)}

    for p in pending:
        pid, text = p["packageId"], canon(p)
        d = fresh.get(pid)
        cacheable = True
        if d is None:
            d = {"action": "open_exception", "vendorName": "unknown", "invoiceNumber": "unknown",
                 "amountMinor": 0, "currency": "INR", "evidenceRefs": ["", "", ""], "reason": ""}
            cacheable = False
        elif d["action"] == "settle_invoice":
            v = verdict_of.get(pid)
            if v is None:
                d = {**d, "action": "open_exception"}
                cacheable = False
            elif settle_confirmed(v, text):
                d = {**d, "reason": v.get("reason") or d.get("reason", "")}
            elif v.get("action") in ACTIONS - {"settle_invoice"} and refs_ok(v.get("evidenceRefs"), text):
                d = {**d, "action": v["action"], "evidenceRefs": v["evidenceRefs"], "reason": v.get("reason", "")}
            else:
                d = {**d, "action": "open_exception"}
        out[pid] = d
        log_line({"packageId": pid, "first": (fresh.get(pid) or {}).get("action"),
                  "final": d["action"], "refs": d["evidenceRefs"], "verdict": verdict_of.get(pid)})
        if d["action"] == "settle_invoice":
            log_line({"SETTLED_PACKAGE": pid, "text": text})
        if cacheable:
            db.execute("INSERT OR REPLACE INTO cache VALUES(?,?)", (pkg_key(p), json.dumps(d)))
    db.commit()
    return out

def proposal(p, d, policy):
    a, r = d["action"], d["evidenceRefs"]
    rationale = (f"Proposing {a} for invoice {d['invoiceNumber']} from {d['vendorName']}. "
                 f"{d.get('reason', '')} Evidence {r[0]}, {r[1]} and {r[2]} in the controlling paragraph support {a}.")[:1500]
    return {"packageId": p["packageId"], "actionId": "act-" + uuid.uuid4().hex, "action": a,
            "facts": {"vendorName": d["vendorName"], "invoiceNumber": d["invoiceNumber"],
                      "amountMinor": d["amountMinor"], "currency": d["currency"]},
            "evidenceRefs": r, "rationale": rationale}

# ---------- message:send ----------
@app.post("/a2a/message:send")
@app.post("/message:send")
async def message_send(req: Request):
    principal, bad = gate(req, post=True)
    if bad: return bad
    try:
        body = await req.json(); msg = body["message"]; mid = msg["messageId"]
        assert isinstance(msg["parts"], list)
    except Exception:
        return err(400, "INVALID_REQUEST")
    h = sha(canon(msg))
    lock = key_locks.setdefault((principal, mid), asyncio.Lock())
    async with lock:
        with dblock:
            seen = db.execute("SELECT hash, task_id FROM msgs WHERE principal=? AND mid=?", (principal, mid
