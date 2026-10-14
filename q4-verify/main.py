import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

PUB = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA2okOHspNjgA+2rTLbeuY
cxiP/hG8C6Sb9iwg3yiLAA4HCnpITcbWCSelbvbYGuc3EbNy4xFyf5Cbj5DHJMID
EkryOgyd2giIIIBOUBj8S63uGcnRpOBh9NFatfNwheKuzsPuVNldu6A9cNteNpXc
WyJjG2axVfmq7i6SuKr1JoWYG7xTTAvKPujSl4OtsQfO3h5NepzdfXpr28oNnzfW
ed+zclR6BcmNNo/WVfJ4xyCLSf0BCOgdTgW6PdaChd1l9VDetJZVEgC5tkyvXsfI
SI6iyrYbKR0NEBSqq4XkadEjsCs4F1RncsS4LlgniT7GlkL9Mce3b0wGLs9/7ZIX
dQIDAQAB
-----END PUBLIC KEY-----"""
ISS = "https://idp.exam.local"
AUD = "tds-tvglga8g.apps.exam.local"

def bad():
    return JSONResponse({"valid": False}, status_code=401)

@app.post("/verify")
async def verify(request: Request):
    try:
        body = await request.json()
        token = body.get("token")
        c = jwt.decode(
            token, PUB, algorithms=["RS256"],
            audience=AUD, issuer=ISS,
            options={"require": ["exp", "iss", "aud"]},
        )
        return {"valid": True, "email": c.get("email"), "sub": c.get("sub"), "aud": c.get("aud")}
    except Exception:
        return bad()
