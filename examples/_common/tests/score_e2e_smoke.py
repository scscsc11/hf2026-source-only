"""Spec 025 — sim:score end-to-end smoke test.

Spawns a real ``redis-server`` (or piggy-backs on the user's running
instance, if any), runs :class:`ScorePublisher` against it for a few
ticks, and asserts the JSON payload arrives on ``sim:score`` as a
subscriber would see it.

Designed for quick manual verification during development; not a unit
test (no mock). Run with:

    python -m examples._common.tests.score_e2e_smoke
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import redis  # type: ignore

from examples._common.score_publisher import ScorePublisher


def main() -> int:
    r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
    try:
        r.ping()
    except Exception as e:
        print(f"[smoke] Redis not reachable: {e}", file=sys.stderr)
        return 2

    # 1) Subscribe first, so we don't miss the early frames.
    pubsub = r.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe("sim:score")

    # 2) Publish a few ticks.
    pub = ScorePublisher(host="127.0.0.1", port=6379)
    print("[smoke] publishing 5 score frames...")
    for i in range(5):
        snap = {
            "profile": "smoke_test",
            "total_score": 50.0 + i * 5,
            "passed": False,
            "dimension_scores": {"a": 60.0, "b": 70.0},
            "tick_count": i,
        }
        pub.publish(snap, sim_time=i * 0.1, tick=i)
        time.sleep(0.02)

    # 3) Publish a final frame.
    pub.publish_final(
        {
            "profile": "smoke_test",
            "total_score": 80.0,
            "passed": True,
            "dimension_scores": {"a": 80.0, "b": 80.0},
        },
        sim_time=0.5,
        tick=5,
        evaluation_path="output/smoke.evaluation.json",
    )
    pub.close()

    # 4) Drain the subscriber and assert.
    received: list[dict] = []
    deadline = time.time() + 2.0
    while time.time() < deadline and len(received) < 6:
        msg = pubsub.get_message(timeout=0.2)
        if msg and msg.get("type") == "message":
            try:
                received.append(json.loads(msg["data"]))
            except json.JSONDecodeError as e:
                print(f"[smoke] bad JSON: {e}", file=sys.stderr)
                return 3
    pubsub.close()

    print(f"[smoke] received {len(received)} messages")
    for m in received:
        print(f"  - type={m.get('type')} total_score={m.get('total_score')} "
              f"final={m.get('final')}")

    if len(received) < 6:
        print(f"[smoke] FAIL: expected 6 messages, got {len(received)}",
              file=sys.stderr)
        return 4

    # Assertions
    assert received[0]["type"] == "score"
    assert received[0]["total_score"] == 50.0
    assert received[4]["total_score"] == 70.0
    assert received[5]["type"] == "score_final"
    assert received[5]["final"] is True
    assert received[5]["passed"] is True
    assert received[5]["evaluation_path"] == "output/smoke.evaluation.json"

    print("[smoke] OK — all 6 messages received and validated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())