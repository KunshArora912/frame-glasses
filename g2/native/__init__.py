"""Even Realities G2 -- native firmware protocol (dashboard, settings, menu, notifications,
Even AI, native apps), transport-agnostic, standard library only.

Sources: ``g2/docs/native-protocol.md`` and the open-g2-hub Dart port it cites
(``g2/vendor/open-g2-hub/lib/protocol/*.dart``).  See ``README.md`` for the module map, the
confidence of every feature and the live-verification checklist.

Quick start::

    from g2.native import framing, session, transport
    t = transport.GatewayRawTransport("http://127.0.0.1:8765")
    async with transport.Glasses(t) as g:
        plan = session.MinimalDashboardPlan(g.alloc, temperature_c=21, weather_type=1, weather_status="Sunny")
        await g.run_plan(plan)
        g.start_keepalive()
"""
from . import apps, dashboard, even_ai, framing, menu, notifications, pb, session, settings, transport  # noqa: F401
from .framing import FLAG_PLAIN, FLAG_REQUEST, FrameAssembler, MagicAllocator, auth_frame, crc16, frame_pb, parse_frame  # noqa: F401
from .session import MinimalDashboardPlan, Step  # noqa: F401
from .transport import FakeTransport, GatewayRawTransport, Glasses, Transport  # noqa: F401

__all__ = [
    "apps", "dashboard", "even_ai", "framing", "menu", "notifications", "pb", "session", "settings", "transport",
    "FLAG_PLAIN", "FLAG_REQUEST", "FrameAssembler", "MagicAllocator", "auth_frame", "crc16", "frame_pb", "parse_frame",
    "MinimalDashboardPlan", "Step", "FakeTransport", "GatewayRawTransport", "Glasses", "Transport",
]
__version__ = "0.1.0"
