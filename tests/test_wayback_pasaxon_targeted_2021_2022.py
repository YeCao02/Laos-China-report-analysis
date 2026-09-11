from __future__ import annotations

import unittest

from laos_china_corpus.archives.wayback import WaybackCapture
from laos_china_corpus.archives.wayback_pasaxon_targeted_2021_2022 import choose_capture


class WaybackPasaxonTargetedTests(unittest.TestCase):
    def test_capture_selection_prefers_closest_post_publication(self):
        captures = [
            WaybackCapture("20210401000000", "http://x/1"),
            WaybackCapture("20210501000000", "http://x/1"),
            WaybackCapture("20210428000000", "http://x/1"),
        ]
        self.assertEqual(choose_capture(captures, "2021-04-27").timestamp, "20210428000000")
        self.assertEqual(choose_capture(captures, "2021-04-27", rank=2).timestamp, "20210501000000")


if __name__ == "__main__":
    unittest.main()
