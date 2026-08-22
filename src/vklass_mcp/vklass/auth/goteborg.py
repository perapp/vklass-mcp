"""Interactive Göteborg Stad guardian login using BankID QR.

Adapted from ``Kaptensanders/vklass`` (MIT), commit
``aedf38bc970c37faa7b7aa7f0718b91ffdc36656``. The flow remains human-driven:
the caller displays each rotating QR seed and the guardian approves it in BankID.
Sensitive SAML values, cookies and QR seeds are deliberately never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from html import unescape
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse

import aiohttp
from bs4 import BeautifulSoup
from yarl import URL

QRCallback = Callable[[str], Awaitable[None]]
_LOG = logging.getLogger(__name__)

_AUTH_VKLASS = "https://auth.vklass.se"
_AUTH_GOTEBORG = "https://authpub.goteborg.se"
_EID_CONNECT = "https://eid-connect.funktionstjanster.se"
_GOTEBORG_IDP = "https://authpub.goteborg.se/idp/sps/idppub/saml20"


class BankIDLoginError(RuntimeError):
    """Raised when the interactive Göteborg BankID flow cannot complete."""


async def authenticate(
    session: aiohttp.ClientSession,
    qr_callback: QRCallback,
    organisation_id: int = 190,
) -> None:
    """Complete the Göteborg SAML/BankID flow in ``session``.

    Organisation 190 is Göteborg GSF and 189 is Göteborg UBF. Success means the
    Vklass authentication cookie should be present in the supplied session.
    """

    if organisation_id not in (189, 190):
        raise ValueError("the Göteborg adapter only supports organisation 189 or 190")
    state: dict[str, Any] = {"organisation_id": str(organisation_id)}
    await _bootstrap(session, state)
    await _start_qr_flow(session, state)
    await _init_bankid(session, state)
    await _poll_bankid(session, state, qr_callback)
    await _handover(session, state)


async def _bootstrap(session: aiohttp.ClientSession, state: dict[str, Any]) -> None:
    html, final_url = await _get_follow_validated(
        session,
        f"{_AUTH_VKLASS}/saml/initiate",
        params={
            "idp": _GOTEBORG_IDP,
            "org": state["organisation_id"],
            "returnUrl": "http://custodian.vklass.se/",
        },
        allowed_hosts={"auth.vklass.se", "authpub.goteborg.se"},
    )
    parsed_final = urlparse(final_url)
    if (
        parsed_final.hostname != "authpub.goteborg.se"
        or "/sp/sps/eidpub/saml20/logininitial" not in parsed_final.path
    ):
        raise BankIDLoginError("Göteborg bootstrap landed on an unexpected endpoint")

    target = parse_qs(urlparse(final_url).query).get("Target", [None])[0]
    soup = BeautifulSoup(html, "html.parser")
    saml_url = None
    for button in soup.find_all("button", attrs={"name": "ITFIM_WAYF_IDP"}):
        value = str(button.get("value", ""))
        if "bankid" in button.get_text(" ").lower() or "bankid" in value.lower():
            saml_url = value
            break
    if not target or not saml_url:
        raise BankIDLoginError("Göteborg BankID bootstrap fields were not found")
    state["target"] = unquote(target)
    state["saml_url"] = saml_url


async def _start_qr_flow(session: aiohttp.ClientSession, state: dict[str, Any]) -> None:
    async with session.get(
        f"{_AUTH_GOTEBORG}/sp/sps/eidpub/saml20/logininitial",
        params={
            "ResponseBinding": "HTTPPost",
            "RequestBinding": "HTTPPost",
            "NameIdFormat": "Transient",
            "ITFIM_WAYF_IDP": state["saml_url"],
            "Target": state["target"],
        },
        allow_redirects=False,
    ) as response:
        html = await _expect(response, 200, final_host="authpub.goteborg.se")

    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    action = str(form.get("action", "")) if form else ""
    relay = _input_value(form, "RelayState")
    request = _input_value(form, "SAMLRequest")
    action = urljoin(_AUTH_GOTEBORG, unescape(action))
    _require_host(action, "eid-connect.funktionstjanster.se")
    if not relay or not request:
        raise BankIDLoginError("SAML request fields for BankID were not found")

    _, final_url = await _post_form_follow_get(
        session,
        action,
        headers={
            "Origin": _AUTH_GOTEBORG,
            "Referer": f"{_AUTH_GOTEBORG}/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.7",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"RelayState": relay, "SAMLRequest": request},
        allowed_host="eid-connect.funktionstjanster.se",
    )
    if "/web/app/v2/" not in urlparse(final_url).path:
        raise BankIDLoginError("BankID flow landed on an unexpected path")

    parsed = urlparse(final_url)
    aid = parse_qs(parsed.query).get("aid", [None])[0]
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 5 or not aid:
        raise BankIDLoginError("unexpected BankID application URL")
    state["aid"] = aid
    state["sp_id"] = parts[-1]
    state["app_url"] = urlunparse(parsed._replace(query=""))


async def _init_bankid(session: aiohttp.ClientSession, state: dict[str, Any]) -> None:
    headers = _eid_headers(state, json_content=True)
    async with session.get(
        f"{_EID_CONNECT}/web/res/api/methods",
        headers=headers,
        params={"lang": "sv", "aid": state["aid"], "spId": state["sp_id"]},
        allow_redirects=False,
    ) as response:
        body = await _expect(response, 200, final_host="eid-connect.funktionstjanster.se")
    try:
        methods = json.loads(body)
    except json.JSONDecodeError as error:
        raise BankIDLoginError("BankID methods response was not JSON") from error
    method_id = None
    if isinstance(methods, list):
        candidates = [item for item in methods if isinstance(item, dict)]
        bankid_candidates = [
            item for item in candidates if "bankid" in json.dumps(item, ensure_ascii=False).lower()
        ]
        selected = (
            (bankid_candidates or candidates)[0] if (bankid_candidates or candidates) else None
        )
        method_id = selected.get("id") if selected else None
    if not method_id:
        raise BankIDLoginError("BankID method was not offered by Göteborg e-ID")
    state["method_id"] = method_id

    async with session.post(
        f"{_EID_CONNECT}/id/bankid/auth",
        headers=headers,
        params={"lang": "sv", "aid": state["aid"], "id": method_id},
        data="",
        allow_redirects=False,
    ) as response:
        token = (
            await _expect(response, 200, final_host="eid-connect.funktionstjanster.se")
        ).strip()
    if not token or token.lower().startswith("<html"):
        raise BankIDLoginError("Göteborg e-ID did not start a BankID order")


async def _poll_bankid(
    session: aiohttp.ClientSession,
    state: dict[str, Any],
    qr_callback: QRCallback,
) -> None:
    headers = _eid_headers(state)
    failures = 0
    for _ in range(90):
        await asyncio.sleep(1)
        try:
            async with session.get(
                f"{_EID_CONNECT}/id/bankid/status",
                params={"aid": state["aid"]},
                headers=headers,
                allow_redirects=False,
            ) as response:
                status_body = await _expect(
                    response, 200, final_host="eid-connect.funktionstjanster.se"
                )
            status_data = json.loads(status_body)
        except (aiohttp.ClientError, BankIDLoginError, json.JSONDecodeError):
            failures += 1
            if failures < 10:
                continue
            raise BankIDLoginError("repeated BankID status failures") from None

        status = status_data.get("status")
        if status == "complete":
            _LOG.info("BankID approval completed")
            return
        if status != "pending":
            hint = status_data.get("hintCode") or status_data.get("substatus") or "unknown"
            raise BankIDLoginError(f"BankID order ended without approval ({hint})")

        async with session.get(
            f"{_EID_CONNECT}/id/bankid/qr",
            params={"aid": state["aid"]},
            headers=headers,
            allow_redirects=False,
        ) as response:
            qr_seed = (
                await _expect(response, 200, final_host="eid-connect.funktionstjanster.se")
            ).strip()
        if not qr_seed.startswith("bankid."):
            raise BankIDLoginError("BankID QR response had an unexpected format")
        await qr_callback(qr_seed)
    raise TimeoutError("BankID approval timed out after 90 seconds")


async def _handover(session: aiohttp.ClientSession, state: dict[str, Any]) -> None:
    headers = _eid_headers(state)
    async with session.get(
        f"{_EID_CONNECT}/id/finish",
        headers=headers,
        params={"aid": state["aid"]},
        allow_redirects=False,
    ) as response:
        html = await _expect(response, 200, final_host="eid-connect.funktionstjanster.se")

    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    action = unescape(str(form.get("action", ""))) if form else ""
    action = urljoin(_AUTH_GOTEBORG, action)
    _require_host(action, "authpub.goteborg.se")
    relay = _input_value(form, "RelayState")
    saml_response = _input_value(form, "SAMLResponse")
    if not relay or not saml_response:
        raise BankIDLoginError("BankID finish response did not contain SAML fields")

    html, final_url = await _post_form_follow_get(
        session,
        action,
        headers={
            **headers,
            "Referer": _EID_CONNECT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"RelayState": relay, "SAMLResponse": saml_response},
        allowed_host="authpub.goteborg.se",
    )
    if "/idp/sps/auth" not in urlparse(final_url).path:
        raise BankIDLoginError("Göteborg SAML handover landed on an unexpected path")

    soup = BeautifulSoup(html, "html.parser")
    saml_response = _input_value(soup.find("form"), "SAMLResponse")
    if not saml_response:
        raise BankIDLoginError("Göteborg SAML handover response was incomplete")

    async with session.post(
        f"{_AUTH_VKLASS}/saml/assertion",
        headers={
            "Origin": _AUTH_GOTEBORG,
            "Referer": f"{_AUTH_GOTEBORG}/",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"RelayState": "", "SAMLResponse": saml_response},
        allow_redirects=False,
    ) as response:
        await _expect(response, 302, final_host="auth.vklass.se", read_body=False)


async def _get_follow_validated(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict[str, str],
    allowed_hosts: set[str],
    maximum_hops: int = 10,
) -> tuple[str, str]:
    current_url: str | URL = url
    current_params: dict[str, str] | None = params
    for _ in range(maximum_hops + 1):
        parsed = urlparse(str(current_url))
        if not _is_https_host(parsed, allowed_hosts):
            raise BankIDLoginError("authentication redirected to an unexpected host")
        async with session.get(
            current_url, params=current_params, allow_redirects=False
        ) as response:
            body = await response.text()
            status = response.status
            response_url = str(response.url)
            location = response.headers.get("Location")
        if status == 200:
            return body, response_url
        if status not in (301, 302, 303, 307, 308) or not location:
            raise BankIDLoginError(f"authentication service returned HTTP {status}")
        current_url = _validated_redirect_url(response_url, location, allowed_hosts)
        current_params = None
    raise BankIDLoginError("authentication exceeded the redirect limit")


async def _post_form_follow_get(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict[str, str],
    data: dict[str, str],
    allowed_host: str,
    maximum_hops: int = 10,
) -> tuple[str, str]:
    """POST sensitive form data once, then follow only validated GET redirects."""

    _require_host(url, allowed_host)
    async with session.post(url, headers=headers, data=data, allow_redirects=False) as response:
        body = await response.text()
        status = response.status
        current_url = str(response.url)
        location = response.headers.get("Location")
    for _ in range(maximum_hops + 1):
        if status == 200:
            return body, current_url
        if status in (307, 308):
            raise BankIDLoginError("authentication attempted to replay sensitive POST data")
        if status not in (301, 302, 303) or not location:
            raise BankIDLoginError(f"authentication service returned HTTP {status}")
        next_url = _validated_redirect_url(current_url, location, {allowed_host})
        async with session.get(next_url, allow_redirects=False) as response:
            body = await response.text()
            status = response.status
            current_url = str(response.url)
            location = response.headers.get("Location")
    raise BankIDLoginError("authentication exceeded the redirect limit")


def _eid_headers(state: dict[str, Any], *, json_content: bool = False) -> dict[str, str]:
    headers = {
        "Accept": "*/*",
        "Origin": _EID_CONNECT,
        "Referer": f"{state['app_url']}?lang=sv&aid={state['aid']}",
    }
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


async def _expect(
    response: aiohttp.ClientResponse,
    status: int,
    *,
    final_host: str,
    final_path_contains: str | None = None,
    read_body: bool = True,
) -> str:
    if response.status != status:
        await response.read()
        raise BankIDLoginError(f"authentication service returned HTTP {response.status}")
    parsed = urlparse(str(response.url))
    if parsed.hostname != final_host:
        await response.read()
        raise BankIDLoginError("authentication redirected to an unexpected host")
    if final_path_contains and final_path_contains not in parsed.path:
        await response.read()
        raise BankIDLoginError("authentication redirected to an unexpected path")
    return await response.text() if read_body else ""


def _input_value(form: Any, name: str) -> str | None:
    if form is None:
        return None
    element = form.find("input", {"name": name})
    value = element.get("value") if element else None
    return str(value) if value else None


def _require_host(url: str, expected: str) -> None:
    if not _is_https_host(urlparse(url), {expected}):
        raise BankIDLoginError("authentication form targeted an unexpected host")


def _validated_redirect_url(base_url: str, location: str, allowed_hosts: set[str]) -> URL:
    """Validate a redirect while preserving signed SAML query bytes exactly."""

    redirect_url = urljoin(base_url, location)
    if not _is_https_host(urlparse(redirect_url), allowed_hosts):
        raise BankIDLoginError("authentication redirected to an unexpected host")
    return URL(redirect_url, encoded=True)


def _is_https_host(parsed: Any, expected_hosts: set[str]) -> bool:
    try:
        port = parsed.port
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname in expected_hosts and port in (None, 443)
