import unittest
from unittest.mock import patch

import ocr


class Geometry(unittest.TestCase):
    def test_actual_resize_dimensions_and_fractional_box(self):
        import cv2
        import numpy as np
        original = np.zeros((101, 103, 3), dtype=np.uint8)
        box = [[1.8, 2.4], [45.2, 2.4], [45.2, 44.7], [1.8, 44.7]]
        def engine(img):
            self.assertEqual(img.shape[:2], (45, 46))
            return [[box, "test", .99]], None
        with patch.object(cv2, "imread", return_value=original), \
             patch.object(ocr, "_engine_rapid", return_value=engine):
            rows = ocr._rapid_rows("unused.png", .5, .45)
        self.assertEqual((rows[0]["x"], rows[0]["y"], rows[0]["w"], rows[0]["h"]),
                         (53, 53, 98, 96))

    def test_boxes_clipped_to_original_and_degenerate_discarded(self):
        import cv2
        import numpy as np
        original = np.zeros((101, 103, 3), dtype=np.uint8)
        boxes = [
            [[-5, -6], [60, -6], [60, 60], [-5, 60]],
            [[100, 100], [101, 100], [101, 101], [100, 101]],
        ]
        with patch.object(cv2, "imread", return_value=original), \
             patch.object(ocr, "_engine_rapid", return_value=lambda img: (
                 [[b, "test", .99] for b in boxes], None)):
            rows = ocr._rapid_rows("unused.png", .5, .45)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["x"], rows[0]["y"], rows[0]["w"], rows[0]["h"]),
                         (51, 50, 103, 101))

    def test_scale_one_preserves_box(self):
        import cv2
        import numpy as np
        box = [[10, 20], [30, 20], [30, 40], [10, 40]]
        with patch.object(cv2, "imread", return_value=np.zeros((100, 100, 3), dtype=np.uint8)), \
             patch.object(ocr, "_engine_rapid", return_value=lambda img: ([[box, "test", .99]], None)):
            row = ocr._rapid_rows("unused.png", .5, 1)[0]
        self.assertEqual((row["x"], row["y"], row["w"], row["h"]), (20, 30, 20, 20))


if __name__ == "__main__":
    unittest.main()
