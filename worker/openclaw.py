"""Small client for the single supported OpenClaw Gateway protocol (v4)."""

import asyncio
import base64
import hashlib
import json
import os
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.asyncio.client import connect

SCOPES = ["operator.read", "operator.write"]


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


@dataclass
class DeviceIdentity:
    key: Ed25519PrivateKey

    @classmethod
    def load_or_create(cls, path: Path) -> "DeviceIdentity":
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError("Expected an Ed25519 private key")
            return cls(key)
        identity = cls(Ed25519PrivateKey.generate())
        # Publish only a complete key; a worker crash cannot leave a partial PEM.
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".identity-", delete=False
        ) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(
                    identity.key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    return cls.load_or_create(path)
            finally:
                temporary.unlink(missing_ok=True)
        return identity

    @property
    def public_key(self) -> str:
        return _base64url(self.key.public_key().public_bytes_raw())

    @property
    def device_id(self) -> str:
        return hashlib.sha256(self.key.public_key().public_bytes_raw()).hexdigest()

    def connect_params(self, nonce: str, token: str) -> dict[str, Any]:
        timestamp = int(time.time() * 1000)
        payload = "|".join(
            [
                "v3",
                self.device_id,
                "gateway-client",
                "backend",
                "operator",
                ",".join(SCOPES),
                str(timestamp),
                token,
                nonce,
                "linux",
                "",
            ]
        )
        return {
            "minProtocol": 4,
            "maxProtocol": 4,
            "client": {
                "id": "gateway-client",
                "displayName": "Talos worker",
                "version": "0.1.0",
                "platform": "linux",
                "mode": "backend",
            },
            "role": "operator",
            "scopes": SCOPES,
            "caps": [],
            "auth": {"token": token},
            "device": {
                "id": self.device_id,
                "publicKey": self.public_key,
                "signature": _base64url(self.key.sign(payload.encode())),
                "signedAt": timestamp,
                "nonce": nonce,
            },
        }


class GatewayError(Exception):
    def __init__(self, error: dict[str, Any]):
        self.code = error.get("code", "UNKNOWN")
        self.details = error.get("details", {})
        super().__init__(str(error.get("message", "Gateway request failed")))


class DeliveryUncertain(Exception):
    """A sent request lost its reply; callers must not replay chat.send."""


class OpenClawClient:
    def __init__(self, url: str, token: str, identity: DeviceIdentity, timeout: float = 30):
        self.url, self.token, self.identity, self.timeout = url, token, identity, timeout
        self._socket = None
        self._reader = None
        self._pending: dict[str, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self.hello: dict[str, Any] = {}

    async def connect(self) -> "OpenClawClient":
        self._socket = await connect(
            self.url,
            open_timeout=self.timeout,
            # Match the pinned Gateway's frame limit, including history responses.
            max_size=25 * 1024 * 1024,
            proxy=None,
        )
        try:
            challenge = json.loads(await asyncio.wait_for(self._socket.recv(), self.timeout))
            nonce = challenge.get("payload", {}).get("nonce")
            if challenge.get("event") != "connect.challenge" or not isinstance(nonce, str):
                raise ValueError("Missing OpenClaw connection challenge")
            self._reader = asyncio.create_task(self._receive())
            self.hello = await self.request(
                "connect", self.identity.connect_params(nonce, self.token)
            )
            if self.hello.get("protocol") != 4:
                raise ValueError("Unsupported OpenClaw protocol")
            return self
        except BaseException:
            await self.close()
            raise

    async def _receive(self):
        failure: Exception = ConnectionError("OpenClaw connection closed")
        try:
            async for raw in self._socket:
                frame = json.loads(raw)
                if frame.get("type") == "res":
                    future = self._pending.get(frame.get("id"))
                    if future is not None and not future.done():
                        if frame.get("ok"):
                            future.set_result(frame.get("payload", {}))
                        else:
                            future.set_exception(GatewayError(frame.get("error", {})))
                elif frame.get("type") == "event":
                    # Never silently discard diagnostic events on overflow.
                    self.events.put_nowait(frame)
        except Exception as exc:
            failure = exc
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(failure)
            # A closed reader is observable even when no RPC is pending.
            if not self.events.full():
                self.events.put_nowait({"type": "disconnect", "error": type(failure).__name__})
            if self._socket is not None:
                await self._socket.close()

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._socket is None:
            raise ConnectionError("OpenClaw is not connected")
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._socket.send(
                json.dumps(
                    {
                        "type": "req",
                        "id": request_id,
                        "method": method,
                        "params": params,
                    }
                )
            )
            return await asyncio.wait_for(future, self.timeout)
        except GatewayError:
            raise
        except Exception as exc:
            if method == "chat.send":
                raise DeliveryUncertain("Chat send acknowledgment was not received") from exc
            raise
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def send(self, session: str, message: str, run_id: str) -> dict[str, Any]:
        return await self.request(
            "chat.send",
            {
                "sessionKey": session,
                "message": message,
                "idempotencyKey": run_id,
                "deliver": False,
            },
        )

    async def history(self, session: str) -> dict[str, Any]:
        return await self.request("chat.history", {"sessionKey": session, "limit": 100})

    async def abort(self, session: str, run_id: str) -> dict[str, Any]:
        return await self.request("chat.abort", {"sessionKey": session, "runId": run_id})

    async def next_event(self, timeout: float = 60) -> dict[str, Any]:
        if self.events.empty() and self._reader is not None and self._reader.done():
            raise ConnectionError("OpenClaw event connection closed")
        return await asyncio.wait_for(self.events.get(), timeout)

    async def close(self):
        if self._socket is not None:
            await self._socket.close()
        if self._reader is not None:
            self._reader.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader
        self._reader, self._socket = None, None

    async def __aenter__(self):
        return await self.connect()

    async def __aexit__(self, *_):
        await self.close()
