"""Loopback Host, Origin and JSON rules. Forwarded headers are not authority."""
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
LOOPBACK = {"127.0.0.1", "::1", "testserver"}


def _headers(scope, name):
    target = name.lower().encode("latin1")
    values = []
    for key, value in scope.get("headers") or ():
        if key.lower() == target:
            values.append(value.decode("latin1").strip())
    return values


def _single(values):
    if not values:
        return None
    if len(values) != 1 or not values[0] or "," in values[0] or "\n" in values[0] or "\r" in values[0]:
        return False
    return values[0]


def acceptable_hosts(server):
    """Exact Host values for the socket that accepted the connection."""
    if not server or len(server) < 2:
        return set()
    host, port = server[0], server[1]
    if host not in LOOPBACK:
        return set()
    bare = "[::1]" if host == "::1" else host
    if port in (None, 80):
        return {bare, f"{bare}:80"}
    return {f"{bare}:{port}"}


def loopback_origins(values):
    """Keep exact http loopback origins. Public or credentialed origins are dropped."""
    accepted = []
    for value in values:
        if not isinstance(value, str):
            continue
        parts = urlsplit(value.strip())
        if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "::1"} or parts.username or parts.password or parts.path not in {"", "/"} or parts.query or parts.fragment:
            continue
        host = "[::1]" if parts.hostname == "::1" else parts.hostname
        port = "" if parts.port in (None, 80) else f":{parts.port}"
        accepted.append(f"http://{host}{port}")
    return tuple(dict.fromkeys(accepted))


def origin_hosts(origins):
    hosts = set()
    for origin in origins:
        parts = urlsplit(origin)
        if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "::1"} or parts.username or parts.password or parts.path not in {"", "/"} or parts.query or parts.fragment:
            continue
        port = parts.port
        bare = "[::1]" if parts.hostname == "::1" else parts.hostname
        hosts.add(bare if port in (None, 80) else f"{bare}:{port}")
    return hosts


def rejection(scope, origins):
    """Return a stable code, or None when the request may proceed."""
    server = scope.get("server")
    allowed = acceptable_hosts(server)
    if not allowed:
        return "LOCAL_HOST_REJECTED"
    host = _single(_headers(scope, "host"))
    if host is False or host not in allowed:
        return "LOCAL_HOST_REJECTED"
    forwarded_hosts = _headers(scope, "x-forwarded-host")
    if forwarded_hosts:
        parsed = [_single([value]) for value in forwarded_hosts]
        allowed_forwarded = allowed | origin_hosts(origins)
        if any(value is False or value not in allowed_forwarded for value in parsed):
            return "LOCAL_FORWARDED_REJECTED"
    if _headers(scope, "forwarded"):
        return "LOCAL_FORWARDED_REJECTED"
    protos = _headers(scope, "x-forwarded-proto")
    if any(value.lower() not in {"http", "https"} or "," in value for value in protos):
        return "LOCAL_FORWARDED_REJECTED"
    origin = _single(_headers(scope, "origin"))
    if origin is False or origin == "null":
        return "LOCAL_ORIGIN_REJECTED"
    if origin is not None and origin not in set(origins):
        return "LOCAL_ORIGIN_REJECTED"
    method = scope.get("method", "GET").upper()
    fetch_site = _single(_headers(scope, "sec-fetch-site"))
    if fetch_site is False:
        return "LOCAL_FETCH_REJECTED"
    if method in UNSAFE:
        if fetch_site not in {None, "same-origin", "none"}:
            return "LOCAL_FETCH_REJECTED"
        if fetch_site == "same-origin" and origin not in set(origins):
            return "LOCAL_ORIGIN_REJECTED"
        if fetch_site == "none" and origin == "null":
            return "LOCAL_ORIGIN_REJECTED"
        content_type = _single(_headers(scope, "content-type"))
        if content_type is False or content_type is None:
            return "LOCAL_CONTENT_TYPE_REJECTED"
        media, _, params = content_type.partition(";")
        if media.strip().lower() != "application/json":
            return "LOCAL_CONTENT_TYPE_REJECTED"
        for piece in params.split(";") if params else []:
            name, _, value = piece.strip().partition("=")
            if name.lower() == "charset" and value.strip().lower() not in {"utf-8", "utf8"}:
                return "LOCAL_CONTENT_TYPE_REJECTED"
        fetch_mode = _single(_headers(scope, "sec-fetch-mode"))
        if fetch_mode is False or fetch_mode == "no-cors":
            return "LOCAL_FETCH_REJECTED"
    return None


class LocalHttpMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, origins=()):
        super().__init__(app)
        self.origins = tuple(origins)

    async def dispatch(self, request, call_next):
        code = rejection(request.scope, self.origins)
        if code:
            return JSONResponse({"detail": code}, status_code=403)
        return await call_next(request)
