"""Browser authorization UI connecting MCP OAuth to Vklass BankID."""

from __future__ import annotations

import asyncio
import html
from contextlib import suppress
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from vklass_mcp.config import Settings
from vklass_mcp.oauth import OAuthProvider, PendingAuthorization
from vklass_mcp.parsers import parse_account_identity
from vklass_mcp.registry import UserRegistry
from vklass_mcp.vklass.client import VklassClient

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; img-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'">
<title>Authorize Vklass MCP</title><style>
body{font:16px system-ui,sans-serif;max-width:680px;margin:2rem auto;padding:0 1rem;color:#17202a}
.card{border:1px solid #ccd1d1;border-radius:.6rem;padding:1.2rem;margin:1rem 0}button{font:inherit;padding:.7rem 1rem;margin:.3rem}
#qr{width:min(340px,90vw);display:none;image-rendering:pixelated}.error{color:#a00}code{word-break:break-word}
</style></head><body><h1>Authorize Vklass MCP</h1>
<div class="card"><p><strong>Client:</strong> CLIENT_NAME</p><p><strong>Return address:</strong> <code>REDIRECT_URI</code></p><p><strong>Requested access:</strong> SCOPES</p>
<p>You will authenticate directly with your own Vklass account using BankID. This client can only access data belonging to that account.</p></div>
<div class="card"><strong id="state">Ready</strong><p id="message">Start BankID to continue.</p><img id="qr" alt="BankID QR code"></div>
<button id="approve">Continue with BankID</button><button id="finish" style="display:none">Authorize this client</button><button id="deny">Deny</button>
<script>
const base=location.pathname;
async function post(action){const r=await fetch(base+'/'+action,{method:'POST',headers:{'X-Vklass-OAuth':'1'}});const j=await r.json();if(!r.ok)throw new Error(j.error||('HTTP '+r.status));return j}
async function refresh(){try{const r=await fetch(base+'/status',{cache:'no-store'});const s=await r.json();
document.querySelector('#state').textContent=s.status;document.querySelector('#message').textContent=s.message||'';
const qr=document.querySelector('#qr');if(s.qr_available){qr.src=base+'/qr.png?r='+s.qr_revision;qr.style.display='block'}else{qr.style.display='none'}
const finish=document.querySelector('#finish');if(s.status==='consent'){finish.style.display='inline-block';document.querySelector('#approve').style.display='none'}
if(s.redirect_url)location.replace(s.redirect_url)}catch(e){document.querySelector('#message').textContent=String(e)}}
document.querySelector('#approve').onclick=async()=>{try{await post('start');document.querySelector('#approve').disabled=true;await refresh()}catch(e){alert(e)}};
document.querySelector('#finish').onclick=async()=>{try{await post('approve');document.querySelector('#finish').disabled=true;await refresh()}catch(e){alert(e)}};
document.querySelector('#deny').onclick=async()=>{try{const s=await post('deny');location.replace(s.redirect_url)}catch(e){alert(e)}};
refresh();setInterval(refresh,1200);
</script></body></html>"""


def register_auth_routes(
    mcp: FastMCP[Any],
    provider: OAuthProvider,
    registry: UserRegistry,
    settings: Settings,
) -> None:
    prefix = "/auth/vklass/{flow_id}"
    bankid_slots = asyncio.Semaphore(settings.max_concurrent_bankid_flows)

    @mcp.custom_route(prefix, methods=["GET"], include_in_schema=False)
    async def authorization_page(request: Request) -> Response:
        flow = _flow(provider, request)
        if flow is None:
            return HTMLResponse("Authorization request expired", status_code=410)
        client_name = html.escape(flow.client.client_name or "Unnamed MCP client")
        scopes = html.escape(", ".join(flow.params.scopes or ["vklass.read"]))
        redirect_uri = html.escape(str(flow.params.redirect_uri))
        page = (
            _PAGE.replace("CLIENT_NAME", client_name)
            .replace("REDIRECT_URI", redirect_uri)
            .replace("SCOPES", scopes)
        )
        return HTMLResponse(page, headers=_security_headers())

    @mcp.custom_route(f"{prefix}/status", methods=["GET"], include_in_schema=False)
    async def authorization_status(request: Request) -> Response:
        flow = _flow(provider, request)
        if flow is None:
            return JSONResponse(
                {"status": "expired", "message": "Request expired"}, status_code=410
            )
        messages = {
            "pending": "Start BankID to continue.",
            "queued": "Waiting for an available BankID login slot.",
            "authenticating": "Scan the QR code with BankID and approve the login.",
            "consent": (
                f"Authenticated as {flow.authenticated_name}. Confirm access for this client."
            ),
            "complete": "Authorization complete. Returning to the MCP client.",
            "denied": "Access denied. Returning to the MCP client.",
            "error": flow.error or "Authentication failed.",
        }
        return JSONResponse(
            {
                "status": flow.status,
                "message": messages.get(flow.status, flow.status),
                "qr_available": flow.qr_seed is not None,
                "qr_revision": flow.qr_revision,
                "redirect_url": flow.redirect_url,
            },
            headers={"Cache-Control": "no-store"},
        )

    @mcp.custom_route(f"{prefix}/qr.png", methods=["GET"], include_in_schema=False)
    async def authorization_qr(request: Request) -> Response:
        flow = _flow(provider, request)
        if flow is None or not flow.qr_seed:
            return Response(status_code=404)
        import io

        import qrcode

        image = qrcode.make(flow.qr_seed)
        buffer = io.BytesIO()
        image.save(buffer)
        return Response(buffer.getvalue(), media_type="image/png", headers=_security_headers())

    @mcp.custom_route(f"{prefix}/start", methods=["POST"], include_in_schema=False)
    async def start_bankid(request: Request) -> Response:
        denied = _authorize_write(request, settings)
        if denied:
            return denied
        flow = _flow(provider, request)
        if flow is None:
            return JSONResponse({"error": "authorization request expired"}, status_code=410)
        if flow.login_task and not flow.login_task.done():
            return JSONResponse({"status": flow.status})
        if flow.status != "pending":
            return JSONResponse({"error": "authorization is no longer pending"}, status_code=409)
        flow.status = "queued"
        flow.login_task = asyncio.create_task(
            _run_bankid(provider, settings, flow, bankid_slots),
            name=f"vklass-oauth-{flow.flow_id[:8]}",
        )
        return JSONResponse({"status": "queued"})

    @mcp.custom_route(f"{prefix}/approve", methods=["POST"], include_in_schema=False)
    async def approve(request: Request) -> Response:
        denied = _authorize_write(request, settings)
        if denied:
            return denied
        flow = _flow(provider, request)
        if flow and flow.status == "complete" and flow.redirect_url:
            return JSONResponse({"status": "complete", "redirect_url": flow.redirect_url})
        if (
            flow is None
            or flow.status != "consent"
            or not flow.authenticated_subject
            or not flow.authenticated_name
            or not flow.pending_cookie
        ):
            return JSONResponse({"error": "authorization is not ready"}, status_code=409)
        subject = flow.authenticated_subject
        display_name = flow.authenticated_name
        cookie = flow.pending_cookie

        async def persist_session() -> None:
            await registry.adopt_authenticated_session(subject, display_name, cookie)

        try:
            redirect_url = await provider.complete_authorization(
                flow.flow_id, subject, persist_session
            )
        except Exception as error:
            provider.fail_authorization(
                flow.flow_id, f"{type(error).__name__}: authorization could not be completed"
            )
            return JSONResponse({"error": "authorization could not be completed"}, status_code=409)
        return JSONResponse({"status": "complete", "redirect_url": redirect_url})

    @mcp.custom_route(f"{prefix}/deny", methods=["POST"], include_in_schema=False)
    async def deny(request: Request) -> Response:
        denied = _authorize_write(request, settings)
        if denied:
            return denied
        flow = _flow(provider, request)
        if flow is None:
            return JSONResponse({"error": "authorization request expired"}, status_code=410)
        if flow.login_task and not flow.login_task.done():
            flow.login_task.cancel()
            with suppress(asyncio.CancelledError):
                await flow.login_task
        try:
            redirect_url = await provider.deny_authorization(flow.flow_id)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=409)
        return JSONResponse({"redirect_url": redirect_url})


async def _run_bankid(
    provider: OAuthProvider,
    settings: Settings,
    flow: PendingAuthorization,
    bankid_slots: asyncio.Semaphore,
) -> None:
    latest_cookie: str | None = None

    async def save_cookie(cookie: str) -> None:
        nonlocal latest_cookie
        latest_cookie = cookie

    async def show_qr(seed: str) -> None:
        flow.qr_seed = seed
        flow.qr_revision += 1

    client = VklassClient(settings.request_timeout_seconds, save_cookie)
    try:
        async with bankid_slots:
            async with flow.lock:
                if flow.status != "queued":
                    return
                flow.status = "authenticating"
            await client.open()
            await client.authenticate_bankid(settings.organisation_id, show_qr)
            identity_html = await client.account_page()
            vklass_user_id, display_name = parse_account_identity(identity_html)
            subject = provider.subject_for_vklass_user(vklass_user_id)
            if not latest_cookie:
                raise RuntimeError("Vklass did not issue a persistent session")
            async with flow.lock:
                if flow.status != "authenticating":
                    return
                flow.authenticated_subject = subject
                flow.authenticated_name = display_name
                flow.pending_cookie = latest_cookie
                flow.status = "consent"
    except asyncio.CancelledError:
        raise
    except Exception as error:
        provider.fail_authorization(
            flow.flow_id, f"{type(error).__name__}: Vklass authentication failed"
        )
    finally:
        flow.qr_seed = None
        flow.qr_revision += 1
        await client.close()


def _flow(provider: OAuthProvider, request: Request) -> PendingAuthorization | None:
    return provider.get_flow(str(request.path_params["flow_id"]))


def _authorize_write(request: Request, settings: Settings) -> Response | None:
    if request.headers.get("X-Vklass-OAuth") != "1":
        return JSONResponse({"error": "missing OAuth UI request header"}, status_code=403)
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != settings.public_base_url.rstrip("/"):
        return JSONResponse({"error": "origin rejected"}, status_code=403)
    return None


def _security_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    }
