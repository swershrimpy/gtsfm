"""Factor-graph based formulation of Bundle adjustment and optimization.

Authors: Xiaolong Wu, John Lambert, Ayush Baid
"""
import os
from pathlib import Path

import dask
from dask.delayed import Delayed
from gtsam import (
    GeneralSFMFactor2Cal3Bundler,
    LevenbergMarquardtOptimizer,
    LevenbergMarquardtParams,
    NonlinearFactorGraph,
    PinholeCameraCal3Bundler,
    PriorFactorCal3Bundler,
    PriorFactorPose3,
    SfmTrack,
    Values,
    symbol_shorthand,
)
from gtsam.noiseModel import Isotropic

import gtsfm.utils.io as io_utils
import gtsfm.utils.logger as logger_utils
from gtsfm.common.gtsfm_data import GtsfmData

METRICS_PATH = Path(__file__).resolve().parent.parent.parent / "result_metrics"

"""In this file, we use the GTSAM's GeneralSFMFactor2 instead of GeneralSFMFactor because Factor2 enables decoupling
of the camera pose and the camera intrinsics, and hence gives an option to share the intrinsics between cameras.
"""

# TODO: any way this goes away?
P = symbol_shorthand.P  # 3d point
X = symbol_shorthand.X  # camera pose
K = symbol_shorthand.K  # calibration

CAM_POSE3_DOF = 6  # 6 dof for pose of camera
CAM_CAL3BUNDLER_DOF = 3  # 3 dof for f, k1, k2 for intrinsics of camera
IMG_MEASUREMENT_DIM = 2  # 2d measurements (u,v) have 2 dof
POINT3_DOF = 3  # 3d points have 3 dof


# noise model params
CAM_POSE3_PRIOR_NOISE_SIGMA = 0.1
CAM_CAL3BUNDLER_PRIOR_NOISE_SIGMA = 0.1
MEASUREMENT_NOISE_SIGMA = 1.0  # in pixels

logger = logger_utils.get_logger()


class BundleAdjustmentOptimizer:
    """Bundle adjustment using factor-graphs in GTSAM.

    This class refines global pose estimates and intrinsics of cameras, and also refines 3D point cloud structure given
    tracks from triangulation."""

    def __init__(self, output_reproj_error_thresh: float, shared_calib: bool = False):
        """Initializes the parameters for bundle adjustment module.

        Args:
            output_reproj_error_thresh: the max reprojection error allowed in output.
            shared_calib (optional): Flag to enable shared calibration across
                                     all cameras. Defaults to False.
        """
        self._output_reproj_error_thresh = output_reproj_error_thresh
        self._shared_calib = shared_calib

    def __add_camera_prior(
        self, graph: NonlinearFactorGraph, camera: PinholeCameraCal3Bundler, camera_idx: int,
    ) -> None:
        """Add a prior factor in the factor graph for the camera parameters.

        Args:
            graph: factor graph for the problem.
            camera: the camera object to add to graph and to initial values.
            camera_idx: index of the camera.
        """
        graph.push_back(
            PriorFactorPose3(X(camera_idx), camera.pose(), Isotropic.Sigma(CAM_POSE3_DOF, CAM_POSE3_PRIOR_NOISE_SIGMA))
        )

        # process just one camera if shared calibration; otherwise process all
        if camera_idx == 0 or not self._shared_calib:
            graph.push_back(
                PriorFactorCal3Bundler(
                    K(camera_idx),
                    camera.calibration(),
                    Isotropic.Sigma(CAM_CAL3BUNDLER_DOF, CAM_CAL3BUNDLER_PRIOR_NOISE_SIGMA),
                )
            )

    def __add_camera_initial_value(
        self, initial_values: Values, camera: PinholeCameraCal3Bundler, camera_idx: int,
    ) -> None:
        """Add initial values for the camera parameters.

        Args:
            initial_values: object holding the initial values for camera parameters and 3d points.
            camera: the camera object to add to graph and to initial values.
            camera_idx: index of the camera.
        """
        # add initial value for pose
        initial_values.insert(X(camera_idx), camera.pose())

        # process just one camera if shared calibration; otherwise process all
        if camera_idx == 0 or not self._shared_calib:
            initial_values.insert(K(camera_idx), camera.calibration())

    def __add_measurement_factor(
        self, graph: NonlinearFactorGraph, track: SfmTrack, track_idx: int, measurement_noise: Isotropic,
    ) -> None:
        """Add factor for each 2D measurement in the track.

        Args:
            graph: factor graph for the problem.
            track: the track to be added.
            track_idx: index of the track to be added.
            measurement_noise: noise associated with the factor.
        """

        for k in range(track.number_measurements()):
            # i represents the camera index, and uv is the 2d measurement
            i, uv = track.measurement(k)

            # add the factor, using the 3D point, camera, and the associated 2d measurement
            graph.add(
                GeneralSFMFactor2Cal3Bundler(
                    uv, measurement_noise, X(i), P(track_idx), K(0 if self._shared_calib else i),
                )
            )

    def run(self, initial_data: GtsfmData) -> GtsfmData:
        """Run the bundle adjustment by forming factor graph and optimizing using Levenberg–Marquardt optimization.

        Args:
            initial_data: initialized cameras, tracks w/ their 3d landmark from triangulation.

        Results:
            optimized camera poses, 3D point w/ tracks, and error metrics.
        """
        logger.info(
            "Input: %d tracks on %d cameras\n",
            initial_data.number_tracks(),
            len(initial_data.get_valid_camera_indices()),
        )

        # noise model for measurements -- one pixel in u and v
        measurement_noise = Isotropic.Sigma(IMG_MEASUREMENT_DIM, MEASUREMENT_NOISE_SIGMA)

        # Create a factor graph
        graph = NonlinearFactorGraph()

        # Create initial estimate
        initial_values = Values()

        # adding factors for measurements and 3D points's initial values
        for j in range(initial_data.number_tracks()):
            track = initial_data.get_track(j)
            self.__add_measurement_factor(graph, track, j, measurement_noise)
            initial_values.insert(P(j), track.point3())

        # add prior and initial values for cameras
        for i in initial_data.get_valid_camera_indices():
            initialized_cam = initial_data.get_camera(i)

            self.__add_camera_prior(graph, initialized_cam, i)
            self.__add_camera_initial_value(initial_values, initialized_cam, i)

        # Optimize the graph and print results
        try:
            params = LevenbergMarquardtParams()
            params.setVerbosityLM("ERROR")
            lm = LevenbergMarquardtOptimizer(graph, initial_values, params)
            result_values = lm.optimize()
        except Exception:
            logger.exception("LM Optimization failed")
            # as we did not perform the bundle adjustment, we skip computing the total reprojection error
            return GtsfmData(initial_data.number_images())

        initial_error = graph.error(initial_values)
        final_error = graph.error(result_values)

        # Error drops from ~2764.22 to ~0.046
        logger.info("initial error: %.2f", initial_error)
        logger.info("final error: %.2f", final_error)

        # construct the results
        optimized_data = values_to_gtsfm_data(result_values, initial_data, self._shared_calib)

        metrics_dict = {}
        metrics_dict["before_filtering"] = optimized_data.aggregate_metrics()
        logger.info("[Result] Number of tracks before filtering: %d", metrics_dict["before_filtering"]["number_tracks"])

        # filter the largest errors
        filtered_result = optimized_data.filter_landmarks(self._output_reproj_error_thresh)

        metrics_dict["after_filtering"] = filtered_result.aggregate_metrics()
        io_utils.save_json_file(os.path.join(METRICS_PATH, "bundle_adjustment_metrics.json"), metrics_dict)

        logger.info("[Result] Number of tracks after filtering: %d", metrics_dict["after_filtering"]["number_tracks"])
        logger.info("[Result] Mean track length %.3f", metrics_dict["after_filtering"]["3d_track_lengths"]["mean"])
        logger.info("[Result] Median track length %.3f", metrics_dict["after_filtering"]["3d_track_lengths"]["median"])
        filtered_result.log_scene_reprojection_error_stats()

        return filtered_result

    def create_computation_graph(self, sfm_data_graph: Delayed) -> Delayed:
        """Create the computation graph for performing bundle adjustment.

        Args:
            sfm_data_graph: an GtsfmData object wrapped up using dask.delayed

        Returns:
            GtsfmData wrapped up using dask.delayed
        """
        return dask.delayed(self.run)(sfm_data_graph)


def values_to_gtsfm_data(values: Values, initial_data: GtsfmData, shared_calib: bool) -> GtsfmData:
    """Cast results from the optimization to GtsfmData object.

    Args:
        values: results of factor graph optimization.
        initial_data: data used to generate the factor graph; used to extract information about poses and 3d points in
                      the graph.

    Returns:
        optimized poses and landmarks.
    """
    result = GtsfmData(initial_data.number_images())

    # add cameras
    for i in initial_data.get_valid_camera_indices():
        result.add_camera(
            i, PinholeCameraCal3Bundler(values.atPose3(X(i)), values.atCal3Bundler(K(0 if shared_calib else i)),)
        )

    # add tracks
    for j in range(initial_data.number_tracks()):
        input_track = initial_data.get_track(j)

        # populate the result with optimized 3D point
        result_track = SfmTrack(values.atPoint3(P(j)))

        for measurement_idx in range(input_track.number_measurements()):
            i, uv = input_track.measurement(measurement_idx)
            result_track.add_measurement(i, uv)

        result.add_track(result_track)

    return result
