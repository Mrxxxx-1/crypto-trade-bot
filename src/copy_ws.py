"""Wake the copy loop the moment the leader trades (Hyperliquid WebSocket ``userFills``).

Polling stays the source of truth: every tick still reads the leader's full
position snapshot over REST, so nothing here can put the copy out of sync. The
WebSocket only decides *when* to tick:

- A leader perp fill wakes the loop at once (after at most ``COPY_POLL_SECONDS``
  since the previous tick, so a burst of fills can't flood the info API).
- While connected and quiet, the loop falls back to a slow safety poll
  (``COPY_SAFETY_POLL_SECONDS``), which is what catches anything the stream
  missed.
- For a minute after any fill, and whenever the socket is down, it polls at the
  normal ``COPY_POLL_SECONDS`` cadence, so a fill that the REST snapshot didn't
  show yet is picked up on the next tick rather than 30 s later.

The connection runs in a daemon thread, pings every 50 s (the server drops
connections idle for 60 s) and reconnects with backoff. A disconnect also wakes
the loop, since fills may have been missed while it was down.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import websocket

MAINNET_WS_URL = "wss://api.hyperliquid.xyz/ws"
PING_SECONDS = 50
RECV_TIMEOUT = 10  # seconds; lets the reader check for stop and send pings while idle
RECENT_FILL_SECONDS = 60  # poll at the normal cadence this long after a fill


def _is_perp(coin: str) -> bool:
    """Spot fills ("@107", "PURR/USDC") are not copied, so they shouldn't wake us."""
    return bool(coin) and not coin.startswith("@") and "/" not in coin


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


class FillTrigger:
    """A ``userFills`` subscription for one address that sets an event on each new perp fill."""

    def __init__(
        self,
        address: str,
        url: str = MAINNET_WS_URL,
        connect: Callable[..., Any] = websocket.create_connection,
        say: Callable[[str], None] = print,
    ) -> None:
        self.address = address.lower()
        self.url = url
        self._connect = connect
        self._say = say
        # Log events are handed to the bot thread (pop_events) so only it writes events.jsonl.
        self._events: deque[tuple[str, dict[str, Any]]] = deque()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.connected = False
        self.fills_seen = 0
        self.last_fill = 0.0  # time.monotonic() of the latest leader perp fill
        self.reconnects = 0

    # --- used by the bot loop -------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="copy-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wait(self, timeout: float) -> bool:
        """Block until a fill (or disconnect) or ``timeout``. Clears the flag; True if woken."""
        woke = self._wake.wait(max(0.0, timeout))
        self._wake.clear()
        return woke

    def pop_events(self) -> list[tuple[str, dict[str, Any]]]:
        """Connection events since the last call, for the bot thread to log."""
        out = []
        while self._events:
            out.append(self._events.popleft())
        return out

    def status(self) -> dict[str, Any]:
        return {"connected": self.connected, "fills_seen": self.fills_seen, "reconnects": self.reconnects}

    def _on_event(self, name: str, data: dict[str, Any]) -> None:
        self._events.append((name, data))

    def idle_seconds(self, poll: float, safety: float, now: Optional[float] = None) -> float:
        """How long the loop may sleep without a wake-up: slow only when connected and quiet."""
        now = time.monotonic() if now is None else now
        if not self.connected or (self.last_fill and now - self.last_fill < RECENT_FILL_SECONDS):
            return poll
        return max(poll, safety)

    # --- the connection thread ------------------------------------------------

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            ws = None
            try:
                ws = self._connect(self.url, timeout=RECV_TIMEOUT)
                ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "userFills", "user": self.address}}))
                self.connected = True
                backoff = 1.0
                self._say(f"[{_stamp()}] WS connected: leader fills wake the bot instantly")
                self._on_event("copy_ws_connected", {"url": self.url})
                self._read(ws)
            except Exception as exc:  # noqa: BLE001 - any failure means: reconnect
                if self._stop.is_set():
                    break
                reason = f"{type(exc).__name__}: {exc}"[:200]
                if self.connected:
                    self._say(f"[{_stamp()}] WS disconnected ({reason}); polling every tick until it reconnects")
                    self._on_event("copy_ws_disconnected", {"error": reason})
                self.connected = False
                self.reconnects += 1
                self._wake.set()  # fills may have been missed while down: poll now
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
            finally:
                self.connected = False
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:  # noqa: BLE001
                        pass

    def _read(self, ws: Any) -> None:
        last_ping = time.monotonic()
        while not self._stop.is_set():
            if time.monotonic() - last_ping >= PING_SECONDS:
                ws.send(json.dumps({"method": "ping"}))
                last_ping = time.monotonic()
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                raise ConnectionError("server closed the connection")
            self.handle(raw)

    def handle(self, raw: str) -> bool:
        """Process one message; True if it was a new leader perp fill."""
        try:
            msg = json.loads(raw)
        except ValueError:
            return False
        if not isinstance(msg, dict) or msg.get("channel") != "userFills":
            return False
        data = msg.get("data") or {}
        if data.get("isSnapshot"):  # the history sent on subscribe, not new trading
            return False
        fills = [f for f in data.get("fills", []) if _is_perp(str(f.get("coin", "")))]
        if not fills:
            return False
        self.fills_seen += len(fills)
        self.last_fill = time.monotonic()
        self._wake.set()
        return True
