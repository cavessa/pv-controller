"""Settings-Formular: Zahlenfelder dürfen gespeicherte Werte nicht blockieren (z. B. Kapazität 8640 Wh)."""
import re
import unittest
from pathlib import Path

import web


class SettingsStepTest(unittest.TestCase):
    def test_no_coarse_step_on_integer_fields(self):
        html = (Path(web.PROJECT_ROOT) / "web" / "index.html").read_text(encoding="utf-8")
        coarse = [m.group(0) for m in re.finditer(r'<input type="number"[^>]*>', html)
                  if (st := re.search(r'step="([0-9.]+)"', m.group(0))) and float(st.group(1)) > 1]
        self.assertEqual(coarse, [])


if __name__ == "__main__":
    unittest.main()
