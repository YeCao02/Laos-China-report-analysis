from __future__ import annotations

import unittest

from laos_china_corpus.archives.commoncrawl_pasaxon_targeted import _capture_distance


class CommonCrawlPasaxonTargetedTests(unittest.TestCase):
    def test_prefers_closest_post_publication_capture(self):
        choices = ["20210301000000", "20210402000000", "20210501000000"]
        ordered = sorted(choices, key=lambda value: _capture_distance(value, "2021-03-31"))
        self.assertEqual(ordered, ["20210402000000", "20210501000000", "20210301000000"])


if __name__ == "__main__":
    unittest.main()
