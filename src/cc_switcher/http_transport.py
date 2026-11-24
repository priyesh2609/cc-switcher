"""HTTP transport for CCS requests without a User-Agent header."""

from __future__ import annotations

import urllib.error
import urllib.request


class _CredentialRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A 302 from /usage to outside.test would otherwise forward its bearer;
        # a redirect to /usage?at_wall=1 would bypass the read-only URL guard.
        if any(name.casefold() == "authorization" for name, _ in req.header_items()):
            raise urllib.error.HTTPError(req.full_url, code, "Refusing an authenticated redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_without_user_agent(request: urllib.request.Request, *, timeout: float):
    """Open a request without urllib's default Python-version User-Agent.

    A fresh opener retains urllib's normal proxy and TLS handlers, but refuses
    redirects carrying credentials. It does not add its default
    ``Python-urllib/<version>`` header and removes any caller-supplied User-Agent.
    """
    for name, _ in request.header_items():
        if name.casefold() == "user-agent":
            request.remove_header(name)
    opener = urllib.request.build_opener(_CredentialRedirectHandler())
    opener.addheaders = []
    return opener.open(request, timeout=timeout)
