"""`python -m continuum.api` — the container entrypoint. Cloud Run supplies PORT."""

from __future__ import annotations

import os

import uvicorn

from continuum.api.app import create_app

if __name__ == "__main__":
    # Binding every interface is required inside the container; access control is in front
    # of the service (Cloud Run IAP), not in the bind address.
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
