"""
Phone push notifications via ntfy.sh — free, no account, works on iOS + Android.

Setup: install the 'ntfy' app, subscribe to your topic (settings.NTFY_TOPIC).
The backend POSTs to https://ntfy.sh/<topic> and it lands on your phone instantly.
"""
from __future__ import annotations
import httpx
from app.config import settings


def send_push(message: str, title: str = "Alpha Markets AI",
              priority: str = "default", tags=None, topic: str = None) -> bool:
    """priority: min|low|default|high|urgent. topic: per-user ntfy topic (falls back to default)."""
    topic = topic or settings.NTFY_TOPIC
    if not settings.NOTIFY_ENABLED or not topic:
        return False
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        httpx.post(f"https://ntfy.sh/{topic}",
                   data=message.encode("utf-8"), headers=headers, timeout=10)
        return True
    except Exception as exc:
        print(f"[notify] push failed: {exc}")
        return False
