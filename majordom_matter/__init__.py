"""Matter integration for MajorDom.

Bridges Matter devices into the MajorDom language via python-matter-server.
`MatterController` is the entry point the Hub (or the SDK's standalone dev runner)
instantiates and drives.
"""

from majordom_matter.controller import MatterController

__all__ = ["MatterController"]
