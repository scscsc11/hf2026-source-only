"""RedMaple RC2 communication protocol."""

from typing import Optional


class CommunicationManager:
    def encode_target(self, target):
        return (
            f"T:{target.target_id},{target.lat:.6f},{target.lon:.6f},"
            f"{target.confidence:.3f},{target.state}"
        )

    def encode_claim(self, target_id, uid):
        return f"C:{target_id},{uid}"

    def encode_release(self, target_id, uid):
        return f"R:{target_id},{uid}"

    def decode(self, payload: str) -> Optional[dict]:
        try:
            if payload.startswith("T:"):
                data = payload[2:].split(",")
                return {
                    "type": "target",
                    "id": data[0],
                    "lat": float(data[1]),
                    "lon": float(data[2]),
                    "confidence": float(data[3]),
                    "state": data[4] if len(data) > 4 else "UNKNOWN",
                }
            if payload.startswith("C:"):
                data = payload[2:].split(",")
                return {"type": "claim", "id": data[0], "uid": data[1]}
            if payload.startswith("R:"):
                data = payload[2:].split(",")
                return {"type": "release", "id": data[0], "uid": data[1]}
        except Exception:
            return None
        return None
