"""All eight boolean combinations map to the route the matrix specifies."""
import unittest

from graphrag.routing import Route, derive_route, route_for


class DeriveRouteTest(unittest.TestCase):
    CASES = [
        ((False, False, False), Route.NONE),
        ((True,  False, False), Route.SCHEMA_ONLY),
        ((False, True,  False), Route.DATA_DIRECT),
        ((True,  True,  False), Route.DATA_WITH_DISCOVERY),
        ((False, False, True),  Route.METADATA_ONLY),
        ((True,  False, True),  Route.METADATA_WITH_DISCOVERY),
        ((False, True,  True),  Route.MIXED_DIRECT),
        ((True,  True,  True),  Route.MIXED_WITH_DISCOVERY),
    ]

    def test_every_combination(self):
        for flags, expected in self.CASES:
            with self.subTest(flags=flags):
                self.assertEqual(derive_route(*flags), expected)

    def test_all_eight_routes_are_reachable(self):
        produced = {derive_route(*flags) for flags, _ in self.CASES}
        self.assertEqual(produced, set(Route))

    def test_route_serialises_as_its_name(self):
        import json
        self.assertEqual(
            json.dumps({"route": derive_route(True, True, False)}),
            '{"route": "DATA_WITH_DISCOVERY"}')

    def test_missing_flags_read_as_false(self):
        # An extraction may omit a key; None must not miss the table.
        self.assertEqual(derive_route(None, None, None), Route.NONE)
        self.assertEqual(derive_route(None, True, None), Route.DATA_DIRECT)

    def test_route_for_reads_an_extraction(self):
        class Stub:
            requires_schema_discovery = True
            requires_record_query = True
            requires_metadata_context = True
        self.assertEqual(route_for(Stub()), Route.MIXED_WITH_DISCOVERY)

    def test_route_for_defaults_when_attributes_absent(self):
        self.assertEqual(route_for(object()), Route.NONE)


if __name__ == "__main__":
    unittest.main()
