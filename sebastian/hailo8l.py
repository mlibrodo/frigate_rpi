import logging
import os
import urllib.request

import numpy as np

try:
    from hailo_platform import (
        HEF,
        ConfigureParams,
        FormatType,
        HailoRTException,
        HailoStreamInterface,
        InferVStreams,
        InputVStreamParams,
        OutputVStreamParams,
        VDevice,
    )
except ModuleNotFoundError:
    pass

from pydantic import BaseModel, Field
from typing_extensions import Literal

from frigate.detectors.detection_api import DetectionApi
from frigate.detectors.detector_config import BaseDetectorConfig

# Set up logging
logger = logging.getLogger(__name__)

# Define the detector key for Hailo
DETECTOR_KEY = "hailo8l"


# Configuration class for model settings
class ModelConfig(BaseModel):
    path: str = Field(default=None, title="Model Path")  # Path to the HEF file


# Configuration class for Hailo detector
class HailoDetectorConfig(BaseDetectorConfig):
    type: Literal[DETECTOR_KEY]  # Type of the detector
    device: str = Field(default="PCIe", title="Device Type")  # Device type (e.g., PCIe)


# Hailo detector class implementation
class HailoDetector(DetectionApi):
    type_key = DETECTOR_KEY  # Set the type key to the Hailo detector key

    def __init__(self, detector_config: HailoDetectorConfig):
        # Initialize device type and model path from the configuration
        self.h8l_device_type = detector_config.device
        self.h8l_model_path = detector_config.model.path
        self.h8l_model_height = detector_config.model.height
        self.h8l_model_width = detector_config.model.width
        self.h8l_model_type = detector_config.model.model_type
        self.h8l_tensor_format = detector_config.model.input_tensor
        self.h8l_pixel_format = detector_config.model.input_pixel_format
        self.model_url = "https://hailo-model-zoo.s3.eu-west-2.amazonaws.com/ModelZoo/Compiled/v2.11.0/hailo8l/ssd_mobilenet_v1.hef"
        self.cache_dir = "/config/model_cache/h8l_cache"
        self.expected_model_filename = "ssd_mobilenet_v1.hef"
        output_type = "FLOAT32"

        logger.info(f"Initializing Hailo device as {self.h8l_device_type}")
        self.check_and_prepare_model()
        try:
            # Validate device type
            if self.h8l_device_type not in ["PCIe", "M.2"]:
                raise ValueError(f"Unsupported device type: {self.h8l_device_type}")

            # Initialize the Hailo device
            self.target = VDevice()
            # Load the HEF (Hailo's binary format for neural networks)
            self.hef = HEF(self.h8l_model_path)
            # Create configuration parameters from the HEF
            self.configure_params = ConfigureParams.create_from_hef(
                hef=self.hef, interface=HailoStreamInterface.PCIe
            )
            # Configure the device with the HEF
            self.network_groups = self.target.configure(self.hef, self.configure_params)
            self.network_group = self.network_groups[0]
            self.network_group_params = self.network_group.create_params()

            # Create input and output virtual stream parameters
            self.input_vstream_params = InputVStreamParams.make(
                self.network_group,
                format_type=self.hef.get_input_vstream_infos()[0].format.type,
            )
            self.output_vstream_params = OutputVStreamParams.make(
                self.network_group, format_type=getattr(FormatType, output_type)
            )

            # Get input and output stream information from the HEF
            self.input_vstream_info = self.hef.get_input_vstream_infos()
            self.output_vstream_info = self.hef.get_output_vstream_infos()

            logger.info("Hailo device initialized successfully")
            logger.debug(f"[__init__] Model Path: {self.h8l_model_path}")
            logger.debug(f"[__init__] Input Tensor Format: {self.h8l_tensor_format}")
            logger.debug(f"[__init__] Input Pixel Format: {self.h8l_pixel_format}")
            logger.debug(f"[__init__] Input VStream Info: {self.input_vstream_info[0]}")
            logger.debug(
                f"[__init__] Output VStream Info: {self.output_vstream_info[0]}"
            )
        except HailoRTException as e:
            logger.error(f"HailoRTException during initialization: {e}")
            raise
        except Exception as e:
            logger.error(f"Failed to initialize Hailo device: {e}")
            raise

    def check_and_prepare_model(self):
        # Ensure cache directory exists
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)

        # Check for the expected model file
        model_file_path = os.path.join(self.cache_dir, self.expected_model_filename)
        if not os.path.isfile(model_file_path):
            logger.info(
                f"A model file was not found at {model_file_path}, Downloading one from {self.model_url}."
            )
            urllib.request.urlretrieve(self.model_url, model_file_path)
            logger.info(f"A model file was downloaded to {model_file_path}.")
        else:
            logger.info(
                f"A model file already exists at {model_file_path} not downloading one."
            )

        # Always set the actual HEF path we will load
        self.h8l_model_path = model_file_path

    def detect_raw(self, tensor_input):
        logger.debug("[detect_raw] Entering function")
        logger.debug(
            f"[detect_raw] The `tensor_input` = {tensor_input} tensor_input shape = {tensor_input.shape}"
        )

        if tensor_input is None:
            raise ValueError(
                "[detect_raw] The 'tensor_input' argument must be provided"
            )

        # --- Normalize tensor shape and crop to model size ---
        exp_h = int(self.h8l_model_height)
        exp_w = int(self.h8l_model_width)

        # Drop batch dim if present: (1,H,W,C) -> (H,W,C)
        if hasattr(tensor_input, "ndim") and tensor_input.ndim == 4 and tensor_input.shape[0] == 1:
            tensor_input = tensor_input[0]

        # If CHW (C,H,W), convert to HWC (H,W,C)
        if (
            hasattr(tensor_input, "ndim")
            and tensor_input.ndim == 3
            and tensor_input.shape[0] in (1, 3)
            and tensor_input.shape[-1] not in (1, 3)
        ):
            tensor_input = np.transpose(tensor_input, (1, 2, 0))

        # Now expect HWC
        h = int(tensor_input.shape[0])
        w = int(tensor_input.shape[1])

        if (h, w) != (exp_h, exp_w):
            if h >= exp_h and w >= exp_w:
                y0 = (h - exp_h) // 2
                x0 = (w - exp_w) // 2
                tensor_input = tensor_input[y0:y0 + exp_h, x0:x0 + exp_w]
            else:
                raise ValueError(f"[detect_raw] Input too small {h}x{w} for expected {exp_h}x{exp_w}")

        tensor_input = np.ascontiguousarray(tensor_input)
        # --- End normalize/crop ---

        # Ensure tensor_input is a numpy array
        if isinstance(tensor_input, list):
            tensor_input = np.array(tensor_input)
            logger.debug(
                f"[detect_raw] Converted tensor_input to numpy array: shape {tensor_input.shape}"
            )

        input_data = tensor_input
        logger.debug(
            f"[detect_raw] Input data for inference shape: {tensor_input.shape}, dtype: {tensor_input.dtype}"
        )

        try:
            with InferVStreams(
                self.network_group,
                self.input_vstream_params,
                self.output_vstream_params,
            ) as infer_pipeline:
                input_dict = {}
                if isinstance(input_data, dict):
                    input_dict = input_data
                    logger.debug("[detect_raw] it a dictionary.")
                elif isinstance(input_data, (list, tuple)):
                    for idx, layer_info in enumerate(self.input_vstream_info):
                        input_dict[layer_info.name] = input_data[idx]
                        logger.debug("[detect_raw] converted from list/tuple.")
                else:
                    if len(input_data.shape) == 3:
                        input_data = np.expand_dims(input_data, axis=0)
                        logger.debug("[detect_raw] converted from an array.")
                    input_dict[self.input_vstream_info[0].name] = input_data

                logger.debug(
                    f"[detect_raw] Input dictionary for inference keys: {input_dict.keys()}"
                )

                # --- FINAL FIX: enforce model input size on the actual vstream input ---
                exp_h = int(self.h8l_model_height)
                exp_w = int(self.h8l_model_width)

                in_name = self.input_vstream_info[0].name
                arr = input_dict[in_name]

                # If someone passed a dict-of-arrays earlier, pick the correct one
                # (input_dict[in_name] should be the array, but this makes it bulletproof)
                if isinstance(arr, dict):
                    arr = arr[in_name]

                # Drop batch dim if present: (1,H,W,C) -> (H,W,C)
                if hasattr(arr, "ndim") and arr.ndim == 4 and arr.shape[0] == 1:
                    arr = arr[0]

                # CHW -> HWC
                if hasattr(arr, "ndim") and arr.ndim == 3 and arr.shape[0] in (1, 3) and arr.shape[-1] not in (1, 3):
                    arr = np.transpose(arr, (1, 2, 0))

                # Now we expect HWC
                h = int(arr.shape[0])
                w = int(arr.shape[1])

                # Center-crop if needed (e.g., 320x320 -> 300x300)
                if (h, w) != (exp_h, exp_w):
                    if h >= exp_h and w >= exp_w:
                        y0 = (h - exp_h) // 2
                        x0 = (w - exp_w) // 2
                        arr = arr[y0:y0 + exp_h, x0:x0 + exp_w]
                    else:
                        raise ValueError(f"[detect_raw] Input too small {h}x{w} for expected {exp_h}x{exp_w}")

                arr = np.ascontiguousarray(arr)

                # Hailo infer in this plugin expects batch dimension
                arr = np.expand_dims(arr, axis=0)

                input_dict[in_name] = arr

                logger.debug(f"[detect_raw] FINAL input to Hailo '{in_name}': shape={arr.shape} elems={arr.size}")
                # --- END FINAL FIX ---
                with self.network_group.activate(self.network_group_params):
                    raw_output = infer_pipeline.infer(input_dict)
                    logger.debug(f"[detect_raw] Raw inference output: {raw_output}")

                    if self.output_vstream_info[0].name not in raw_output:
                        logger.error(
                            f"[detect_raw] Missing output stream {self.output_vstream_info[0].name} in inference results"
                        )
                        return np.zeros((20, 6), np.float32)

                    raw_output = raw_output[self.output_vstream_info[0].name][0]
                    logger.debug(
                        f"[detect_raw] Raw output for stream {self.output_vstream_info[0].name}: {raw_output}"
                    )

            # Process the raw output
            detections = self.process_detections(raw_output)
            if len(detections) == 0:
                logger.debug(
                    "[detect_raw] No detections found after processing. Setting default values."
                )
                return np.zeros((20, 6), np.float32)
            else:
                formatted_detections = detections
                if (
                    formatted_detections.shape[1] != 6
                ):  # Ensure the formatted detections have 6 columns
                    logger.error(
                        f"[detect_raw] Unexpected shape for formatted detections: {formatted_detections.shape}. Expected (20, 6)."
                    )
                    return np.zeros((20, 6), np.float32)
                return formatted_detections
        except HailoRTException as e:
            logger.error(f"[detect_raw] HailoRTException during inference: {e}")
            return np.zeros((20, 6), np.float32)
        except Exception as e:
            logger.error(f"[detect_raw] Exception during inference: {e}")
            return np.zeros((20, 6), np.float32)
        finally:
            logger.debug("[detect_raw] Exiting function")

    def process_detections(self, raw_detections, threshold=0.5):
        boxes, scores, classes = [], [], []
        num_detections = 0

        logger.debug(f"[process_detections] Raw detections: {raw_detections}")

        for i, detection_set in enumerate(raw_detections):
            if not isinstance(detection_set, np.ndarray) or detection_set.size == 0:
                logger.debug(
                    f"[process_detections] Detection set {i} is empty or not an array, skipping."
                )
                continue

            logger.debug(
                f"[process_detections] Detection set {i} shape: {detection_set.shape}"
            )

            for detection in detection_set:
                if detection.shape[0] == 0:
                    logger.debug(
                        f"[process_detections] Detection in set {i} is empty, skipping."
                    )
                    continue

                ymin, xmin, ymax, xmax = detection[:4]
                score = np.clip(detection[4], 0, 1)  # Use np.clip for clarity

                if score < threshold:
                    logger.debug(
                        f"[process_detections] Detection in set {i} has a score {score} below threshold {threshold}. Skipping."
                    )
                    continue

                logger.debug(
                    f"[process_detections] Adding detection with coordinates: ({xmin}, {ymin}), ({xmax}, {ymax}) and score: {score}"
                )
                boxes.append([ymin, xmin, ymax, xmax])
                scores.append(score)
                classes.append(i)
                num_detections += 1

        logger.debug(
            f"[process_detections] Boxes: {boxes}, Scores: {scores}, Classes: {classes}, Num detections: {num_detections}"
        )

        if num_detections == 0:
            logger.debug("[process_detections] No valid detections found.")
            return np.zeros((20, 6), np.float32)

        combined = np.hstack(
            (
                np.array(classes)[:, np.newaxis],
                np.array(scores)[:, np.newaxis],
                np.array(boxes),
            )
        )

        if combined.shape[0] < 20:
            padding = np.zeros(
                (20 - combined.shape[0], combined.shape[1]), dtype=combined.dtype
            )
            combined = np.vstack((combined, padding))

        logger.debug(
            f"[process_detections] Combined detections (padded to 20 if necessary): {np.array_str(combined, precision=4, suppress_small=True)}"
        )

        return combined[:20, :6]
