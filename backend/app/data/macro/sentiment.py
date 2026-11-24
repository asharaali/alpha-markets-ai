"""Fear gauge. CNN's index blocks automated clients, so it is not used; the crypto Fear & Greed
index (alternative.me, a documented free API) and the VIX quote stand in."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict

from app.core.http import make_client


@dataclass
class Sentiment:
    name: str
    value: int
    label: str
    as_of: int
    kind: str = "index"
    source: str = "alternative.me"

    def to_dict(self):
        return asdict(self)


def parse_alternative(payload: Dict[str, Any]) -> Sentiment:
    d = payload["data"][0]
    return Sentiment(name="Crypto Fear & Greed", value=int(d["value"]),
                     label=d["value_classification"], as_of=int(d["timestamp"]))


async def fetch() -> Sentiment:
    async with make_client(timeout=20) as c:
        r = await c.get("https://api.alternative.me/fng/", params={"limit": 1})
        r.raise_for_status()
        return parse_alternative(r.json())
