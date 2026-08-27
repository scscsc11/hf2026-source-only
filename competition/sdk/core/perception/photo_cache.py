"""PhotoCache — 后台线程拉取 redis sync_camera 帧 (spec 029).

复用 visualization/src/bridge/frame-reader.ts 的逻辑（Python 版）：
SCAN sync_camera:{uid}:frame:* → 取最大 frame_no → HGET image。
每 uid 一个 daemon 线程，~30Hz 轮询；get(uid) 非阻塞返回最新缓存。
"""
from __future__ import annotations

import re
import threading
from typing import Dict, Optional, Protocol


class _RedisLike(Protocol):
    """redis-py 的最小子集（FakeRedis 和真 redis.Redis 都满足）。"""
    def hget(self, key: str, field: str): ...
    def scan_iter(self, match: str): ...


_FRAME_RE = re.compile(r"frame:(\d+)$")


class PhotoCache:
    """后台拉取每架 UAV 最新相机帧 PNG bytes。

    Args:
        redis_client: 已连接的 redis 客户端（或 FakeRedis）。
        uids: 需要拉帧的实体 uid 列表。
        poll_interval_s: 轮询间隔（秒）。
    """

    def __init__(self, redis_client: _RedisLike, uids,
                 poll_interval_s: float = 0.033) -> None:
        self._redis = redis_client
        self._uids = list(uids)
        self._poll_interval_s = poll_interval_s
        self._cache: Dict[str, Optional[bytes]] = {uid: None for uid in self._uids}
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._threads:
            return   # 已在运行，幂等
        self._stop_event.clear()
        for uid in self._uids:
            t = threading.Thread(target=self._loop, args=(uid,), daemon=True,
                                 name=f"PhotoCache-{uid}")
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads.clear()

    def get(self, uid: str) -> Optional[bytes]:
        """返回 uid 最新帧 bytes 或 None（非阻塞）。"""
        return self._cache.get(uid)

    def _loop(self, uid: str) -> None:
        while not self._stop_event.is_set():
            try:
                self._poll_once(uid)
            except Exception:
                pass   # 瞬时错误不杀线程，下一轮重试
            self._stop_event.wait(self._poll_interval_s)

    def _poll_once(self, uid: str) -> None:
        """拉取 uid 的最新帧（同步，便于单测）。"""
        pattern = f"sync_camera:{uid}:frame:*"
        best_no = -1
        best_key = None
        for key in self._redis.scan_iter(pattern):
            key_str = key.decode() if isinstance(key, (bytes, bytearray)) else str(key)
            m = _FRAME_RE.search(key_str)
            if m:
                no = int(m.group(1))
                if no > best_no:
                    best_no, best_key = no, key
        if best_key is None:
            return
        image = self._redis.hget(best_key, "image")
        if image is not None:
            self._cache[uid] = image
