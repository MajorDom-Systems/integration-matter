from typing import override

from matter_server.client import MatterClient as _MatterClient
from matter_server.common.models import EventMessage, EventType


class MatterClient(_MatterClient):
    @override
    def _handle_event_message(self, msg: EventMessage) -> None:
        # matterjs-server may report a just-commissioned node's attributes before announcing the node (whose
        # announcement carries them). Upstream raises KeyError on those, which ends listening and cancels the
        # commissioning call that's still waiting for its result.
        if msg.event == EventType.ATTRIBUTE_UPDATED and msg.data[0] not in self._nodes:
            return
        super()._handle_event_message(msg)
