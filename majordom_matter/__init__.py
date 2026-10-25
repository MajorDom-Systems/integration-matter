"""Matter integration for MajorDom.

Bridges Matter devices into the MajorDom language via python-matter-server.
`peripherals` is what the Hub (or the SDK's standalone dev runner) runs: matterjs-server and the
`MatterController` that drives it.
"""

from majordom_integration_sdk import Peripheral, Platform

from majordom_matter.config import matterjs_server
from majordom_matter.controller import MatterController


def peripherals(platform: Platform) -> list[Peripheral]:
    return [matterjs_server(), MatterController(platform)]


__all__ = ["MatterController", "peripherals"]
