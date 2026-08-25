"""Verify the 422 handler logs field names and never echoes submitted values."""
import pytest


@pytest.mark.asyncio
async def test_422_strips_input_and_logs_fields(client, caplog):
    caplog.set_level("WARNING")
    secret = "s3cret-verifier-value"
    resp = await client.post(
        "/api/v1/auth/register",
        json={"private_number": "1234567890", "registration_token": "x",
              "login_password": secret},
    )
    assert resp.status_code == 422
    body = resp.text

    # The response must not echo the submitted verifier back.
    assert secret not in body, "422 body leaked a submitted password value"
    assert "input" not in resp.json()["detail"][0]

    # Shape still usable by the client formatter.
    err = resp.json()["detail"][0]
    assert err["loc"] == ["body", "delete_password"]
    assert err["type"] == "missing"

    # It logged the field name, and NOT the value.
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "[422]" in logged
    assert "delete_password" in logged
    assert secret not in logged, "log leaked a submitted password value"
