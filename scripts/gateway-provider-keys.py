#!/usr/bin/env python3
"""Report model-gateway provider connections whose key is no longer accepted.

The estate runs one OmniRoute for every product, and nothing in the media stack
*needs* it. That is exactly how a refused key went unnoticed: the gateway kept
answering on ``:20128``, refused the work with a 401, and the failure surfaced
somewhere else entirely as *every model in the chain failed*. Nothing was
watching the gateway's own idea of its credentials, so this is that watch.

What counts, and why it is narrow
---------------------------------
The gateway stores the outcome of its last connection test against each
connection: ``errorCode`` and ``lastErrorType``. This script fails on the
*authentication* classes only — 401 and 403, and error types that mean the same
thing (``invalid_key``, ``unauthorized``, ...). A 429, a 402 or a 503 on the same
field is a quota, an exhausted balance or an upstream having a bad day; those are
routinely true on a free-tier gateway and reporting them would make this line an
alert nobody reads.

Only connections the deployment has switched *on* are judged, for the same
reason a stopped container is not drift: a connection somebody deliberately
turned off is a decision, not a fault. Switching one off is also the intended
answer for a provider that is out of credit and will not be topped up.

Exit codes (the ``verify-ldap.py`` convention, so the caller can tell the cases
apart rather than reading prose):

    0   every active connection's key was accepted
    1   at least one active connection refused its key
    2   the gateway could not be read at all (unset, unreachable, unparseable)

Usage
-----
    python3 scripts/gateway-provider-keys.py --check
    python3 scripts/gateway-provider-keys.py --url http://192.168.1.71:20128 \\
        --token "$OMNIROUTE_API_KEY"
    python3 scripts/gateway-provider-keys.py --from-file /tmp/providers.json

``--from-file`` reads the endpoint's JSON from disk instead of the network, which
is how the classification is tested without a gateway to point at.

``--token`` is optional. It is sent as a bearer when set, for a gateway that
gates its management API; on this estate's OmniRoute the connection list answers
identically without one, so a deployment does not need a credential here to be
watched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

# The HTTP statuses that mean "this credential is not accepted". Anything else on
# the connection's last error is a quota or an upstream, not a rejected key.
AUTH_STATUS_CODES = frozenset({401, 403})

# The same claim in `lastErrorType` form. The gateway is not consistent about
# which of the two it fills in, so both are read.
AUTH_ERROR_TYPES = frozenset(
    {
        "auth",
        "authentication",
        "authorization",
        "forbidden",
        "invalid_api_key",
        "invalid_key",
        "unauthorized",
        "unregistered",
    }
)

DEFAULT_TIMEOUT = 10


def status_code_of(connection: dict[str, Any]) -> int | None:
    """The HTTP status on the connection's last error, as an int.

    ``errorCode`` arrives in more than one shape: ``401`` as a number, and
    ``"429.0"`` as a string, because the column is a float somewhere behind the
    API. Both are read here, and anything that is not a status at all is `None`
    rather than an exception — a field this script did not expect is not a reason
    to stop reporting on the ones it did.
    """
    raw = connection.get("errorCode")
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        head = raw.strip().split(".", 1)[0].strip()
        if head.isdigit():
            return int(head)
    return None


def rejection_of(connection: dict[str, Any]) -> str | None:
    """Why this connection's key was refused, or `None` when it was not.

    Deliberately two tests rather than one: the status is the gateway's own
    verdict, and the error *type* is what it calls that verdict, and a gateway
    that fills in only one of them is still reporting a rejected key.
    """
    status = status_code_of(connection)
    if status in AUTH_STATUS_CODES:
        return f"HTTP {status}"

    error_type = connection.get("lastErrorType")
    if isinstance(error_type, str) and error_type.strip().lower() in AUTH_ERROR_TYPES:
        return f"last error type {error_type.strip()!r}"
    return None


def name_of(connection: dict[str, Any]) -> str:
    """`provider/name`, which is how a person finds the row in the UI."""
    provider = str(connection.get("provider") or "unknown")
    label = str(connection.get("name") or connection.get("id") or "?")
    return f"{provider}/{label}"


def is_active(connection: dict[str, Any]) -> bool:
    """True unless the deployment switched this connection off.

    A missing flag counts as active: an older gateway that does not publish
    `isActive` is not evidence that somebody turned the connection off, and
    silently skipping every row would be a check that always passes.
    """
    return connection.get("isActive") is not False


def inspect(connections: list[dict[str, Any]]) -> tuple[list[tuple[str, str]], int]:
    """(rejections, active_count). A rejection is `(name, reason)`."""
    rejections: list[tuple[str, str]] = []
    active = 0
    for connection in connections:
        if not isinstance(connection, dict):
            continue
        if not is_active(connection):
            continue
        active += 1
        reason = rejection_of(connection)
        if reason is not None:
            rejections.append((name_of(connection), reason))
    return rejections, active


def fetch(url: str, token: str | None, timeout: int) -> list[dict[str, Any]]:
    """The connection list, as the gateway reports it.

    The token is optional, and deliberately so: OmniRoute's `REQUIRE_API_KEY`
    gates `/v1/*`, while `/api/providers` answers an unauthenticated read with
    the same projection — `errorCode`, `lastError` and `lastErrorType` included,
    checked both ways against the live gateway. It is sent when configured for a
    gateway (or a proxy in front of one) that gates the management API too, and
    left out otherwise rather than putting a credential on the estate for
    nothing.
    """
    request = urllib.request.Request(url.rstrip("/") + "/api/providers")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    connections = payload.get("connections") if isinstance(payload, dict) else None
    if not isinstance(connections, list):
        raise ValueError("the gateway answered without a `connections` list")
    return [c for c in connections if isinstance(c, dict)]


def connections_from_file(path: str) -> list[dict[str, Any]]:
    with open(path, "rb") as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        return [c for c in payload if isinstance(c, dict)]
    connections = payload.get("connections") if isinstance(payload, dict) else None
    if not isinstance(connections, list):
        raise ValueError("that file holds no `connections` list")
    return [c for c in connections if isinstance(c, dict)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--url",
        default=os.environ.get("DRIFT_GATEWAY_URL", ""),
        help="gateway base URL (default: $DRIFT_GATEWAY_URL)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("DRIFT_GATEWAY_TOKEN", ""),
        help="gateway API key, sent as a bearer (default: $DRIFT_GATEWAY_TOKEN)",
    )
    parser.add_argument(
        "--from-file",
        default="",
        help="read the connection list from a JSON file instead of the gateway",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("DRIFT_GATEWAY_TIMEOUT", DEFAULT_TIMEOUT)),
        help=f"per-request timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when a key was refused (read-only; nothing is changed)",
    )
    args = parser.parse_args(argv)

    try:
        if args.from_file:
            connections = connections_from_file(args.from_file)
        elif args.url:
            connections = fetch(args.url, args.token or None, args.timeout)
        else:
            print(
                "gateway: no URL given - set DRIFT_GATEWAY_URL to watch the model gateway",
                file=sys.stderr,
            )
            return 2
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"gateway: could not read the connection list ({exc})", file=sys.stderr)
        return 2

    rejections, active = inspect(connections)

    for name, reason in rejections:
        print(f"REJECTED {name} refused its key ({reason}) - re-key it or switch it off")

    if rejections:
        print(
            f"gateway: {len(rejections)} of {active} active connection(s) refused their key",
            file=sys.stderr,
        )
        return 1

    if active == 0:
        print("model gateway: no active provider connection is configured")
        return 0

    print(f"model gateway: {active} active connection(s), every key accepted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
