import time, uuid
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
EMAIL = "23f1001297@ds.study.iitm.ac.in"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://dash-n2jkpz.example.com"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-Process-Time"],
)

@app.middleware("http")
async def ctx(request: Request, call_next):
    t = time.perf_counter()
    r = await call_next(request)
    r.headers["X-Request-ID"] = str(uuid.uuid4())
    r.headers["X-Process-Time"] = f"{time.perf_counter() - t:.6f}"
    return r

@app.get("/stats")
def stats(values: str = ""):
    v = [int(x) for x in values.split(",") if x.strip()]
    n = len(v)
    return {
        "email": EMAIL,
        "count": n,
        "sum": sum(v),
        "min": min(v, default=0),
        "max": max(v, default=0),
        "mean": sum(v) / n if n else 0,
    }
