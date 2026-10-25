from __future__ import annotations

import json
from typing import AsyncIterator

from app.core.events import Event


def encode_event(event: Event) -> str:
    data = json.dumps(event.to_dict(), ensure_ascii=False)
    return f"event: {event.type}\ndata: {data}\n\n"


async def event_stream(events: AsyncIterator[Event]) -> AsyncIterator[str]:
    yield "event: open\ndata: {}\n\n"
    try:
        async for event in events:
            yield encode_event(event)
    except Exception as exc:  # pragma: no cover - transport-level guard
        payload = json.dumps({"message": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
        yield f"event: stream.error\ndata: {payload}\n\n"
    yield "event: done\ndata: {}\n\n"
