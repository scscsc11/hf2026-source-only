"""Spec 025 — ScorePublisher unit tests.

Pure-Python tests; no Redis server required. We monkey-patch the
``redis`` import inside :mod:`examples._common.score_publisher` with a
fake client that records ``publish`` calls, then verify:

  * channel-name constant
  * payload wire shape (``type``, ``sim_time``, ``tick``, ``final``,
    ``ts``, preserved evaluator fields)
  * publish / publish_final contract
  * graceful failure when Redis is unreachable (no exception escapes)
  * idempotent ``close()``
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import examples._common.score_publisher as sp  # noqa: E402


# ── fake redis client ────────────────────────────────────────────────────


class _FakeRedisClient:
    """In-memory stand-in for ``redis.Redis`` used by :class:`ScorePublisher`."""

    def __init__(self, *, raise_on_publish: bool = False) -> None:
        self.published: list[tuple[str, str]] = []
        self._raise = raise_on_publish
        self.closed = False

    def publish(self, channel: str, message: str) -> int:
        if self._raise:
            raise RuntimeError("fake redis down")
        self.published.append((channel, message))
        return 1

    def close(self) -> None:
        self.closed = True


def _install_fake_redis(faker: _FakeRedisClient) -> None:
    """Patch the ``redis`` module seen by :mod:`score_publisher`."""

    class _FakeRedisModule:
        @staticmethod
        def Redis(**_kw):
            return faker

    sys.modules["redis"] = _FakeRedisModule


# ── tests ────────────────────────────────────────────────────────────────


class TestChannelConstant(unittest.TestCase):
    def test_default_channel_is_sim_score(self):
        self.assertEqual(sp.SCORE_CHANNEL, "sim:score")


class TestPayloadShape(unittest.TestCase):
    """Cover ``_build_payload`` — the format-only contract."""

    def test_build_payload_preserves_evaluator_fields(self):
        score = {
            "profile": "uav_search_track_car",
            "total_score": 75.4,
            "passed": True,
            "dimension_scores": {"search": 80.0, "completion": 60.0},
            "n_targets": 2,
            "n_completed": 1,
        }
        payload = sp._build_payload(score, sim_time=5.0, tick=10)
        self.assertEqual(payload["profile"], "uav_search_track_car")
        self.assertEqual(payload["total_score"], 75.4)
        self.assertTrue(payload["passed"])
        self.assertEqual(
            payload["dimension_scores"],
            {"search": 80.0, "completion": 60.0},
        )
        self.assertEqual(payload["n_targets"], 2)
        self.assertEqual(payload["n_completed"], 1)
        self.assertEqual(payload["type"], "score")
        self.assertFalse(payload["final"])
        self.assertEqual(payload["sim_time"], 5.0)
        self.assertEqual(payload["tick"], 10)
        self.assertIsInstance(payload["ts"], float)

    def test_build_payload_final_marks_type(self):
        score = {"total_score": 88.0, "passed": True, "dimension_scores": {}}
        payload = sp._build_payload(score, final=True)
        self.assertEqual(payload["type"], "score_final")
        self.assertTrue(payload["final"])

    def test_build_payload_does_not_mutate_caller(self):
        score = {"total_score": 10.0, "dimension_scores": {}}
        sp._build_payload(score, sim_time=1.0, tick=1)
        self.assertNotIn("type", score)
        self.assertNotIn("final", score)
        self.assertNotIn("sim_time", score)
        self.assertNotIn("tick", score)
        self.assertNotIn("ts", score)


class TestPublishDelivery(unittest.TestCase):
    """Cover the ``publish`` and ``publish_final`` wire delivery."""

    def setUp(self):
        self.fake = _FakeRedisClient()
        _install_fake_redis(self.fake)
        self.publisher = sp.ScorePublisher(host="127.0.0.1", port=6379)

    def tearDown(self):
        # Drop our fake redis so other tests get the real module back.
        sys.modules.pop("redis", None)
        self.publisher.close()

    def test_publish_emits_to_sim_score(self):
        score = {
            "profile": "uav_search_track_car",
            "total_score": 75.4,
            "passed": False,
            "dimension_scores": {"search": 80.0},
        }
        ok = self.publisher.publish(score, sim_time=2.0, tick=20)
        self.assertTrue(ok)
        self.assertEqual(len(self.fake.published), 1)
        channel, body = self.fake.published[0]
        self.assertEqual(channel, "sim:score")
        parsed = json.loads(body)
        self.assertEqual(parsed["profile"], "uav_search_track_car")
        self.assertEqual(parsed["total_score"], 75.4)
        self.assertEqual(parsed["type"], "score")
        self.assertFalse(parsed["final"])
        self.assertEqual(parsed["sim_time"], 2.0)
        self.assertEqual(parsed["tick"], 20)

    def test_publish_final_includes_evaluation_path(self):
        ok = self.publisher.publish_final(
            {"total_score": 87.0, "passed": True, "dimension_scores": {}},
            sim_time=120.0,
            tick=1200,
            evaluation_path="output/run_x.evaluation.json",
        )
        self.assertTrue(ok)
        _, body = self.fake.published[0]
        parsed = json.loads(body)
        self.assertEqual(parsed["type"], "score_final")
        self.assertTrue(parsed["final"])
        self.assertEqual(
            parsed["evaluation_path"], "output/run_x.evaluation.json"
        )

    def test_close_calls_underlying_client(self):
        self.publisher.close()
        self.assertTrue(self.fake.closed)

    def test_close_is_idempotent(self):
        self.publisher.close()
        self.publisher.close()  # must not raise
        self.assertTrue(self.fake.closed)


class TestPublishFailureTolerance(unittest.TestCase):
    """Redis outages must not crash the 10 Hz control loop."""

    def setUp(self):
        self.fake = _FakeRedisClient(raise_on_publish=True)
        _install_fake_redis(self.fake)
        self.publisher = sp.ScorePublisher(host="127.0.0.1", port=6379)

    def tearDown(self):
        sys.modules.pop("redis", None)
        self.publisher.close()

    def test_publish_returns_false_on_redis_error(self):
        ok = self.publisher.publish(
            {"total_score": 50.0, "dimension_scores": {}, "passed": False}
        )
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()