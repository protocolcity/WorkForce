"""Lock the api/roster.py → workforce.api.roster package peel."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import workforce  # noqa: E402
from workforce.api import roster as roster_pkg
from workforce.api.roster import (
    constants,
    desk,
    helpers,
    law,
    models,
    pulse,
)


class RosterPackagePeelTest(unittest.TestCase):
    def test_stable_import_surface(self) -> None:
        """Callers keep importing read models from workforce.api.roster."""
        self.assertIs(roster_pkg.scene_model, roster_pkg.scene_model)
        self.assertTrue(callable(roster_pkg.scene_model))
        self.assertTrue(callable(roster_pkg.scene_tape))
        self.assertTrue(callable(roster_pkg.report_model))
        self.assertTrue(callable(roster_pkg.worker_model))
        self.assertTrue(callable(roster_pkg.generation_token))
        self.assertTrue(callable(roster_pkg.recent_failures))
        self.assertEqual(roster_pkg.scene_model.__module__, "workforce.api.roster")

    def test_boundaries(self) -> None:
        self.assertTrue(hasattr(models, "scene_model"))
        self.assertTrue(hasattr(models, "report_model"))
        self.assertTrue(hasattr(models, "worker_model"))
        self.assertTrue(hasattr(pulse, "generation_token"))
        self.assertTrue(hasattr(desk, "_desk_json"))
        self.assertTrue(hasattr(helpers, "_load_roster"))
        self.assertTrue(hasattr(law, "_worker_health"))
        self.assertTrue(hasattr(constants, "DEFAULT_PORT"))
        self.assertFalse(hasattr(constants, "scene_model"))
        self.assertFalse(hasattr(pulse, "scene_model"))

    def test_city_folder_path_pin(self) -> None:
        """_city_folder_name still walks from the checkout root, not api/roster/."""
        src = constants._city_folder_name.__code__.co_names
        self.assertIn("workforce", src)
        hood = os.path.abspath(os.path.join(os.path.dirname(workforce.__file__), ".."))
        self.assertTrue(os.path.isdir(hood))

    def test_monkeypatch_surface_is_package(self) -> None:
        """Adopted functions look up collaborators on the package."""
        self.assertIs(
            roster_pkg.scene_model.__globals__.get("_load_roster"),
            roster_pkg._load_roster,
        )
        self.assertIs(
            roster_pkg._worker_queue.__globals__.get("_http_get_json"),
            roster_pkg._http_get_json,
        )


if __name__ == "__main__":
    unittest.main()
