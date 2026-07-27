"""
Adaptive Light - Home Assistant client.

Two transports, used for different things:

  * REST for request/response work: reading config and states, calling
    services, and pushing almanac sensors.

  * WebSocket for the live event stream, and for the config commands
    that create and delete helpers.

Nothing here knows about rooms, sections or almanacs. It is a transport
layer; the runtime built on top supplies the meaning.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

log = logging.getLogger("adaptive_light.ha")

DEFAULT_BACKOFF = (1, 2, 5, 10, 30, 60)


class HAError(RuntimeError):
    pass


class HAAuthError(HAError):
    """Token rejected. Never retried - retrying a bad token just spams
    the log and delays the user discovering the real problem."""


# ---------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------

@dataclass
class HARest:
    base_url: str
    token: str
    verify_ssl: bool = True
    timeout: float = 10.0
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                verify=self.verify_ssl,
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, **kw) -> Any:
        resp = await self.client.request(method, path, **kw)
        if resp.status_code in (401, 403):
            raise HAAuthError("Home Assistant rejected the access token")
        resp.raise_for_status()
        if not resp.content:
            return None
        try:
            return resp.json()
        except json.JSONDecodeError:
            return resp.text

    async def ping(self) -> str:
        """GET /api/ - cheapest proof that the URL and token both work."""
        data = await self._request("GET", "/api/")
        return data.get("message", "") if isinstance(data, dict) else str(data)

    async def config(self) -> dict:
        """Latitude, longitude, elevation, time_zone, version.

        The single source of location and timezone: computing them
        independently is how the container and HA come to disagree about
        when sunset is.
        """
        return await self._request("GET", "/api/config")

    async def states(self) -> list[dict]:
        return await self._request("GET", "/api/states")

    async def state(self, entity_id: str) -> dict | None:
        try:
            return await self._request("GET", f"/api/states/{entity_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    async def set_state(
        self, entity_id: str, state: str, attributes: dict | None = None
    ) -> dict:
        """Push a state into HA's state machine.

        This is how almanacs reach Home Assistant. Note that states set
        this way do NOT survive an HA restart, so the runtime re-pushes
        on a timer and on homeassistant_start.
        """
        return await self._request(
            "POST",
            f"/api/states/{entity_id}",
            json={"state": state, "attributes": attributes or {}},
        )

    async def delete_state(self, entity_id: str) -> None:
        await self._request("DELETE", f"/api/states/{entity_id}")

    async def call_service(
        self, domain: str, service: str, data: dict | None = None
    ) -> Any:
        return await self._request(
            "POST", f"/api/services/{domain}/{service}", json=data or {}
        )

    async def render_template(self, template: str) -> str:
        """Render a Jinja template server-side. Always returns a string,
        so it cannot answer questions about native types on its own -
        use it to render a template that reports the type instead."""
        return await self._request("POST", "/api/template", json={"template": template})

    async def trigger_automation(self, entity_id: str) -> Any:
        """Fire an automation's action block.

        Note: skip_condition defaults to true, so any conditions on the
        target automation are ignored. Generated scene automations
        therefore carry none.
        """
        return await self.call_service(
            "automation", "trigger", {"entity_id": entity_id}
        )


# ---------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------

EventHandler = Callable[[dict], Awaitable[None] | None]


class HAWebSocket:
    """Authenticated WebSocket with a reconnect ladder.

    Subscriptions are re-established after every reconnect, because a
    dropped connection silently loses them - which would leave the
    container running, apparently healthy, and blind.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        verify_ssl: bool = True,
        backoff: tuple[int, ...] = DEFAULT_BACKOFF,
    ) -> None:
        self.url = (
            base_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
            + "/api/websocket"
        )
        self.token = token
        self.verify_ssl = verify_ssl
        self.backoff = backoff

        self._ws: Any = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: dict[str, list[EventHandler]] = {}
        self._subscriptions: dict[int, str] = {}
        self._connected = asyncio.Event()
        self._stop = False
        self.last_seen: datetime | None = None
        self.ha_version: str | None = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def on_event(self, event_type: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    # -- lifecycle ----------------------------------------------------

    async def run(self) -> None:
        """Connect and stay connected. Runs until stop() is called."""
        attempt = 0
        while not self._stop:
            try:
                await self._connect_once()
                attempt = 0
            except HAAuthError:
                raise  # a bad token will never fix itself
            except (OSError, ConnectionClosed, asyncio.TimeoutError) as exc:
                self._connected.clear()
                delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                attempt += 1
                log.warning("websocket lost (%s); reconnecting in %ss", exc, delay)
                await asyncio.sleep(delay)

    async def stop(self) -> None:
        self._stop = True
        if self._ws is not None:
            await self._ws.close()

    async def _connect_once(self) -> None:
        kw: dict[str, Any] = {}
        if self.url.startswith("wss://") and not self.verify_ssl:
            import ssl

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            kw["ssl"] = ctx

        async with ws_connect(self.url, **kw) as ws:
            self._ws = ws
            greeting = json.loads(await ws.recv())
            if greeting.get("type") != "auth_required":
                raise HAError(f"unexpected greeting: {greeting.get('type')}")
            self.ha_version = greeting.get("ha_version")

            await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            result = json.loads(await ws.recv())
            if result.get("type") != "auth_ok":
                raise HAAuthError(result.get("message", "authentication failed"))

            self.ha_version = result.get("ha_version", self.ha_version)
            self._connected.set()
            self.last_seen = datetime.now(timezone.utc)
            log.info("connected to Home Assistant %s", self.ha_version)

            # The reader must be running before any command is sent.
            # Every command awaits its result message, and only the read
            # loop can deliver one - subscribing first deadlocks until
            # the timeout, drops the connection, and reconnects into the
            # same trap, leaving the event stream permanently dead while
            # the log cheerfully reports "connected".
            reader = asyncio.create_task(self._read_loop(ws))
            try:
                await self._resubscribe()
                await reader
            finally:
                reader.cancel()

    async def _resubscribe(self) -> None:
        wanted = set(self._subscriptions.values()) or set(self._handlers)
        self._subscriptions.clear()
        for event_type in wanted:
            await self.subscribe(event_type)

    async def _read_loop(self, ws) -> None:
        async for raw in ws:
            self.last_seen = datetime.now(timezone.utc)
            msg = json.loads(raw)
            kind = msg.get("type")

            if kind == "event":
                await self._dispatch(msg.get("event", {}))
            elif kind in ("result", "pong"):
                fut = self._pending.pop(msg.get("id"), None)
                if fut and not fut.done():
                    if kind == "result" and not msg.get("success", True):
                        fut.set_exception(
                            HAError(msg.get("error", {}).get("message", "command failed"))
                        )
                    else:
                        fut.set_result(msg.get("result"))

    async def _dispatch(self, event: dict) -> None:
        for handler in self._handlers.get(event.get("event_type", ""), []):
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # a bad handler must not kill the socket
                log.exception("event handler failed")

    # -- commands -----------------------------------------------------

    async def send(self, payload: dict, timeout: float = 10.0) -> Any:
        if not self.connected:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
        self._id += 1
        msg_id = self._id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send(json.dumps({"id": msg_id, **payload}))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(msg_id, None)

    async def subscribe(self, event_type: str) -> int:
        await self.send({"type": "subscribe_events", "event_type": event_type})
        self._subscriptions[self._id] = event_type
        return self._id

    # -- helper management --------------------------------------------
    # These use the frontend's own commands rather than a documented
    # public API. Verified by tools/doctor.py before being relied on;
    # if unavailable, the fallback is generated YAML.

    async def create_helper(self, domain: str, config: dict) -> dict:
        return await self.send({"type": f"{domain}/create", **config})

    async def list_helpers(self, domain: str) -> list[dict]:
        return await self.send({"type": f"{domain}/list"})

    async def delete_helper(self, domain: str, helper_id: str) -> Any:
        return await self.send({"type": f"{domain}/delete", f"{domain}_id": helper_id})
