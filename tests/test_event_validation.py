import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "backend"))

import pytest
from pydantic import ValidationError
from app import Event


def test_valid_enter_event():
    event = Event(camera_id="entrance", event_type="enter")
    assert event.event_type == "enter"


def test_invalid_event_type():
    with pytest.raises(ValidationError):
        Event(camera_id="entrance", event_type="wrong_event")


def test_table_event_requires_table_id():
    with pytest.raises(ValidationError):
        Event(
            camera_id="wide",
            event_type="table_occupied",
            meta={"people": 2}
        )


def test_room_count_requires_people():
    with pytest.raises(ValidationError):
        Event(camera_id="wide", event_type="room_count", meta={})


def test_invalid_timestamp():
    with pytest.raises(ValidationError):
        Event(
            camera_id="entrance",
            event_type="enter",
            timestamp="not-a-date"
        )
