import pytest

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
