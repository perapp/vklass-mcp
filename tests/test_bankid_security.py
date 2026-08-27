import pytest
from starlette.requests import Request

from vklass_mcp.auth_ui import _authorize_write
from vklass_mcp.config import Settings
from vklass_mcp.vklass.auth.goteborg import (
    BankIDLoginError,
    _require_host,
    _validated_redirect_url,
)


def test_bankid_form_host_requires_exact_https_host() -> None:
    _require_host("https://authpub.goteborg.se/path", "authpub.goteborg.se")
    with pytest.raises(BankIDLoginError):
        _require_host("http://authpub.goteborg.se/path", "authpub.goteborg.se")
    with pytest.raises(BankIDLoginError):
        _require_host("https://authpub.goteborg.se.evil.invalid/", "authpub.goteborg.se")


def test_oauth_ui_mutations_require_same_origin_browser_request() -> None:
    settings = Settings(
        public_base_url="https://vklass.example.com",
        state_key="state-key-for-tests",
        _env_file=None,
    )
    missing_origin = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/vklass/flow/approve",
            "headers": [(b"x-vklass-oauth", b"1")],
        }
    )
    rejected = _authorize_write(missing_origin, settings)
    assert rejected is not None
    assert rejected.status_code == 403

    same_origin = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/vklass/flow/approve",
            "headers": [
                (b"x-vklass-oauth", b"1"),
                (b"origin", b"https://vklass.example.com"),
            ],
        }
    )
    assert _authorize_write(same_origin, settings) is None


def test_signed_saml_redirect_is_not_requoted() -> None:
    location = (
        "https://authpub.goteborg.se/idp/login?"
        "SAMLRequest=abc%2Fdef%2Bghi%3D&Signature=sig%2Bvalue%3D"
    )

    redirect = _validated_redirect_url(
        "https://auth.vklass.se/saml/initiate",
        location,
        {"authpub.goteborg.se"},
    )

    assert str(redirect) == location
    with pytest.raises(BankIDLoginError):
        _validated_redirect_url(
            "https://auth.vklass.se/saml/initiate",
            "https://authpub.goteborg.se.evil.invalid/idp/login",
            {"authpub.goteborg.se"},
        )
