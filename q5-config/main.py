from typing import List
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def coerce(k, v):
    if k in ("port", "workers"):
        return int(v)
    if k == "debug":
        return str(v).strip().lower() in ("true", "1", "yes", "on")
    return str(v)

@app.get("/effective-config")
def cfg(set: List[str] = Query(default=[])):
    c = {"port": 8000, "workers": 1, "debug": False, "log_level": "info", "api_key": "default-secret-000"}
    c.update({"workers": 9, "debug": True, "api_key": "key-h5lg20ugdx"})
    c.update({"workers": 12, "log_level": "warning", "api_key": "key-nemw4jf18l"})
    c.update({"port": 8650, "log_level": "error", "api_key": "key-dxst08ss87"})
    for s in set:
        if "=" in s:
            k, v = s.split("=", 1)
            c[k.strip()] = v
    out = {}
    for k, v in c.items():
        try:
            out[k] = coerce(k, v)
        except ValueError:
            out[k] = v
    out["api_key"] = "****"
    return out
