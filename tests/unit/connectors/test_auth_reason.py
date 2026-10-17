"""Why credentials stopped working (ADR 0016 A6): every refusal maps to one of four classes, and the
class crosses Temporal inside the error's text (the workflow passes the text to the pause)."""

from __future__ import annotations

import pytest

from edisc_connectors_base.types import (
    AUTH_REASON_CLASSES,
    AuthenticationError,
    auth_reason_class,
    reason_class_in,
)


@pytest.mark.parametrize(
    ("reason", "cls"),
    [
        ("invalid_auth", "credentials_revoked"),
        ("token_revoked", "credentials_revoked"),
        ("account_inactive", "credentials_revoked"),
        ("invalid_grant", "credentials_revoked"),
        ("not_authed", "credentials_revoked"),
        ("token_expired", "credentials_expired"),
        ("AADSTS700082: The refresh token has expired", "credentials_expired"),
        ("consent_required", "consent_withdrawn"),
        ("AADSTS65001: The user or administrator has not consented", "consent_withdrawn"),
        ("unauthorized_client", "consent_withdrawn"),
        ("invalid_client", "platform_credential_expired"),
        (
            "AADSTS700027: Client assertion contains an invalid signature",
            "platform_credential_expired",
        ),
    ],
)
def test_each_refusal_has_a_class(reason: str, cls: str) -> None:
    assert auth_reason_class(reason) == cls
    exc = AuthenticationError(reason)
    assert exc.reason_class == cls and reason_class_in(str(exc)) == cls


def test_the_class_survives_the_text_and_an_explicit_class_wins() -> None:
    exc = AuthenticationError("token_revoked", reason_class="consent_withdrawn")
    assert str(exc) == "source rejected credentials (consent_withdrawn): token_revoked"
    assert reason_class_in(f"AuthenticationError: {exc}") == "consent_withdrawn"
    # an older worker's text carries no class: classified from the text itself
    assert reason_class_in("source rejected credentials: token_expired") == "credentials_expired"
    with pytest.raises(ValueError, match="unknown reason class"):
        AuthenticationError("x", reason_class="whatever")
    assert set(AUTH_REASON_CLASSES) == {"credentials_revoked", "credentials_expired",
                                        "consent_withdrawn", "platform_credential_expired"}  # fmt: skip
