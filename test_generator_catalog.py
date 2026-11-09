"""Tests for workbook metadata matching and technology permissions."""

import unittest

from generator_catalog import resolve_generator, technology_is_allowed


class GeneratorCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {
            100: [{
                "mpid": "WND1",
                "facility_name": "Wind One",
                "bus": 100,
                "area": 17,
                "region": "Northwest",
                "technology": "WIND",
            }],
            200: [
                {
                    "mpid": "SC1",
                    "facility_name": "Simple Cycle",
                    "bus": 200,
                    "area": 49,
                    "region": "Southwest",
                    "technology": "SC",
                },
                {
                    "mpid": "SUN1",
                    "facility_name": "Solar One",
                    "bus": 200,
                    "area": 49,
                    "region": "Southwest",
                    "technology": "SOLAR",
                },
            ],
        }

    def test_renewable_can_be_selected_down_but_not_up(self):
        metadata = resolve_generator(self.catalog, 100, "WND1")
        self.assertTrue(technology_is_allowed(metadata, []))
        self.assertFalse(technology_is_allowed(metadata, ["CC", "SC"]))

    def test_shared_bus_is_conservative_without_machine_id(self):
        metadata = resolve_generator(self.catalog, 200, "")
        self.assertTrue(metadata["ambiguous"])
        self.assertFalse(technology_is_allowed(metadata, ["SC"]))
        self.assertTrue(technology_is_allowed(metadata, ["SC", "SOLAR"]))

    def test_mpid_in_psse_name_resolves_shared_bus(self):
        metadata = resolve_generator(self.catalog, 200, "SC1 UNIT")
        self.assertEqual(metadata["technology"], "SC")
        self.assertTrue(technology_is_allowed(metadata, ["SC"]))

    def test_unmapped_policy_is_explicit(self):
        metadata = resolve_generator(self.catalog, 999, "UNKNOWN")
        self.assertFalse(technology_is_allowed(metadata, [], allow_unmapped=False))
        self.assertTrue(technology_is_allowed(metadata, [], allow_unmapped=True))


if __name__ == "__main__":
    unittest.main()
