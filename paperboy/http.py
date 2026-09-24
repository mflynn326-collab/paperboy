from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


USER_AGENT = "Paperboy/0.1 (local research literature monitor)"


def get_json(
    url: str,
    params: dict[str, str | int | None] | None = None,
    headers: dict[str, str] | None = None,
    retries: int = 5,
    pause: float = 1.0,
) -> dict:
    if params:
        clean = {key: value for key, value in params.items() if value is not None}
        url = f"{url}?{urlencode(clean)}"

    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    for attempt in range(retries):
        try:
            request = Request(url, headers=request_headers)
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code in {429, 500, 502, 503, 504} and attempt < retries - 1:
                time.sleep(pause * (2**attempt))
                continue
            raise
        except (URLError, ConnectionError, OSError):
            # Long backfills encounter occasional DNS, TLS, and socket resets.
            # These are transient transport failures, not failed source records.
            if attempt < retries - 1:
                time.sleep(pause * (2**attempt))
                continue
            raise

    raise RuntimeError("unreachable")


def download(url: str, headers: dict[str, str] | None = None) -> tuple[bytes, str | None]:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    with urlopen(request, timeout=120) as response:
        content_type = response.headers.get("Content-Type")
        return response.read(), content_type
