from collections import defaultdict
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

KEY = "ak_xwne58rdg44v2qyqsxzg2o6u"
EMAIL = "23f1001297@ds.study.iitm.ac.in"

@app.post("/analytics")
def analytics(body: dict, x_api_key: str = Header(None)):
    if x_api_key != KEY:
        raise HTTPException(status_code=401, detail="unauthorized")
    events = body.get("events", [])
    totals = defaultdict(float)
    for e in events:
        amt = float(e.get("amount", 0))
        if amt > 0:
            totals[e.get("user")] += amt
    return {
        "email": EMAIL,
        "total_events": len(events),
        "unique_users": len({e.get("user") for e in events}),
        "revenue": round(sum(totals.values()), 2),
        "top_user": max(totals, key=totals.get) if totals else "",
    }
