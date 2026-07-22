"""FastAPI dashboard. Binds to loopback by default; expects a reverse proxy in
front to terminate TLS and authenticate (see Caddyfile). Optionally enforces a
shared X-Auth-Token header as defense-in-depth.
"""
import io
import os
import uuid
from contextlib import asynccontextmanager

import qrcode
import qrcode.image.svg
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth_claude
import config
import crypto
import db
import health
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


def _qr_response(url: str) -> Response:
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgImage)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml")


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
    return _render(
        request,
        "index.html",
        instances=manager.list_instances(),
        credentials=db.all_credentials(),
        secret_ready=crypto.available(),
        secret_message=crypto.key_message(),
        health=health.check(),
        claude=auth_claude.login_state(),
    )


@app.get("/partials/instances", response_class=HTMLResponse)
def instances_partial(request: Request):
    return _render(request, "_cards.html", instances=manager.list_instances())


@app.get("/partials/health", response_class=HTMLResponse)
def health_partial(request: Request):
    return _render(request, "_health.html", health=health.check())


# --------------------------------------------------------------------------- #
# Actions (htmx posts these and swaps in the refreshed card grid)
# --------------------------------------------------------------------------- #
@app.post("/instances", response_class=HTMLResponse)
def create_instance(
    request: Request,
    repo_url: str = Form(...),
    name: str = Form(""),
    credential_id: str = Form(""),
):
    error = None
    try:
        manager.spawn(repo_url, name, credential_id or None)
    except manager.SpawnError as exc:
        error = str(exc)
    return _render(request, "_cards.html", instances=manager.list_instances(), error=error)


@app.get("/instances/{iid}", response_class=HTMLResponse)
def instance_detail(request: Request, iid: str):
    item = manager.get_instance(iid)
    if item is None:
        raise HTTPException(status_code=404, detail="No such instance.")
    return _render(request, "instance.html", i=item, log=manager.instance_log(item))


@app.get("/instances/{iid}/log", response_class=HTMLResponse)
def instance_log(request: Request, iid: str):
    item = manager.get_instance(iid)
    if item is None:
        raise HTTPException(status_code=404, detail="No such instance.")
    return _render(request, "_log.html", i=item, log=manager.instance_log(item))


@app.post("/instances/{iid}/kill", response_class=HTMLResponse)
def kill_instance(request: Request, iid: str):
    manager.kill(iid)
    return _render(request, "_cards.html", instances=manager.list_instances())


@app.post("/instances/{iid}/rerun", response_class=HTMLResponse)
def rerun_instance(request: Request, iid: str):
    try:
        manager.rerun(iid)
    except manager.SpawnError as exc:
        return _render(request, "_error.html", error=str(exc))
    # Success: reload the detail page so it shows the running session + live log.
    return Response(status_code=204, headers={"HX-Redirect": f"/instances/{iid}"})


@app.post("/cleanup", response_class=HTMLResponse)
def cleanup_instance(request: Request, workdir: str = Form(...)):
    error = None
    try:
        manager.cleanup(workdir)
    except ValueError as exc:
        error = str(exc)
    return _render(request, "_cards.html", instances=manager.list_instances(), error=error)


# --------------------------------------------------------------------------- #
# Credentials (secrets encrypted at rest; the token is never rendered back)
# --------------------------------------------------------------------------- #
def _refresh() -> Response:
    """Tell htmx to reload the page so both the credential list and the spawn
    dropdown reflect the change."""
    return Response(status_code=204, headers={"HX-Refresh": "true"})


@app.post("/credentials")
def create_credential(
    name: str = Form(...),
    token: str = Form(...),
    provider: str = Form("github"),
    # Advanced (all optional): host is derived from each repo URL at spawn time;
    # username defaults to oauth2 (works for GitHub/GitLab tokens; set it for a
    # deploy token).
    host: str = Form(""),
    username: str = Form(""),
    git_name: str = Form(""),
    git_email: str = Form(""),
):
    if not crypto.available():
        raise HTTPException(
            status_code=400,
            detail=f"Cannot store credentials: {crypto.key_message()}",
        )
    provider = provider.strip().lower()
    if provider not in ("github", "gitlab"):
        provider = "github"
    try:
        secret_enc = crypto.encrypt(token)
    except Exception as exc:  # never surface a raw 500 for a config problem
        raise HTTPException(status_code=400, detail=f"Cannot encrypt token: {exc}")
    db.add_credential(
        uuid.uuid4().hex[:12],
        name.strip(),
        provider,
        host.strip(),
        username.strip() or "oauth2",
        secret_enc,
        git_name.strip() or None,
        git_email.strip() or None,
    )
    return _refresh()


@app.post("/credentials/{cid}/delete")
def delete_credential(cid: str):
    db.delete_credential(cid)
    return _refresh()


# --------------------------------------------------------------------------- #
# QR codes (served per-instance so card partials stay light)
# --------------------------------------------------------------------------- #
@app.get("/instances/{iid}/qr.svg")
def qr_svg(iid: str):
    row = db.get(iid)
    if row is None or not row["relay_url"]:
        raise HTTPException(status_code=404, detail="No relay URL yet.")
    return _qr_response(row["relay_url"])


# --------------------------------------------------------------------------- #
# Claude account (server-wide browser OAuth login)
# --------------------------------------------------------------------------- #
@app.get("/partials/claude", response_class=HTMLResponse)
def claude_partial(request: Request):
    return _render(request, "_claude_auth.html", claude=auth_claude.login_state())


@app.post("/claude/login", response_class=HTMLResponse)
def claude_login(request: Request):
    auth_claude.start_login()
    return _render(request, "_claude_auth.html", claude=auth_claude.login_state())


@app.post("/claude/code", response_class=HTMLResponse)
def claude_code(request: Request, code: str = Form(...)):
    ok = auth_claude.submit_code(code)
    error = None if ok else "Code not accepted (or still processing). Check it and try again."
    return _render(request, "_claude_auth.html", claude=auth_claude.login_state(error))


@app.post("/claude/logout", response_class=HTMLResponse)
def claude_logout(request: Request):
    auth_claude.logout()
    return _render(request, "_claude_auth.html", claude=auth_claude.login_state())


@app.get("/claude/login-qr.svg")
def claude_login_qr():
    url = auth_claude.login_url()
    if not url:
        raise HTTPException(status_code=404, detail="No login URL yet.")
    return _qr_response(url)


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="claude-fleet", description="Claude Fleet dashboard")
    parser.add_argument(
        "-c", "--config", default=None,
        help="Path to a .env config file (default: ~/.claude-fleet/.env, "
             "or the FLEET_CONFIG env var). Real environment variables still win.",
    )
    args = parser.parse_args()
    config.load(args.config)

    uvicorn.run(app, host=config.HOST, port=config.PORT)
