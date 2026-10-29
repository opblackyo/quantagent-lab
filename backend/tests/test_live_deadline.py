"""Owned 127.0.0.1 deadline bounds. Elapsed time is the process clock.

DNS inside socket.create_connection happens before the socket wait cap.
These listeners use a literal address, so they do not prove a DNS bound.
Nothing here claims the upstream stopped billing.
"""
import os
import socket
import threading
import time

import httpx
import pytest

from app.http_policy import HTTPTimeoutPolicy
from app.live_readiness import LiveRefusal, ServerLiveApproval, send_live_attempt, send_loopback_attempt

SECRET = "synthetic-loopback-secret-marker"
PHASE = HTTPTimeoutPolicy(connect=30, read=30, write=30, pool=30)
DEADLINE = 0.5


@pytest.fixture(autouse=True)
def no_provider_or_key(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("provider or non-loopback network is forbidden")

    from app.agents import OpenAIProvider
    from app.gateway import KuaiAPIProvider

    monkeypatch.setattr(OpenAIProvider, "__init__", forbidden)
    monkeypatch.setattr(KuaiAPIProvider, "__init__", forbidden)
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo
    original_create = socket.create_connection

    def allowed(address):
        host = address[0] if isinstance(address, tuple) and address else None
        return str(host) == "127.0.0.1"

    def guarded_connect(self, address, *args, **kwargs):
        if allowed(address):
            return original_connect(self, address, *args, **kwargs)
        return forbidden()

    def guarded_connect_ex(self, address, *args, **kwargs):
        if allowed(address):
            return original_connect_ex(self, address, *args, **kwargs)
        return forbidden()

    def guarded_getaddrinfo(host, *args, **kwargs):
        if str(host) == "127.0.0.1":
            return original_getaddrinfo(host, *args, **kwargs)
        return forbidden()

    def guarded_create(address, *args, **kwargs):
        if allowed(address):
            return original_create(address, *args, **kwargs)
        return forbidden()

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", guarded_create)
    original = os._Environ.__getitem__

    def guarded(env, name):
        if str(name).startswith(("OPENAI_", "KUAIAPI_", "LLM_")):
            return forbidden()
        return original(env, name)

    monkeypatch.setattr(os._Environ, "__getitem__", guarded)


class Loader:
    def __init__(self):
        self.calls = []

    def load(self, profile_id):
        self.calls.append(profile_id)
        return SECRET


class Listener:
    """One thread, one loopback socket, closed by an event."""

    def __init__(self, mode):
        self.mode = mode
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.requests = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._run, name="owned-deadline-" + mode, daemon=True)
        self.thread.start()
        if not self.ready.wait(2):
            self.close()
            raise RuntimeError("owned listener did not start")

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.port}/v1/responses"

    def close(self):
        self.stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self.thread.join(3)

    def _run(self):
        self.ready.set()
        while not self.stop.is_set():
            try:
                conn, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(0.2)
                self.requests.append(self._read_headers(conn))
                try:
                    self._respond(conn)
                except OSError:
                    pass

    def _read_headers(self, conn):
        data = b""
        while b"\r\n\r\n" not in data and not self.stop.is_set():
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            data += chunk
            if len(data) > 100000:
                break
        head, marker, rest = data.partition(b"\r\n\r\n")
        if not marker:
            return data
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip() or b"0")
        while len(rest) < length and not self.stop.is_set():
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            rest += chunk
        return head + marker + rest

    def _finish(self, conn):
        try:
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            return
        while not self.stop.is_set():
            try:
                if not conn.recv(4096):
                    return
            except socket.timeout:
                continue
            except OSError:
                return

    def _respond(self, conn):
        if self.mode == "first-byte":
            self.stop.wait(3)
            return
        if self.mode == "inter-chunk":
            conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\nx\r\n")
            self.stop.wait(3)
            return
        if self.mode == "trickle":
            conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            while not self.stop.is_set():
                conn.sendall(b"1\r\nx\r\n")
                if self.stop.wait(0.05):
                    return
            return
        if self.mode == "large":
            conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            remaining = 1_048_576 + 4096
            piece = b"y" * 4096
            while remaining > 0 and not self.stop.is_set():
                take = piece if remaining >= len(piece) else b"y" * remaining
                conn.sendall(f"{len(take):x}\r\n".encode() + take + b"\r\n")
                remaining -= len(take)
            conn.sendall(b"0\r\n\r\n")
            self._finish(conn)
            return
        if self.mode == "redirect":
            conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:9/nowhere\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            self._finish(conn)
            return
        if self.mode == "reset":
            return
        raise AssertionError(self.mode)


def _attempt(listener, deadline=DEADLINE):
    approval = ServerLiveApproval(holder="server", funding_approved=True, manifest_hash="a" * 64, profile_id="synthetic-live")
    loader = Loader()
    client = httpx.Client(http2=False, follow_redirects=False)
    started = time.monotonic()
    try:
        result = send_loopback_attempt(
            approval=approval, profile_id="synthetic-live", manifest_hash="a" * 64, endpoint=listener.endpoint,
            loader=loader, client=client, timeout_policy=PHASE, deadline_seconds=deadline, content=b"{}")
    finally:
        elapsed = time.monotonic() - started
        client.close()
    return result, loader, elapsed


def test_production_sender_refuses_loopback_before_load():
    listener = Listener("first-byte")
    loader = Loader()
    client = httpx.Client(http2=False, follow_redirects=False)
    try:
        with pytest.raises(LiveRefusal, match="LIVE_ENDPOINT_REJECTED"):
            send_live_attempt(
                approval=ServerLiveApproval(holder="server", funding_approved=True, manifest_hash="a" * 64, profile_id="synthetic-live"),
                profile_id="synthetic-live", manifest_hash="a" * 64, endpoint=listener.endpoint, loader=loader,
                client=client, timeout_policy=PHASE, deadline_seconds=DEADLINE, content=b"{}")
        assert loader.calls == [] and listener.requests == []
    finally:
        client.close()
        listener.close()


@pytest.mark.parametrize("mode,stopped,floor", [
    ("first-byte", {"OVERALL_DEADLINE", "PHASE_TIMEOUT"}, 0.25),
    ("inter-chunk", {"OVERALL_DEADLINE", "PHASE_TIMEOUT"}, 0.25),
    ("trickle", {"OVERALL_DEADLINE"}, 0.25),
    ("large", {"RESPONSE_BODY_LIMIT"}, 0.0),
    ("redirect", {"REDIRECT_REJECTED"}, 0.0),
    ("reset", {"TRANSPORT_UNCERTAIN"}, 0.0),
])
def test_owned_listener_stays_inside_the_local_wait_cap(mode, stopped, floor):
    listener = Listener(mode)
    try:
        result, loader, elapsed = _attempt(listener)
        assert result.sent is True and result.retry_allowed is False and result.financial_proof is False
        assert result.stopped in stopped, (result.stopped, elapsed, len(listener.requests))
        assert "upstream" not in (result.stopped or "")
        assert floor <= elapsed < 2.5, (mode, elapsed, result.stopped)
        assert len(listener.requests) == 1 and loader.calls == ["synthetic-live"]
        assert SECRET.encode() not in (result.body_text or "").encode()
    finally:
        listener.close()
        assert listener.thread.is_alive() is False


def _build_ephemeral_tls_pki(tmp_path):
    from pathlib import Path
    import shutil
    import ssl
    import subprocess

    openssl = shutil.which("openssl")
    if not openssl:
        git_bin = shutil.which("git")
        assert git_bin is not None, "openssl or git distribution required for local TLS test"
        openssl = str(Path(git_bin).resolve().parents[1] / "usr" / "bin" / "openssl.exe")
    ca_cnf = tmp_path / "ca.cnf"
    ca_cnf.write_text(
        "[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=v3_ca\n"
        "[dn]\nCN=SyntheticLocalRootCA\n"
        "[v3_ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
        "subjectKeyIdentifier=hash\n",
        encoding="utf-8",
    )
    ca_key = tmp_path / "ca.key"
    ca_pem = tmp_path / "ca.pem"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(ca_key),
         "-out", str(ca_pem), "-days", "1", "-config", str(ca_cnf), "-extensions", "v3_ca"],
        check=True, capture_output=True,
    )

    def make_leaf(name, san, signed_by_ca=True):
        key = tmp_path / f"{name}.key"
        csr = tmp_path / f"{name}.csr"
        pem = tmp_path / f"{name}.pem"
        cnf = tmp_path / f"{name}.cnf"
        cnf.write_text(
            f"[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN={name}\n"
            f"[v3_leaf]\nbasicConstraints=CA:FALSE\n"
            f"keyUsage=critical,digitalSignature,keyEncipherment\n"
            f"extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\n"
            f"authorityKeyIdentifier=keyid,issuer\nsubjectAltName={san}\n",
            encoding="utf-8",
        )
        if signed_by_ca:
            subprocess.run(
                [openssl, "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                 "-out", str(csr), "-config", str(cnf)],
                check=True, capture_output=True,
            )
            subprocess.run(
                [openssl, "x509", "-req", "-in", str(csr), "-CA", str(ca_pem), "-CAkey", str(ca_key),
                 "-CAcreateserial", "-out", str(pem), "-days", "1", "-extfile", str(cnf), "-extensions", "v3_leaf"],
                check=True, capture_output=True,
            )
        else:
            subprocess.run(
                [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                 "-out", str(pem), "-days", "1", "-config", str(cnf), "-extensions", "v3_leaf"],
                check=True, capture_output=True,
            )
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(certfile=str(pem), keyfile=str(key))
        return server_ctx

    client_ctx = ssl.create_default_context(cafile=str(ca_pem))
    assert client_ctx.verify_mode == ssl.CERT_REQUIRED and client_ctx.check_hostname is True
    return {
        "client_ctx": client_ctx,
        "valid_server_ctx": make_leaf("loopback-valid", "IP:127.0.0.1", signed_by_ca=True),
        "wrong_host_ctx": make_leaf("loopback-wrong-san", "DNS:wrong.example.test", signed_by_ca=True),
        "untrusted_ctx": make_leaf("loopback-untrusted", "IP:127.0.0.1", signed_by_ca=False),
    }


class TLSListener:
    """Owned 127.0.0.1 HTTPS listener for offline certificate and TLS handshake stall tests."""

    def __init__(self, mode, ssl_context=None):
        self.mode = mode
        self.ssl_context = ssl_context
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.accepted = 0
        self.requests = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._run, name="owned-tls-" + mode, daemon=True)
        self.thread.start()
        if not self.ready.wait(2):
            self.close()
            raise RuntimeError("owned TLS listener did not start")

    @property
    def endpoint(self):
        return f"https://127.0.0.1:{self.port}/v1/responses"

    def close(self):
        self.stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self.thread.join(3)

    def _run(self):
        import ssl

        self.ready.set()
        while not self.stop.is_set():
            try:
                conn, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.accepted += 1
            with conn:
                if self.mode == "tls-stall":
                    deadline = time.monotonic() + 2.5
                    while time.monotonic() < deadline and not self.stop.is_set():
                        time.sleep(0.02)
                    continue
                conn.settimeout(1.0)
                try:
                    tls_conn = self.ssl_context.wrap_socket(conn, server_side=True)
                except (ssl.SSLError, OSError):
                    continue
                with tls_conn:
                    tls_conn.settimeout(0.5)
                    self.requests.append(Listener._read_headers(self, tls_conn))
                    body = f'{{"id":"tls-ok","echo":"{SECRET}"}}'.encode()
                    reply = (
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                        + body
                    )
                    try:
                        tls_conn.sendall(reply)
                        tls_conn.unwrap()
                    except (ssl.SSLError, OSError):
                        pass
                Listener._finish(self, conn)


def _tls_attempt(listener, client_ctx, deadline=DEADLINE):
    approval = ServerLiveApproval(holder="server", funding_approved=True, manifest_hash="a" * 64, profile_id="synthetic-live")
    loader = Loader()
    client = httpx.Client(verify=client_ctx, http2=False, follow_redirects=False)
    started = time.monotonic()
    try:
        result = send_loopback_attempt(
            approval=approval, profile_id="synthetic-live", manifest_hash="a" * 64, endpoint=listener.endpoint,
            loader=loader, client=client, timeout_policy=PHASE, deadline_seconds=deadline, content=b"{}")
    finally:
        elapsed = time.monotonic() - started
        client.close()
    return result, loader, elapsed


def test_loopback_tls_certificate_validation_and_handshake_stall_bound(tmp_path):
    pki = _build_ephemeral_tls_pki(tmp_path)
    valid = TLSListener("tls-ok", ssl_context=pki["valid_server_ctx"])
    try:
        loader = Loader()
        client = httpx.Client(verify=pki["client_ctx"], http2=False, follow_redirects=False)
        try:
            with pytest.raises(LiveRefusal, match="LIVE_ENDPOINT_REJECTED"):
                send_live_attempt(
                    approval=ServerLiveApproval(holder="server", funding_approved=True, manifest_hash="a" * 64, profile_id="synthetic-live"),
                    profile_id="synthetic-live", manifest_hash="a" * 64, endpoint=valid.endpoint,
                    loader=loader, client=client, timeout_policy=PHASE, deadline_seconds=5.0, content=b"{}")
            assert loader.calls == [] and valid.accepted == 0
        finally:
            client.close()

        ok_result, ok_loader, _ok_elapsed = _tls_attempt(valid, pki["client_ctx"], deadline=5.0)
        assert ok_result.sent is True and ok_result.status_code == 200 and ok_result.stopped is None
        assert ok_result.retry_allowed is False and ok_result.financial_proof is False
        assert SECRET not in (ok_result.body_text or "") and "[REDACTED_API_KEY]" in (ok_result.body_text or "")
        assert ok_loader.calls == ["synthetic-live"] and valid.accepted == 1 and len(valid.requests) == 1
    finally:
        valid.close()
        assert valid.thread.is_alive() is False

    for name, ctx in (("untrusted", pki["untrusted_ctx"]), ("wrong-san", pki["wrong_host_ctx"])):
        bad = TLSListener("tls-ok", ssl_context=ctx)
        try:
            bad_result, bad_loader, _bad_elapsed = _tls_attempt(bad, pki["client_ctx"], deadline=5.0)
            assert bad_result.sent is True and bad_result.status_code is None, name
            assert bad_result.stopped == "TRANSPORT_UNCERTAIN" and bad_result.retry_allowed is False, name
            assert bad_loader.calls == ["synthetic-live"] and bad.accepted == 1 and bad.requests == [], name
        finally:
            bad.close()
            assert bad.thread.is_alive() is False

    stall = TLSListener("tls-stall")
    try:
        stall_result, stall_loader, stall_elapsed = _tls_attempt(stall, pki["client_ctx"], deadline=0.35)
        assert stall_result.sent is True and stall_result.status_code is None
        assert stall_result.stopped in {"OVERALL_DEADLINE", "PHASE_TIMEOUT"}
        assert stall_result.retry_allowed is False and stall_result.financial_proof is False
        assert 0.25 <= stall_elapsed < 2.5, (stall_elapsed, stall_result.stopped)
        assert stall_loader.calls == ["synthetic-live"] and stall.accepted == 1
    finally:
        stall.close()
        assert stall.thread.is_alive() is False
