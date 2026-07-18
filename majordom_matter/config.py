import os

# The MajorDom Matter integration talks to a python-matter-server instance over WebSocket.
# Point it at yours via MATTER_SERVER_URL; the default matches a `matter-server` service on
# the same Docker network.
matter_server_url = os.environ.get("MATTER_SERVER_URL", "ws://matter-server:5580/ws")
