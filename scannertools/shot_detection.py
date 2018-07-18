from .prelude import *
from scipy.spatial import distance
import numpy as np

WINDOW_SIZE = 500

class ShotDetectionPipeline(Pipeline):
    job_suffix = 'hist'
    parser_fn = lambda _: readers.histograms

    def build_pipeline(self):
        return {'histogram': self._db.ops.Histogram(
            frame=self._sources['frame'].op,
            device=DeviceType.GPU if self._db.has_gpu() and False else DeviceType.CPU)}

    def _compute_shot_boundaries(self, hists):
        # Compute the mean difference between each pair of adjacent frames
        diffs = np.array([
            np.mean([distance.chebyshev(hists[i - 1][j], hists[i][j]) for j in range(3)])
            for i in range(1, len(hists))
        ])
        diffs = np.insert(diffs, 0, 0)
        n = len(diffs)

        # Do simple outlier detection to find boundaries between shots
        boundaries = []
        for i in range(1, n):
            window = diffs[max(i - WINDOW_SIZE, 0):min(i + WINDOW_SIZE, n)]
            if diffs[i] - np.mean(window) > 3 * np.std(window):
                boundaries.append(i)
        return boundaries

    def default_output(self):
        all_hists = super().default_output()
        return [self._compute_shot_boundaries(list(vid_hists.load())) for vid_hists in all_hists]


detect_shots = ShotDetectionPipeline.make_runner()
