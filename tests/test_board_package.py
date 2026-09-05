"""Lock the board.py → workforce.board package peel."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import board as board_pkg
from workforce.board import handler, paths, server


class BoardPackagePeelTest(unittest.TestCase):
    def test_stable_import_surface(self) -> None:
        self.assertTrue(callable(board_pkg.make_server))
        self.assertTrue(callable(board_pkg.serve))
        self.assertTrue(callable(board_pkg.scene_model))
        self.assertTrue(callable(board_pkg.worker_model))
        self.assertTrue(hasattr(board_pkg, "_Handler"))
        self.assertTrue(board_pkg.API_ONLY)
        self.assertEqual(board_pkg.make_server.__module__, "workforce.board")
        self.assertEqual(board_pkg._Handler.__module__, "workforce.board")

    def test_boundaries(self) -> None:
        self.assertTrue(hasattr(paths, "_days_param"))
        self.assertTrue(hasattr(handler, "_Handler"))
        self.assertTrue(hasattr(server, "make_server"))
        self.assertFalse(hasattr(paths, "_Handler"))
        self.assertFalse(hasattr(server, "do_GET"))

    def test_monkeypatch_surface_is_package(self) -> None:
        """Adopted handler methods look up collaborators on the package."""
        do_get = board_pkg._Handler.do_GET
        self.assertIs(do_get.__globals__.get("_load_roster"), board_pkg._load_roster)
        self.assertIs(do_get.__globals__.get("_worker_queue"), board_pkg._worker_queue)
        self.assertIs(board_pkg.make_server.__globals__.get("_Handler"), board_pkg._Handler)


if __name__ == "__main__":
    unittest.main()
