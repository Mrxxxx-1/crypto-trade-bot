"""Tests for log slimming: heartbeat thinning, error compaction, state snapshot.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import agent_tools, log_hygiene  # noqa: E402
from src.exchange import _BrokerBase  # noqa: E402

REAL_502 = (
    "(502, '<html>\\r\\n<head><title>502 Bad Gateway</title></head>\\r\\n<body>\\r\\n"
    "<center><h1>502 Bad Gateway</h1></center>\\r\\n<hr><center>nginx</center>\\r\\n"
    "</body>\\r\\n</html>\\r\\n')"
)

REAL_504 = (
    "(504, '<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 4.01 Transitional//EN\" "
    '"http://www.w3.org/TR/html4/loose.dtd">\\n<HTML><HEAD>'
    "<META HTTP-EQUIV=\"Content-Type\" CONTENT=\"text/html; charset=UTF-8\">\\n"
    "<TITLE>ERROR: The request could not be satisfied</TITLE>\\n</HEAD><BODY>\\n"
    "<H1>504 Gateway Timeout ERROR</H1>\\n<H2>The request could not be satisfied.</H2>\\n"
    "We can't connect to the server for this app or website at this time." * 3
)


def _heartbeat(loop: int, *, positions=None, statuses=None, paused=False) -> dict:
    positions = positions if positions is not None else []
    return {
        "loop": loop,
        "equity": 1000.0 + loop,
        "open_positions": len(positions),
        "paused": paused,
        "positions": positions,
        "statuses": statuses if statuses is not None else ["BTC/USDC:USDC no_entry(short) no_trend"],
    }


ETH_POSITION = {"symbol": "ETH/USDC:USDC", "side": "sell", "legs": 1, "avg_entry": 1716.93}


class ErrorCompactionTests(unittest.TestCase):
    def test_502_html_collapses_to_status_line(self):
        out = log_hygiene.error_message_compact(REAL_502)
        self.assertEqual(out, "502 Bad Gateway")

    def test_504_prefers_heading_with_status_code(self):
        out = log_hygiene.error_message_compact(REAL_504)
        self.assertIn("504", out)
        self.assertNotIn("<", out)
        self.assertLessEqual(len(out), log_hygiene.DEFAULT_ERROR_MAX_CHARS)

    def test_long_plaintext_is_truncated_to_limit(self):
        out = log_hygiene.error_message_compact("x" * 500, limit=50)
        self.assertEqual(len(out), 50)
        self.assertTrue(out.endswith("..."))

    def test_short_plaintext_is_unchanged(self):
        self.assertEqual(
            log_hygiene.error_message_compact("KeyError: 'BTC-USDT-SWAP'"),
            "KeyError: 'BTC-USDT-SWAP'",
        )

    def test_non_string_input_is_handled(self):
        self.assertEqual(log_hygiene.error_message_compact(ValueError("boom")), "boom")

    def test_compact_payload_only_touches_error_keys(self):
        payload = {"symbol": "BTC/USDC:USDC", "message": REAL_502, "loop": 7}
        out = log_hygiene.compact_event_payload(payload)
        self.assertEqual(out["message"], "502 Bad Gateway")
        self.assertEqual(out["symbol"], "BTC/USDC:USDC")
        self.assertEqual(out["loop"], 7)
        self.assertEqual(payload["message"], REAL_502, "input payload must not be mutated")


class HeartbeatThinnerTests(unittest.TestCase):
    def test_first_heartbeat_is_verbose(self):
        thinner = log_hygiene.HeartbeatThinner(verbose_every=20, enabled=True)
        out = thinner.thin(_heartbeat(5))
        self.assertIn("positions", out)
        self.assertIn("statuses", out)

    def test_routine_heartbeats_drop_heavy_keys_but_keep_scalars(self):
        thinner = log_hygiene.HeartbeatThinner(verbose_every=20, enabled=True)
        thinner.thin(_heartbeat(5))
        out = thinner.thin(_heartbeat(10))
        self.assertNotIn("positions", out)
        self.assertNotIn("statuses", out)
        for key in ("loop", "equity", "open_positions", "paused"):
            self.assertIn(key, out, f"{key} is used by dashboard/briefing and must survive")

    def test_verbose_snapshot_returns_every_n_heartbeats(self):
        thinner = log_hygiene.HeartbeatThinner(verbose_every=5, enabled=True)
        verbose_at = [
            i for i in range(1, 12) if "positions" in thinner.thin(_heartbeat(i))
        ]
        self.assertEqual(verbose_at, [1, 6, 11])

    def test_position_change_forces_verbose(self):
        thinner = log_hygiene.HeartbeatThinner(verbose_every=100, enabled=True)
        thinner.thin(_heartbeat(1))
        self.assertNotIn("positions", thinner.thin(_heartbeat(2)))
        opened = thinner.thin(_heartbeat(3, positions=[ETH_POSITION]))
        self.assertIn("positions", opened, "a new position must be captured immediately")
        self.assertNotIn("positions", thinner.thin(_heartbeat(4, positions=[ETH_POSITION])))

    def test_pause_change_forces_verbose(self):
        thinner = log_hygiene.HeartbeatThinner(verbose_every=100, enabled=True)
        thinner.thin(_heartbeat(1))
        thinner.thin(_heartbeat(2))
        self.assertIn("positions", thinner.thin(_heartbeat(3, paused=True)))

    def test_risk_blocked_status_always_logged_in_full(self):
        thinner = log_hygiene.HeartbeatThinner(verbose_every=100, enabled=True)
        thinner.thin(_heartbeat(1))
        blocked = thinner.thin(
            _heartbeat(2, statuses=["BTC/USDC:USDC blocked_by_risk last=62892.0000"])
        )
        self.assertIn("statuses", blocked, "briefing counts blocked_by_risk from statuses")

    def test_disabled_thinner_keeps_everything(self):
        thinner = log_hygiene.HeartbeatThinner(verbose_every=20, enabled=False)
        for i in range(1, 6):
            self.assertIn("positions", thinner.thin(_heartbeat(i)))


class StateSnapshotTests(unittest.TestCase):
    def test_write_then_read_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"ts": "2026-08-30T00:00:00+00:00", "positions": [ETH_POSITION]}
            self.assertIsNotNone(log_hygiene.write_state_snapshot(tmp, payload))
            self.assertEqual(log_hygiene.read_state_snapshot(tmp), payload)

    def test_repeated_writes_overwrite_instead_of_appending(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(20):
                log_hygiene.write_state_snapshot(tmp, {"ts": f"t{i}", "positions": []})
            path = log_hygiene.state_path(tmp)
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(log_hygiene.read_state_snapshot(tmp)["ts"], "t19")

    def test_missing_and_corrupt_files_return_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(log_hygiene.read_state_snapshot(tmp))
            log_hygiene.state_path(tmp).write_text("{not json", encoding="utf-8")
            self.assertIsNone(log_hygiene.read_state_snapshot(tmp))


class BrokerLogEventTests(unittest.TestCase):
    """log_event is the single choke point every error path goes through."""

    def _broker(self, logs_dir: str) -> _BrokerBase:
        settings = SimpleNamespace(logs_dir=logs_dir, initial_equity=1000.0)
        return _BrokerBase(settings, exchange=None)

    def test_error_event_is_compacted_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = self._broker(tmp)
            broker.log_event("error", {"loop": 3, "message": REAL_502, "type": "ServerError"})
            line = (Path(tmp) / "events.jsonl").read_text(encoding="utf-8").strip()
            row = json.loads(line)
            self.assertEqual(row["message"], "502 Bad Gateway")
            self.assertEqual(row["type"], "ServerError")
            self.assertLess(len(line), 150)

    def test_normal_events_keep_their_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = self._broker(tmp)
            broker.log_event("leg_open", {"symbol": "ETH/USDC:USDC", "price": 1716.93})
            row = json.loads((Path(tmp) / "events.jsonl").read_text(encoding="utf-8").strip())
            self.assertEqual(row["symbol"], "ETH/USDC:USDC")
            self.assertEqual(row["price"], 1716.93)
            self.assertEqual(row["event"], "leg_open")


class HeartbeatWritePathTests(unittest.TestCase):
    """Mirrors the heartbeat block in FuturesBot.run_forever end to end."""

    def test_state_file_stays_full_while_event_lines_go_slim(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker = _BrokerBase(
                SimpleNamespace(logs_dir=tmp, initial_equity=1000.0), exchange=None
            )
            thinner = log_hygiene.HeartbeatThinner(verbose_every=20, enabled=True)
            for loop in range(1, 4):
                snapshot = _heartbeat(loop, positions=[ETH_POSITION])
                log_hygiene.write_state_snapshot(
                    tmp, {"ts": f"2026-08-30T00:0{loop}:00+00:00", "event": "heartbeat", **snapshot}
                )
                broker.log_event("heartbeat", thinner.thin(snapshot))

            rows = [
                json.loads(line)
                for line in (Path(tmp) / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("positions", rows[0], "first heartbeat keeps the full snapshot")
            self.assertNotIn("positions", rows[1])
            self.assertNotIn("positions", rows[2])
            self.assertTrue(all(r.get("equity") for r in rows), "equity is never dropped")

            state = log_hygiene.read_state_snapshot(tmp)
            self.assertEqual(state["positions"], [ETH_POSITION])
            self.assertEqual(state["ts"], "2026-08-30T00:03:00+00:00")


class AgentToolsReadPathTests(unittest.TestCase):
    """Slim heartbeats must not blind the dashboard / MCP / Telegram readers."""

    def _settings(self, logs_dir: str) -> SimpleNamespace:
        return SimpleNamespace(logs_dir=logs_dir, mode="live", symbols=["ETH/USDC:USDC"])

    def _write_slim_log(self, tmp: str) -> None:
        events = Path(tmp) / "events.jsonl"
        with events.open("w", encoding="utf-8") as f:
            for i in range(1, 4):
                f.write(json.dumps({
                    "ts": f"2026-08-30T00:0{i}:00+00:00",
                    "event": "heartbeat",
                    "loop": i * 5,
                    "equity": 942.65,
                    "open_positions": 1,
                    "paused": False,
                }) + "\n")

    def test_positions_come_from_state_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_slim_log(tmp)
            log_hygiene.write_state_snapshot(tmp, {
                "ts": "2026-08-30T00:03:00+00:00",
                "event": "heartbeat",
                "equity": 942.65,
                "open_positions": 1,
                "paused": False,
                "positions": [ETH_POSITION],
                "statuses": ["ETH/USDC:USDC in_position(short)"],
            })
            settings = self._settings(tmp)

            status = agent_tools.get_status(settings)
            self.assertEqual(status["equity"], 942.65)
            self.assertEqual(status["open_positions"], 1)
            self.assertEqual(status["positions"], [ETH_POSITION])
            self.assertEqual(status["last_heartbeat_ts"], "2026-08-30T00:03:00+00:00")

            positions = agent_tools.get_open_positions(settings)
            self.assertEqual(positions["count"], 1)
            self.assertEqual(positions["positions"], [ETH_POSITION])

    def test_falls_back_to_last_verbose_heartbeat_without_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            with events.open("w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": "2026-08-30T00:01:00+00:00", "event": "heartbeat",
                    "equity": 900.0, "open_positions": 1, "paused": False,
                    "positions": [ETH_POSITION], "statuses": ["ETH in_position(short)"],
                }) + "\n")
                f.write(json.dumps({
                    "ts": "2026-08-30T00:02:00+00:00", "event": "heartbeat",
                    "equity": 942.65, "open_positions": 1, "paused": False,
                }) + "\n")

            positions = agent_tools.get_open_positions(self._settings(tmp))
            self.assertEqual(positions["positions"], [ETH_POSITION])
            self.assertEqual(positions["as_of"], "2026-08-30T00:01:00+00:00")

    def test_empty_logs_do_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = agent_tools.get_status(self._settings(tmp))
            self.assertIsNone(status["equity"])
            self.assertEqual(status["positions"], [])
            self.assertEqual(agent_tools.get_open_positions(self._settings(tmp))["count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
