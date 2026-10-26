"""Smoke tests — no matterjs-server or binaries required (run everywhere)."""

from majordom_integration_sdk import Service
from majordom_integration_sdk.controller import AbstractController
from majordom_integration_sdk.testing import build_test_platform

import majordom_matter
from majordom_matter import MatterController


def test_is_an_integration_controller():
    assert issubclass(MatterController, AbstractController)


def test_integration_identity_is_class_level():
    assert MatterController.name == "Matter"
    assert MatterController.slug() == "matter"


def test_runs_its_controller_with_the_matterjs_server_it_needs():
    server, controller = majordom_matter.peripherals(build_test_platform())
    assert isinstance(server, Service) and server.name == "matterjs-server"
    assert isinstance(controller, MatterController)
    assert majordom_matter.peripherals(build_test_platform())[0] is not server  # one of its own per runtime
