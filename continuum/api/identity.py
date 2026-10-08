"""Who is the reviewer.

The console has no login of its own. In the cloud the service sits behind Identity-Aware Proxy
on Cloud Run: only Google identities granted access reach the container, and IAP adds the
caller's email as `X-Goog-Authenticated-User-Email: accounts.google.com:<email>`. That email is
recorded on every review decision.

Outside the cloud (local, CI) there is no proxy, so a fixed development identity is used. In
any other environment a request without the header is refused rather than given a default.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from continuum.config.settings import get_settings

IAP_EMAIL_HEADER = "x-goog-authenticated-user-email"
LOCAL_REVIEWER = "local-dev"


def reviewer_identity(request: Request) -> str:
    raw = request.headers.get(IAP_EMAIL_HEADER, "")
    email = raw.rsplit(":", 1)[-1].strip()
    if email:
        return email
    if get_settings().environment in ("local", "ci"):
        return LOCAL_REVIEWER
    raise HTTPException(status_code=401, detail="no authenticated reviewer identity")
