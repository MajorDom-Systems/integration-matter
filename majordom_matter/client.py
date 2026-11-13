from typing import override

from matter_server.client import MatterClient as _MatterClient
from matter_server.common.models import EventMessage, EventType, SuccessResultMessage

_FEATURE_MAP = "/65532"  # the attribute path's ending: global attribute 0xFFFC


def _fix_empty_feature_maps(attributes: dict):
    # matterjs-server writes a feature map with no feature set as {} (a bitmap object) for the custom clusters
    # of some devices, where upstream expects the number: parsing it ends listening
    for path, value in attributes.items():
        if path.endswith(_FEATURE_MAP) and value == {}:
            attributes[path] = 0


def _fix_message(message):
    match message:
        case SuccessResultMessage(result=list(nodes)):
            for node in nodes:
                if isinstance(node, dict):
                    _fix_empty_feature_maps(node.get("attributes", {}))
        case EventMessage(event=EventType.NODE_ADDED | EventType.NODE_UPDATED, data=dict(node)):
            _fix_empty_feature_maps(node.get("attributes", {}))
        case EventMessage(event=EventType.ATTRIBUTE_UPDATED, data=[_, path, {}]) if path.endswith(_FEATURE_MAP):
            message.data[2] = 0
    return message


class MatterClient(_MatterClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        receive = self.connection.receive_message_or_raise

        async def receive_fixed():
            return _fix_message(await receive())

        self.connection.receive_message_or_raise = receive_fixed  # ty: ignore[invalid-assignment]

    @override
    def _handle_event_message(self, msg: EventMessage):
        # matterjs-server may report a just-commissioned node's attributes before announcing the node (whose
        # announcement carries them). Upstream raises KeyError on those, which ends listening and cancels the
        # commissioning call that's still waiting for its result.
        if msg.event == EventType.ATTRIBUTE_UPDATED and msg.data[0] not in self._nodes:
            return
        super()._handle_event_message(msg)
