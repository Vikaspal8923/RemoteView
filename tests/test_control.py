import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from shared.control import ControlMessageError, decode_control_message, make_control


def test_control_messages_roundtrip_and_validation():
    assert decode_control_message(make_control("MOUSE_MOVE", x=0.25, y=0.75)).type == "MOUSE_MOVE"
    assert decode_control_message(make_control("TYPE_TEXT", text="hello")).data["text"] == "hello"


def test_control_rejects_unsafe_or_invalid_messages():
    with pytest.raises(ControlMessageError):
        decode_control_message(b'{"type":"SHELL","command":"whoami"}\n')
    with pytest.raises(ControlMessageError):
        decode_control_message(make_control("MOUSE_MOVE", x=2, y=0.5))
