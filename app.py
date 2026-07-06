"""FastAPI dashboard. Binds to loopback by default; expects a reverse proxy in
front to terminate TLS and authenticate (see Caddyfile). Optionally enforces a
shared X-Auth-Token header as defense-in-depth.
"""
import io
import os
from contextlib import asynccontextmanager

import qrcode
import qrcode.image.svg
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
import db
import manager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    yield


app = FastAPI(title="Claude Fleet", lifespan=lifespan)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


def _render(request: Request, name: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context)


@app.middleware("http")
async def _auth(request: Request, call_next):
    if config.AUTH_TOKEN and request.headers.get("X-Auth-Token") != config.AUTH_TOKEN:
        return Response("Forbidden", status_code=403)
    return await call_next(request)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return _render(request, "index.html", instances=manager.list_instances())


@app.get("/partials/instances", response_class=HTMLResponse)
def instances_partial(request: Request):
    return _render(request, "_cards.html", instances=manager.list_instances())


# --------------------------------------------------------------------------- #
# Actions (htmx posts these and swaps in the refreshed card grid)
# --------------------------------------------------------------------------- #
@app.post("/instances", response_class=HTMLResponse)
def create_instance(request: Request, repo_url: str = Form(...), name: str = Form("")):
    error = None
    try:
        manager.spawn(repo_url, name)
    except manager.SpawnError as exc:
        error = str(exc)
    return _render(request, "_cards.html", instances=manager.list_instances(), error=error)


@app.post("/instances/{iid}/kill", response_class=HTMLResponse)
def kill_instance(request: Request, iid: str):
    manager.kill(iid)
    return _render(request, "_cards.html", instances=manager.list_instances())


@app.post("/cleanup", response_class=HTMLResponse)
def cleanup_instance(request: Request, workdir: str = Form(...)):
    error = None
    try:
        manager.cleanup(workdir)
    except ValueError as exc:
        error = str(exc)
    return _render(request, "_cards.html", instances=manager.list_instances(), error=error)


# --------------------------------------------------------------------------- #
# QR codes (served per-instance so card partials stay light)
# --------------------------------------------------------------------------- #
@app.get("/instances/{iid}/qr.svg")
def qr_svg(iid: str):
    row = db.get(iid)
    if row is None or not row["relay_url"]:
        raise HTTPException(status_code=404, detail="No relay URL yet.")
    img = qrcode.make(row["relay_url"], image_factory=qrcode.image.svg.SvgImage)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
