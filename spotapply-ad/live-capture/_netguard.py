"""Hard egress guard for the demo server process (launcher-only, never in the repo).

The app has a few best-effort outbound calls that run even without API keys
(e.g. app/intelligence/referral.get_company_github_repos -> api.github.com, the
/api/favicon proxy, the liveness check behind POST /api/jobs/{id}/verify). For
this capture NOTHING may leave the box, so every outbound TCP connect is refused
at the socket layer -- including to the local egress proxy on 127.0.0.1, which
is how httpx would otherwise reach the internet. The app's own code catches
these failures (they are all best-effort paths). Every refusal is logged to
logs/netguard.log so the report can say exactly what tried to go out.
"""
import os
import socket
import time

_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "netguard.log")
_SELF_PORT = int(os.environ.get("DEMO_PORT", "8765"))


def _note(what: str) -> None:
    try:
        os.makedirs(os.path.dirname(_LOG), exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} pid={os.getpid()} BLOCKED {what}\n")
    except Exception:
        pass


_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex
_orig_getaddrinfo = socket.getaddrinfo


def _allowed(sock, address) -> bool:
    if sock.family == getattr(socket, "AF_UNIX", None):
        return True
    try:
        host, port = address[0], int(address[1])
    except Exception:
        return False
    # Only the demo server talking to itself (never used in practice).
    return host in ("127.0.0.1", "::1", "localhost") and port == _SELF_PORT


def _guarded_connect(self, address):
    if not _allowed(self, address):
        _note(f"connect {address!r}")
        raise ConnectionRefusedError(f"demo net guard: outbound connect to {address!r} refused")
    return _orig_connect(self, address)


def _guarded_connect_ex(self, address):
    if not _allowed(self, address):
        _note(f"connect_ex {address!r}")
        return 111  # ECONNREFUSED
    return _orig_connect_ex(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    h = host.decode() if isinstance(host, bytes) else host
    if h not in (None, "", "localhost", "127.0.0.1", "::1", "0.0.0.0"):
        _note(f"getaddrinfo {h!r}")
        raise socket.gaierror(socket.EAI_NONAME, f"demo net guard: name lookup for {h!r} refused")
    return _orig_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.getaddrinfo = _guarded_getaddrinfo
