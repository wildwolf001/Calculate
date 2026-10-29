from __future__ import annotations

from app.core.event_store import EventStore
from app.core.session import SessionService


class _Registry:
    def __init__(self, default_id: str = ""):
        self.default_id = default_id


def test_default_is_resolved_at_call_time(tmp_path):
    registry = _Registry("ds_a")
    service = SessionService(
        lambda dataset_id: None,
        EventStore(tmp_path / "sessions"),
        default_dataset_id="ds_old",
        registry=registry,
    )
    assert service.current_dataset_id() == "ds_a"

    registry.default_id = "ds_b"
    assert service.current_dataset_id() == "ds_b"

    registry.default_id = ""
    assert service.current_dataset_id() == ""
