"""Disposable pilot receiver. Bind only to loopback/private test networks."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
state = {"status": 503, "attempts": 0, "accepted": 0, "digests": 0, "duplicates": 0}
seen = set()


@app.post("/events")
async def receive(request: Request):
    state["attempts"] += 1
    if state["status"] != 200:
        return JSONResponse({"mock": "unavailable"}, status_code=state["status"])
    data = await request.json()
    key = request.headers.get("idempotency-key") or request.headers.get("x-opstrail-delivery-id")
    if key in seen:
        state["duplicates"] += 1
    elif key:
        seen.add(key)
        state["accepted"] += 1
        state["digests"] += data.get("schema") == "opstrail.digest.v1"
    return {"mock": "accepted"}


@app.get("/state")
async def stats():
    return state


@app.post("/mode/{status}")
async def mode(status: int):
    if status not in (200, 400, 429, 503):
        return JSONResponse({"error": "unsupported mock status"}, status_code=400)
    state["status"] = status
    return state
