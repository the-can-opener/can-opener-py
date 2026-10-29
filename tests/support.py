from can_opener.transport.mock import MAX_MONITOR_IDS, MockTransport

# Historical name used by the test-suite; the library's MockTransport now
# provides the same scripted-response and frame-injection surface.
FixtureTransport = MockTransport

__all__ = ["MAX_MONITOR_IDS", "FixtureTransport", "MockTransport"]
