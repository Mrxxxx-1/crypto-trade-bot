"""Tests for the WebSocket fill trigger (``src.copy_ws``) and the bot loop's use of it, offline."""

from __future__ import annotations

import json
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import websocket

from src.bot import FuturesBot
from src.copy_ws import RECENT_FILL_SECONDS, FillTrigger

LEADER = "0x" + "AB" * 20


def fills_msg(*coins, snapshot=False):
    data = {"user": LEADER.lower(), "fills": [{"coin": c, "sz": "1", "px": "1"} for c in coins]}
    if snapshot:
        data["isSnapshot"] = True
    return json.dumps({"channel": "userFills", "data": data})


class FakeWS:
    """Replays ``script`` (messages, or exceptions to raise) then idles with recv timeouts."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []
        self.closed = False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self):
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        time.sleep(0.01)
        raise websocket.WebSocketTimeoutException("idle")

    def close(self):
        self.closed = True


def wait_until(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class TestHandle(unittest.TestCase):
    def setUp(self):
        self.t = FillTrigger(LEADER, connect=None, say=lambda s: None)

    def test_new_perp_fill_wakes(self):
        self.assertTrue(self.t.handle(fills_msg("BTC", "xyz:SOXL")))
        self.assertEqual(self.t.fills_seen, 2)
        self.assertTrue(self.t.wait(0))
        self.assertFalse(self.t.wait(0))  # waking clears the flag

    def test_subscribe_snapshot_is_history_not_trading(self):
        self.assertFalse(self.t.handle(fills_msg("BTC", snapshot=True)))
        self.assertFalse(self.t.wait(0))

    def test_spot_fills_and_other_channels_are_ignored(self):
        self.assertFalse(self.t.handle(fills_msg("@107", "PURR/USDC")))
        self.assertFalse(self.t.handle(json.dumps({"channel": "pong"})))
        self.assertFalse(self.t.handle(json.dumps({"channel": "subscriptionResponse", "data": {}})))
        self.assertFalse(self.t.handle("not json"))
        self.assertFalse(self.t.wait(0))


class TestIdle(unittest.TestCase):
    def test_slow_only_when_connected_and_quiet(self):
        t = FillTrigger(LEADER, connect=None, say=lambda s: None)
        self.assertEqual(t.idle_seconds(5, 30), 5)  # not connected: normal polling
        t.connected = True
        self.assertEqual(t.idle_seconds(5, 30), 30)
        t.handle(fills_msg("ETH"))
        self.assertEqual(t.idle_seconds(5, 30), 5)  # right after a fill: keep polling fast
        later = time.monotonic() + RECENT_FILL_SECONDS + 1
        self.assertEqual(t.idle_seconds(5, 30, now=later), 30)


class TestConnection(unittest.TestCase):
    def test_subscribes_wakes_on_fill_and_reconnects(self):
        sockets = [
            FakeWS([fills_msg("BTC", snapshot=True), fills_msg("BTC"), ConnectionError("reset")]),
            FakeWS([]),
        ]
        calls = []

        def connect(url, timeout):
            calls.append(url)
            return sockets[len(calls) - 1] if len(calls) <= len(sockets) else FakeWS([])

        said = []
        t = FillTrigger(LEADER, connect=connect, say=said.append)
        t.start()
        try:
            self.assertTrue(wait_until(lambda: len(calls) >= 2 and t.connected))
        finally:
            t.stop()
            t._thread.join(2)
        sub = sockets[0].sent[0]
        self.assertEqual(sub["subscription"], {"type": "userFills", "user": LEADER.lower()})
        self.assertEqual(t.fills_seen, 1)  # the snapshot didn't count
        self.assertEqual(t.reconnects, 1)
        self.assertTrue(sockets[0].closed)
        names = [n for n, _ in t.pop_events()]
        self.assertEqual(names, ["copy_ws_connected", "copy_ws_disconnected", "copy_ws_connected"])
        self.assertTrue(any("disconnected" in s for s in said))

    def test_failed_connect_wakes_the_loop(self):
        def connect(url, timeout):
            raise OSError("no network")

        t = FillTrigger(LEADER, connect=connect, say=lambda s: None)
        t.start()
        try:
            self.assertTrue(t.wait(2))  # poll now: fills may have been missed
            self.assertFalse(t.connected)
        finally:
            t.stop()
            t._thread.join(2)


class FakeTrigger:
    def __init__(self, wakes, idle=30):
        self.wakes = wakes
        self.idle = idle
        self.waited = []
        self.events = [("copy_ws_connected", {"url": "x"})]

    def idle_seconds(self, poll, safety):
        return self.idle

    def wait(self, timeout):
        self.waited.append(timeout)
        return self.wakes

    def pop_events(self):
        out, self.events = self.events, []
        return out


class TestBotWait(unittest.TestCase):
    def bot(self, trigger):
        logged = []
        bot = SimpleNamespace(
            fill_trigger=trigger,
            settings=SimpleNamespace(copy_poll_seconds=5, copy_safety_poll_seconds=30),
            broker=SimpleNamespace(log_event=lambda name, data: logged.append(name)),
            _poll_interval=lambda: 5,
        )
        return bot, logged

    def test_fill_wakes_but_ticks_no_faster_than_poll(self):
        trigger = FakeTrigger(wakes=True)
        bot, logged = self.bot(trigger)
        with mock.patch("src.bot.time.monotonic", return_value=101.0), mock.patch("src.bot.time.sleep") as sleep:
            FuturesBot._wait_next_tick(bot, tick_started=100.0)
        self.assertAlmostEqual(trigger.waited[0], 29.0)  # up to the safety poll
        sleep.assert_called_once_with(4.0)  # 5 s minimum gap since the last tick
        self.assertEqual(logged, ["copy_ws_connected"])  # logged from the bot thread

    def test_quiet_waits_out_the_idle_period(self):
        trigger = FakeTrigger(wakes=False)
        bot, _ = self.bot(trigger)
        with mock.patch("src.bot.time.monotonic", return_value=100.0), mock.patch("src.bot.time.sleep") as sleep:
            FuturesBot._wait_next_tick(bot, tick_started=100.0)
        self.assertAlmostEqual(trigger.waited[0], 30.0)
        sleep.assert_not_called()

    def test_without_ws_it_polls_as_before(self):
        bot, _ = self.bot(None)
        with mock.patch("src.bot.time.sleep") as sleep:
            FuturesBot._wait_next_tick(bot, tick_started=0.0)
        sleep.assert_called_once_with(5)


class TestConfig(unittest.TestCase):
    def test_defaults_and_switch(self):
        from tests.helpers import make_settings

        s = make_settings()
        self.assertTrue(s.copy_ws)
        self.assertEqual(s.copy_safety_poll_seconds, 30)
        self.assertFalse(make_settings(COPY_WS="false").copy_ws)


if __name__ == "__main__":
    unittest.main()
