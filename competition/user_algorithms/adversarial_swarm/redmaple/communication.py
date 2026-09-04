"""RedMaple communication protocol."""

from typing import Optional


class CommunicationManager:
    """Encode/decode lightweight distributed swarm messages."""

    def encode_target(self, target):
        return (
            f"T:{target.target_id},{target.lat:.6f},{target.lon:.6f},"
            f"{target.confidence:.3f},{target.state}"
        )

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
        except Exception:
            return None
        return None
