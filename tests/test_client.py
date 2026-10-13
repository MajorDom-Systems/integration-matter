"""The controller's matter-server client, in the order matterjs-server sends events."""

from matter_server.common.models import EventMessage, EventType

from majordom_matter.controller import MatterClient
from majordom_matter.testing import load_on_off_light_node


def test_an_attribute_update_may_come_before_its_node():
    client = MatterClient("ws://matter-server/ws", None)
    node = load_on_off_light_node()
    client._nodes[node.node_id] = node
    updated = []
    client.subscribe_events(lambda event, data: updated.append(data), EventType.ATTRIBUTE_UPDATED)

    # a node just commissioned, announced only after its first attributes
    client._handle_event_message(EventMessage(EventType.ATTRIBUTE_UPDATED, [node.node_id + 1, "0/40/5", "new"]))
    client._handle_event_message(EventMessage(EventType.ATTRIBUTE_UPDATED, [node.node_id, "0/40/5", "known"]))

    assert updated == ["known"]
