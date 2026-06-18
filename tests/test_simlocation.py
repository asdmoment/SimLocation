import importlib.util
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT_DIR / "bin" / "simlocation.py"
SPEC = importlib.util.spec_from_file_location("simlocation_under_test", MODULE_PATH)
simlocation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(simlocation)


class SimLocationSmokeTests(unittest.TestCase):
    def test_module_exposes_core_cli_boundaries(self):
        self.assertTrue(callable(simlocation.clear_location))
        self.assertTrue(callable(simlocation.start_hold_session))
        self.assertTrue(callable(simlocation.parse_args))


if __name__ == "__main__":
    unittest.main()
