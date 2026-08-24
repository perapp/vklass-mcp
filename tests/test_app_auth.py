import base64
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from vklass_mcp.app import Application
from vklass_mcp.config import Settings


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


@pytest.mark.asyncio
async def test_mcp_oauth_discovery_and_subject_token(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(
        data_dir=tmp_path,
        public_base_url="http://127.0.0.1:8000",
        state_key="state-encryption-key-for-tests",
        max_pending_authorizations_per_client=1,
        _env_file=None,
    )
    app = Application(settings)
    subject = app.provider.subject_for_vklass_user("raw-vklass-user-id")
    assert subject == app.provider.subject_for_vklass_user("raw-vklass-user-id")
    assert "raw-vklass-user-id" not in subject
    await app.provider.open()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8000"
        ) as client:
            unauthorized = await client.post("/mcp", json={})
            assert unauthorized.status_code == 401
            assert "resource_metadata=" in unauthorized.headers["www-authenticate"]

            auth_metadata = await client.get("/.well-known/oauth-authorization-server")
            assert auth_metadata.status_code == 200
            assert auth_metadata.json()["registration_endpoint"].endswith("/register")
            assert "none" in auth_metadata.json()["token_endpoint_auth_methods_supported"]
            assert "none" in auth_metadata.json()["revocation_endpoint_auth_methods_supported"]

            resource_metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
            assert resource_metadata.status_code == 200
            assert resource_metadata.json()["resource"] == settings.resource_server_url

            rejected_registration = await client.post(
                "/register",
                json={
                    "redirect_uris": ["http://not-loopback.example/callback"],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                },
            )
            assert rejected_registration.status_code == 400
            assert rejected_registration.json()["error"] == "invalid_redirect_uri"

            registration = await client.post(
                "/register",
                json={
                    "redirect_uris": ["http://127.0.0.1:8765/callback"],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "client_name": "Test MCP client",
                    "scope": "vklass.read",
                },
            )
            assert registration.status_code == 201
            client_id = registration.json()["client_id"]

            verifier = "a" * 64
            missing_resource = await client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": "http://127.0.0.1:8765/callback",
                    "response_type": "code",
                    "code_challenge": _challenge(verifier),
                    "code_challenge_method": "S256",
                    "scope": "vklass.read",
                },
            )
            assert missing_resource.status_code == 302
            assert parse_qs(urlparse(missing_resource.headers["location"]).query)["error"] == [
                "invalid_request"
            ]

            authorization = await client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": "http://127.0.0.1:8765/callback",
                    "response_type": "code",
                    "code_challenge": _challenge(verifier),
                    "code_challenge_method": "S256",
                    "scope": "vklass.read",
                    "resource": settings.resource_server_url,
                    "state": "client-state",
                },
            )
            assert authorization.status_code == 302
            flow_id = authorization.headers["location"].rsplit("/", 1)[-1]
            flow = app.provider.get_flow(flow_id)
            assert flow is not None
            flow.status = "consent"
            flow.authenticated_subject = "vklass-user-42"
            callback = await app.provider.complete_authorization(flow_id, "vklass-user-42")
            query = parse_qs(urlparse(callback).query)

            mismatched_token = await client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": query["code"][0],
                    "redirect_uri": "http://127.0.0.1:8765/callback",
                    "client_id": client_id,
                    "code_verifier": verifier,
                    "resource": "http://127.0.0.1:8000/other",
                },
            )
            assert mismatched_token.status_code == 400

            token_response = await client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": query["code"][0],
                    "redirect_uri": "http://127.0.0.1:8765/callback",
                    "client_id": client_id,
                    "code_verifier": verifier,
                    "resource": settings.resource_server_url,
                },
            )
            assert token_response.status_code == 200
            token = token_response.json()
            verified = await app.provider.load_access_token(token["access_token"])
            assert verified is not None
            assert verified.subject == "vklass-user-42"
            assert verified.scopes == ["vklass.read"]

            async with (
                app.mcp.session_manager.run(),
                httpx.AsyncClient(
                    transport=transport,
                    headers={"Authorization": f"Bearer {token['access_token']}"},
                ) as mcp_http,
                streamable_http_client("http://127.0.0.1:8000/mcp", http_client=mcp_http) as (
                    read_stream,
                    write_stream,
                    _,
                ),
                ClientSession(read_stream, write_stream) as session,
            ):
                initialized = await session.initialize()
                assert initialized.instructions is not None
                assert "always call the relevant list/get tool" in initialized.instructions
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                sync_description = tools["vklass_sync_now"].description or ""
                care_description = tools["vklass_list_care_schedule"].description or ""
                assert "returns status/counts, not records" in sync_description
                assert "Monday-to-Sunday" in care_description
                care_schema = tools["vklass_list_care_schedule"].inputSchema
                assert "Inclusive start date" in care_schema["properties"]["start"]["description"]
                assert "Inclusive end date" in care_schema["properties"]["end"]["description"]
                invalid = await session.call_tool("vklass_list_care_schedule", {"limit": 0})
                assert invalid.isError
                assert any(
                    "MCP tool call failed name=vklass_list_care_schedule " in record.message
                    and "argument_keys=limit" in record.message
                    and "error=ToolError" in record.message
                    for record in caplog.records
                )
                result = await session.call_tool("vklass_capabilities", {})
                assert not result.isError

            refreshed_response = await client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": token["refresh_token"],
                    "client_id": client_id,
                    "resource": settings.resource_server_url,
                },
            )
            assert refreshed_response.status_code == 200
            refreshed = refreshed_response.json()
            assert refreshed["refresh_token"] != token["refresh_token"]
            assert await app.provider.load_access_token(token["access_token"]) is None

            replay = await client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": token["refresh_token"],
                    "client_id": client_id,
                    "resource": settings.resource_server_url,
                },
            )
            assert replay.status_code == 400
            assert replay.json()["error"] == "invalid_grant"
            assert await app.provider.load_access_token(refreshed["access_token"]) is None

            revoked = await client.post(
                "/revoke",
                data={
                    "token": refreshed["access_token"],
                    "token_type_hint": "access_token",
                    "client_id": client_id,
                    "client_secret": "",
                },
            )
            assert revoked.status_code == 200
            assert await app.provider.load_access_token(refreshed["access_token"]) is None

            denied_authorization = await client.get(
                "/authorize",
                params={
                    "client_id": client_id,
                    "redirect_uri": "http://127.0.0.1:8765/callback",
                    "response_type": "code",
                    "code_challenge": _challenge(verifier),
                    "code_challenge_method": "S256",
                    "scope": "vklass.read",
                    "resource": settings.resource_server_url,
                },
            )
            denied_flow_id = denied_authorization.headers["location"].rsplit("/", 1)[-1]
            denied_flow = app.provider.get_flow(denied_flow_id)
            assert denied_flow is not None
            denied_flow.status = "consent"
            denied_flow.authenticated_subject = "vklass-user-42"
            denied_callback = await app.provider.complete_authorization(
                denied_flow_id, "vklass-user-42"
            )
            denied_code = parse_qs(urlparse(denied_callback).query)["code"][0]
            await app.provider.deny_authorization(denied_flow_id)
            denied_exchange = await client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": denied_code,
                    "redirect_uri": "http://127.0.0.1:8765/callback",
                    "client_id": client_id,
                    "code_verifier": verifier,
                    "resource": settings.resource_server_url,
                },
            )
            assert denied_exchange.status_code == 400

            database_text = (tmp_path / "oauth.db").read_bytes().decode("latin-1")
            assert token["access_token"] not in database_text
            assert token["refresh_token"] not in database_text
            assert refreshed["access_token"] not in database_text
            assert refreshed["refresh_token"] not in database_text
            assert query["code"][0] not in database_text
            assert "Test MCP client" not in database_text
    finally:
        await app.registry.stop()
        await app.provider.close()
