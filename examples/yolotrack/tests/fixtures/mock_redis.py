"""Mock Redis：模拟 spec/022 相机帧 push。

仅用于单测，不做生产。
"""
from __future__ import annotations

from typing import Iterator


class MockRedis:
    """最简版：hset/hgetall/scan_iter。"""

    def __init__(self):
        self._hashes: dict[bytes, dict[bytes, bytes]] = {}

    def hset(self, key: bytes | str, field: bytes | str, value: bytes | str) -> int:
        if isinstance(key, str): key = key.encode()
        if isinstance(field, str): field = field.encode()
        if isinstance(value, str): value = value.encode()
        self._hashes.setdefault(key, {})[field] = value
        return 1

    def hgetall(self, key: bytes | str) -> dict[bytes, bytes]:
        if isinstance(key, str): key = key.encode()
        return dict(self._hashes.get(key, {}))

    def scan_iter(self, match: str = "*", count: int = 100) -> Iterator[bytes]:
        pattern = match.replace("*", "")
        for key in list(self._hashes.keys()):
            if pattern in key.decode():
                yield key

    def ping(self):
        return True

    def publish_camera_frame(
        self, uav_id: str, frame_no: int, image_bytes: bytes, sim_time: float = 0.0,
    ) -> None:
        key = f"sync_camera:{uav_id}:frame:{frame_no}".encode()
        self.hset(key, b"image", image_bytes)
        self.hset(key, b"sim_time", str(sim_time).encode())
