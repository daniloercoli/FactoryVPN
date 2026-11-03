"""Test-only SMTP capture. Production code never exposes a code-reading endpoint."""

messages = []


async def capture(recipient, code, purpose):
    messages.append({"recipient": recipient, "code": code, "purpose": purpose})


def last_code(recipient=None):
    return next(
        m["code"] for m in reversed(messages) if recipient is None or m["recipient"] == recipient
    )


async def complete(client, response, *, portal=False):
    if response.status_code == 200 and response.json().get("challenge_required"):
        prefix = "/api/v1/web/login" if portal else "/api/v1/auth/login"
        return await client.post(
            prefix + "/verify",
            json={"challenge_token": response.json()["challenge_token"], "code": last_code()},
        )
    return response
