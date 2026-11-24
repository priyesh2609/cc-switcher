"""HTTP transport for CCS requests without a User-Agent header."""

from __future__ import annotations

import urllib.request


def open_without_user_agent(request: urllib.request.Request, *, timeout: float):
    """Open a request without urllib's default Python-version User-Agent.

    A fresh opener retains urllib's normal proxy, TLS and redirect handlers, but does
    not add its default ``Python-urllib/<version>`` header. It also removes a
    caller-supplied User-Agent so redirects inherit no such header.
    """
    for name, _ in request.header_items():
        if name.casefold() == "user-agent":
            request.remove_header(name)
    opener = urllib.request.build_opener()
    opener.addheaders = []
    return opener.open(request, timeout=timeout)
