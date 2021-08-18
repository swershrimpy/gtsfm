""" Create 2D-3D data association as a precursor to Bundle Adjustment.
1. Forms feature tracks from verified correspondences and global poses.
2. Estimates 3D landmark for each track (Ransac and simple triangulation modes available)
3. Filters tracks based on reprojection error.

References: 
1. Richard I. Hartley and Peter Sturm. Triangulation. Computer Vision and Image Understanding, Vol. 68, No. 2,
   November, pp. 146–157, 1997

Authors: Sushmita Warrier, Xiaolong Wu, John Lambert
"""
import os
from collections import Counter
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import dask
import numpy as np
from dask.delayed import Delayed
from gtsam import PinholeCameraCal3Bundler, SfmTrack

import gtsfm.utils.logger as logger_utils
from gtsfm.common.gtsfm_data import GtsfmData
from gtsfm.common.keypoints import Keypoints
from gtsfm.common.sfm_track import SfmTrack2d
from gtsfm.common.image import Image
from gtsfm.data_association.point3d_initializer import Point3dInitializer, TriangulationParam, TriangulationExitCode
from gtsfm.evaluation.metrics import GtsfmMetric, GtsfmMetricsGroup

import gtsfm.utils.io as io_utils

logger = logger_utils.get_logger()


class DataAssociation(NamedTuple):
    """Class to form feature tracks; for each track, call LandmarkInitializer.

    Args:
        reproj_error_thresh: the maximum reprojection error allowed.
        min_track_len: min length required for valid feature track / min nb of supporting views required for a landmark
                       to be valid.
        mode: triangulation mode, which dictates whether or not to use robust estimation.
        min_tri_angle_deg (optional): minimum allowed angle in degrees between any two rays during triangulation. Each
                                      ray originates at a camera and shoots towards the triangulated 3d point. Small
                                      angles indicate small baselines, which are unreliable. Defaults to None (check
                                      inactive).
        num_ransac_hypotheses (optional): number of hypothesis for RANSAC-based triangulation.
        save_track_patches_viz (optional): flag to turn on saving visualization of patches for each track.
    """

    reproj_error_thresh: float
    min_track_len: int
    mode: TriangulationParam
    min_tri_angle_deg: Optional[float] = None
    num_ransac_hypotheses: Optional[int] = None
    save_track_patches_viz: Optional[bool] = False

    def __validate_track(self, sfm_track: Optional[SfmTrack]) -> bool:
        """Validate the track by checking its length."""
        return sfm_track is not None and sfm_track.number_measurements() >= self.min_track_len

    def run(
        self,
        num_images: int,
        cameras: Dict[int, PinholeCameraCal3Bundler],
        corr_idxs_dict: Dict[Tuple[int, int], np.ndarray],
        keypoints_list: List[Keypoints],
        images: Optional[List[Image]] = None,
    ) -> Tuple[GtsfmData, Dict[str, Any]]:
        """Perform the data association.

        Args:
            num_images: Number of images in the scene.
            cameras: dictionary, with image index -> camera mapping.
            corr_idxs_dict: dictionary, with key as image pair (i1,i2) and value as matching keypoint indices.
            keypoints_list: keypoints for each image.
            images: a list of all images in scene (optional and only for track patch visualization)
            viz_patch_sz: width and height of patches, if if dumping/visualizing a patch for each 2d track measurement

        Returns:
            A tuple of GtsfmData with cameras and tracks, and a GtsfmMetricsGroup with data association metrics
        """
        # generate tracks for 3D points using pairwise correspondences
        tracks_2d = SfmTrack2d.generate_tracks_from_pairwise_matches(corr_idxs_dict, keypoints_list)

        if self.save_track_patches_viz and images is not None:
            io_utils.save_track_visualizations(tracks_2d, images, save_dir=os.path.join("plots", "tracks_2d"))

        # track lengths w/o triangulation check
        track_lengths_2d = list(map(lambda x: x.number_measurements(), tracks_2d))

        logger.debug("[Data association] input number of tracks: %s", len(tracks_2d))
        logger.debug("[Data association] input avg. track length: %s", np.mean(track_lengths_2d))

        # initializer of 3D landmark for each track
        point3d_initializer = Point3dInitializer(
            track_camera_dict=cameras,
            mode=self.mode,
            reproj_error_thresh=self.reproj_error_thresh,
            min_tri_angle_deg=self.min_tri_angle_deg,
            num_ransac_hypotheses=self.num_ransac_hypotheses,
        )

        # aggregating the failure types
        triangulation_result_counter: Counter = Counter()
        per_accepted_track_avg_errors = []
        per_rejected_track_avg_errors = []

        # form GtsfmData object after triangulation
        triangulated_data = GtsfmData(num_images)

        # add all cameras
        for i, camera in cameras.items():
            triangulated_data.add_camera(i, camera)

        # add valid tracks where triangulation is successful
        for track_2d in tracks_2d:
            # triangulate and filter based on reprojection error
            sfm_track, avg_track_reproj_error, triangulation_exit_code = point3d_initializer.triangulate(track_2d)
            triangulation_result_counter[triangulation_exit_code] += 1

            if sfm_track is not None and self.__validate_track(sfm_track) and TriangulationExitCode.SUCCESS:
                triangulated_data.add_track(sfm_track)
                per_accepted_track_avg_errors.append(avg_track_reproj_error)
            elif TriangulationExitCode.EXCEEDS_REPROJ_THRESH:
                per_rejected_track_avg_errors.append(avg_track_reproj_error)

        # pick only the largest connected component
        connected_data = triangulated_data.select_largest_connected_component()
        num_accepted_tracks = connected_data.number_tracks()
        accepted_tracks_ratio = num_accepted_tracks / len(tracks_2d)

        per_accepted_track_avg_errors = np.array(per_accepted_track_avg_errors, dtype=np.float32)
        per_rejected_track_avg_errors = np.array(per_rejected_track_avg_errors, dtype=np.float32)

        mean_3d_track_length, _ = connected_data.get_track_length_statistics()
        track_lengths_3d = connected_data.get_track_lengths()

        logger.debug("[Data association] output number of tracks: %s", num_accepted_tracks)
        logger.debug("[Data association] output avg. track length: %.2f", mean_3d_track_length)

        data_assoc_metrics = GtsfmMetricsGroup(
            "data_association_metrics",
            [
                GtsfmMetric(
                    "2D_track_lengths",
                    track_lengths_2d,
                    store_full_data=False,
                    plot_type=GtsfmMetric.PlotType.HISTOGRAM,
                ),
                GtsfmMetric("accepted_tracks_ratio", accepted_tracks_ratio),
                GtsfmMetric(
                    "triangulation_success_ratio",
                    triangulation_result_counter[TriangulationExitCode.SUCCESS] / len(tracks_2d),
                ),
                GtsfmMetric(
                    "cheirality_failure_ratio",
                    triangulation_result_counter[TriangulationExitCode.CHEIRALITY_FAILURE] / len(tracks_2d),
                ),
                GtsfmMetric(
                    "small_baseline_failure_ratio",
                    triangulation_result_counter[TriangulationExitCode.SMALL_BASELINE] / len(tracks_2d),
                ),
                GtsfmMetric("num_accepted_tracks", num_accepted_tracks),
                GtsfmMetric(
                    "3d_tracks_length",
                    track_lengths_3d,
                    store_full_data=False,
                    plot_type=GtsfmMetric.PlotType.HISTOGRAM,
                ),
                GtsfmMetric("accepted_track_avg_errors_px", per_accepted_track_avg_errors, store_full_data=False),
                GtsfmMetric("rejected_track_avg_errors_px", per_rejected_track_avg_errors, store_full_data=False),
            ],
        )

        return connected_data, data_assoc_metrics

    def create_computation_graph(
        self,
        num_images: int,
        cameras: Delayed,
        corr_idxs_graph: Dict[Tuple[int, int], Delayed],
        keypoints_graph: List[Delayed],
        images_graph: Optional[Delayed] = None,
    ) -> Tuple[Delayed, Delayed]:
        """Creates a computation graph for performing data association.

        Args:
            num_images: number of images in the scene.
            cameras: list of cameras wrapped up as Delayed.
            corr_idxs_graph: dictionary of correspondence indices, each value wrapped up as Delayed.
            keypoints_graph: list of wrapped up keypoints for each image.
            images_graph: a list of all images in scene (optional and only for track patch visualization)

        Returns:
            ba_input_graph: GtsfmData object wrapped up using dask.delayed
            data_assoc_metrics_graph: dictionary with different statistics about the data
                association result
        """
        data_assoc_graph = dask.delayed(self.run)(num_images, cameras, corr_idxs_graph, keypoints_graph, images_graph)
        ba_input_graph = data_assoc_graph[0]
        data_assoc_metrics_graph = data_assoc_graph[1]

        return ba_input_graph, data_assoc_metrics_graph
