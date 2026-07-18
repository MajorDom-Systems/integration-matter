"""Smoke tests — no matter-server or binaries required (run everywhere)."""

from majordom_integration_sdk.controller import AbstractController

from majordom_matter import MatterController


def test_is_an_integration_controller():
    assert issubclass(MatterController, AbstractController)


def test_integration_identity_is_class_level():
    assert MatterController.name == "Matter"
    assert MatterController.slug() == "matter"
