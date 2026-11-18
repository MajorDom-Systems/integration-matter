"""The controller's matter-server client, in the order matterjs-server sends events."""

from unittest.mock import Mock

from aiohttp import ClientSession
from matter_server.common.models import EventMessage, EventType, SuccessResultMessage

from majordom_matter.client import _fix_message
from majordom_matter.controller import MatterClient
from majordom_matter.testing import load_on_off_light_node


def test_an_attribute_update_may_come_before_its_node():
    client = MatterClient("ws://matter-server/ws", Mock(spec=ClientSession))
    node = load_on_off_light_node()
    client._nodes[node.node_id] = node
    updated = []
    client.subscribe_events(lambda event, data: updated.append(data), EventType.ATTRIBUTE_UPDATED)

    # a node just commissioned, announced only after its first attributes
    client._handle_event_message(EventMessage(EventType.ATTRIBUTE_UPDATED, [node.node_id + 1, "0/40/5", "new"]))
    client._handle_event_message(EventMessage(EventType.ATTRIBUTE_UPDATED, [node.node_id, "0/40/5", "known"]))

    assert updated == ["known"]


def test_an_empty_feature_map_is_a_number():
    node = {"node_id": 7, "attributes": {"1/4294048800/65532": {}, "1/6/65532": 1}}

    assert _fix_message(SuccessResultMessage("id", [node])).result[0]["attributes"] == {
        "1/4294048800/65532": 0,
        "1/6/65532": 1,
    }
    assert _fix_message(EventMessage(EventType.NODE_UPDATED, node)).data["attributes"]["1/4294048800/65532"] == 0
    assert _fix_message(EventMessage(EventType.ATTRIBUTE_UPDATED, [7, "1/4294048800/65532", {}])).data[2] == 0
