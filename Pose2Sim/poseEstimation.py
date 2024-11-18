#!/usr/bin/env python
# -*- coding: utf-8 -*-


'''
###########################################################################
## POSE ESTIMATION                                                       ##
###########################################################################

    Estimate pose from a video file or a folder of images and 
    write the results to JSON files, videos, and/or images.
    Results can optionally be displayed in real time.

    Supported models: HALPE_26 (default, body and feet), COCO_133 (body, feet, hands), COCO_17 (body)
    Supported modes: lightweight, balanced, performance (edit paths at rtmlib/tools/solutions if you 
    need nother detection or pose models)

    Optionally gives consistent person ID across frames (slower but good for 2D analysis)
    Optionally runs detection every n frames and inbetween tracks points (faster but less accurate).

    If a valid cuda installation is detected, uses the GPU with the ONNXRuntime backend. Otherwise, 
    uses the CPU with the OpenVINO backend.

    INPUTS:
    - videos or image folders from the video directory
    - a Config.toml file

    OUTPUTS:
    - JSON files with the detected keypoints and confidence scores in the OpenPose format
    - Optionally, videos and/or image files with the detected keypoints 
'''


## INIT
import os
import glob
import json
import logging
import cv2
import time
import threading
import queue
import multiprocessing

import numpy as np
from datetime import datetime
from pathlib import Path
from tqdm import tqdm
from multiprocessing import Process, Queue, shared_memory, Manager

from rtmlib import draw_skeleton
from Sports2D.Utilities.config import determine_tracker_settings, setup_pose_tracker
from Sports2D.Utilities.video_management import track_people

## AUTHORSHIP INFORMATION
__author__ = "HunMin Kim, David Pagnon"
__copyright__ = "Copyright 2021, Pose2Sim"
__credits__ = ["HunMin Kim", "David Pagnon"]
__license__ = "BSD 3-Clause License"
__version__ = "0.9.4"
__maintainer__ = "David Pagnon"
__email__ = "contact@david-pagnon.com"
__status__ = "Development"


## FUNCTIONS
def rtm_estimator(config_dict):
    '''
    Estimate pose from webcams, video files, or a folder of images, and write the results to JSON files, videos, and/or images.
    Results can optionally be displayed in real-time.

    Supported models: HALPE_26 (default, body and feet), COCO_133 (body, feet, hands), COCO_17 (body)
    Supported modes: lightweight, balanced, performance (edit paths at rtmlib/tools/solutions if you need another detection or pose models)

    Optionally gives consistent person ID across frames (slower but good for 2D analysis)
    Optionally runs detection every n frames and in between tracks points (faster but less accurate).

    If a valid CUDA installation is detected, uses the GPU with the ONNXRuntime backend. Otherwise, uses the CPU with the OpenVINO backend.

    INPUTS:
    - videos or image folders from the video directory
    - a Config.toml file

    OUTPUTS:
    - JSON files with the detected keypoints and confidence scores in the OpenPose format
    - Optionally, videos and/or image files with the detected keypoints
    '''

    # Read config
    output_dir = config_dict['project']['project_dir']
    source_dir = os.path.join(output_dir, 'videos')
    pose_dir = os.path.join(output_dir, 'pose')

    show_realtime_results = config_dict['pose'].get('show_realtime_results', False)

    vid_img_extension = config_dict['pose']['vid_img_extension']
    webcam_ids = config_dict['pose'].get('webcam_ids', [])

    overwrite_pose = config_dict['pose'].get('overwrite_pose', False)

    # Check if pose estimation has already been done
    if os.path.exists(pose_dir) and not overwrite_pose:
        logging.info('Skipping pose estimation as it has already been done. Set overwrite_pose to true in Config.toml if you want to run it again.')
        return
    elif overwrite_pose:
        logging.info('Overwriting previous pose estimation.')

    logging.info('Estimating pose...')

    # Prepare list of sources (webcams, videos, image folders)
    sources = []

    if vid_img_extension == 'webcam':
        sources.extend({'type': 'webcam', 'id': cam_id, 'path': cam_id} for cam_id in (webcam_ids if isinstance(webcam_ids, list) else [webcam_ids]))
    else:
        video_files = [str(f) for f in Path(source_dir).rglob('*' + vid_img_extension) if f.is_file()]
        sources.extend({'type': 'video', 'id': idx, 'path': video_path} for idx, video_path in enumerate(video_files))
        image_dirs = [str(f) for f in Path(source_dir).iterdir() if f.is_dir()]
        sources.extend({'type': 'images', 'id': idx, 'path': folder} for idx, folder in enumerate(image_dirs, start=len(video_files)))

    if not sources:
        raise FileNotFoundError(f'No Webcams or no media files found in {source_dir}.')

    process_functions = {}
    for source in sources:
        if source['type'] == 'webcam':
            process_functions[source['id']] = process_single_frame
        else:
            process_functions[source['id']] = process_single_frame

    logging.info(f'Processing sources: {sources}')

    # Create display queue
    manager = Manager()
    display_queue = manager.Queue()

    pose_tracker_settings = determine_tracker_settings(config_dict)

    # Initialize shared counts for each source
    shared_counts = manager.dict()
    for source in sources:
        shared_counts[source['id']] = manager.dict({'queued': 0, 'processed': 0})

    # Initialize streams
    stream_manager = StreamManager(sources, config_dict, display_queue, output_dir, process_functions, pose_tracker_settings, shared_counts)
    stream_manager.start()

    # Start display thread only if show_realtime_results is True
    display_thread = None
    if show_realtime_results:
        input_size = config_dict['pose'].get('input_size', (640, 480))
        display_thread = CombinedDisplayThread(sources, input_size, display_queue)
        display_thread.start()

    # Initialize progress bars
    progress_bars = {}
    for source in sources:
        source_id = source['id']
        desc = f"Source {source_id}"
        progress_bars[source_id] = tqdm(total=0, desc=desc, position=source_id, leave=True)

    try:
        while not stream_manager.stopped:
            for source in sources:
                source_id = source['id']
                counts = shared_counts[source_id]
                queued = counts.get('queued', 0)
                processed = counts.get('processed', 0)
                pending = queued - processed
                progress_bars[source_id].total = max(progress_bars[source_id].total, queued)
                progress_bars[source_id].n = pending
                progress_bars[source_id].refresh()
            time.sleep(0.5)

            if display_thread and display_thread.stopped:
                break
    except KeyboardInterrupt:
        logging.info("Processing interrupted by user.")
    finally:
        stream_manager.stop()
        if display_thread:
            display_thread.stop()
            display_thread.join()
        for pb in progress_bars.values():
            pb.close()
        logging.shutdown()


def process_single_frame(config_dict, frame, source_id, frame_idx, output_dirs, pose_tracker, multi_person, save_video, save_images, show_realtime_results, output_format, out_vid):
    '''
    Processes a single frame from a source.

    Args:
        config_dict (dict): Configuration dictionary.
        frame (ndarray): Frame image.
        source_id (int): Source ID.
        frame_idx (int): Frame index.
        output_dirs (tuple): Output directories.
        pose_tracker: Pose tracker object.
        multi_person (bool): Whether to track multiple persons.
        output_format (str): Output format.
        save_video (bool): Whether to save the output video.
        save_images (bool): Whether to save output images.
        show_realtime_results (bool): Whether to display results in real time.
        out_vid (cv2.VideoWriter): Video writer object.

    Returns:
        tuple: (source_id, img_show)
    '''
    output_dir, output_dir_name, img_output_dir, json_output_dir, output_video_path = output_dirs

    # Perform pose estimation on the frame
    keypoints, scores = pose_tracker(frame)

    # Tracking people IDs across frames (if needed)
    keypoints, scores, _ = track_people(
        keypoints, scores, multi_person, None, None, pose_tracker
    )

    if 'openpose' in output_format:
        json_file_path = os.path.join(json_output_dir, f'{output_dir_name}_{frame_idx:06d}.json')
        save_to_openpose(json_file_path, keypoints, scores)

    # Draw skeleton on the frame
    img_show = draw_skeleton(frame, keypoints, scores, kpt_thr=0.1)

    # Save video and images
    if save_video and out_vid is not None:
        out_vid.write(img_show)

    if save_images:
        cv2.imwrite(os.path.join(img_output_dir, f'{output_dir_name}_{frame_idx:06d}.jpg'), img_show)

    return source_id, img_show


class CombinedDisplayThread(threading.Thread):
    '''
    Thread for displaying combined images to avoid thread-safety issues with OpenCV.
    '''
    def __init__(self, sources, input_size, display_queue):
        super().__init__(daemon=True)
        self.display_queue = display_queue
        self.stopped = False
        self.sources = sources
        self.input_size = input_size
        self.window_name = "Combined Feeds"
        self.grid_size = self.calculate_grid_size(len(sources))
        self.img_placeholder = np.zeros((input_size[1], input_size[0], 3), dtype=np.uint8)
        self.frames = {source['id']: self.get_placeholder_frame(source['id'], 'Not Connected') for source in sources}
        self.source_ids = [source['id'] for source in self.sources]

    def run(self):
        while not self.stopped:
            try:
                frames_dict = self.display_queue.get(timeout=0.1)
                if frames_dict:
                    self.frames.update(frames_dict)
                    self.display_combined_image()
            except queue.Empty:
                continue

    def display_combined_image(self):
        combined_image = self.combine_frames()
        if combined_image is not None:
            cv2.imshow(self.window_name, combined_image)
            if cv2.waitKey(1) & 0xFF in [ord('q'), 27]:
                logging.info("Display window closed by user.")
                self.stopped = True

    def combine_frames(self):
        resized_frames = [cv2.resize(frame, self.input_size) if frame.shape[:2] != self.input_size else frame
                          for frame in (self.frames.get(source_id, self.img_placeholder) for source_id in self.source_ids)]
        rows = [np.hstack(resized_frames[i:i + self.grid_size[1]]) for i in range(0, len(resized_frames), self.grid_size[1])]
        return np.vstack(rows)

    def calculate_grid_size(self, num_sources):
        cols = int(np.ceil(np.sqrt(num_sources)))
        rows = int(np.ceil(num_sources / cols))
        return (rows, cols)

    def stop(self):
        self.stopped = True

    def get_placeholder_frame(self, source_id, message):
        return cv2.putText(self.img_placeholder, f'Source {source_id}: {message}', (50, self.input_size[1] // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2, cv2.LINE_AA)

    def __del__(self):
        cv2.destroyAllWindows()


class StreamManager:
    def __init__(self, sources, config_dict, display_queue, output_dir, process_functions, pose_tracker_settings, shared_counts):
        self.sources = sources
        self.config_dict = config_dict
        self.display_queue = display_queue
        self.output_dir = output_dir
        self.process_functions = process_functions
        self.stopped = False
        self.pose_tracker_settings = pose_tracker_settings
        self.shared_counts = shared_counts
        self.processes = {}

    def start(self):
        for source in self.sources:
            process = SourceProcess(
                source,
                self.config_dict,
                self.output_dir,
                self.pose_tracker_settings,
                self.display_queue,
                self.shared_counts
            )
            process.start()
            self.processes[source['id']] = process

        threading.Thread(target=self.monitor_processes, daemon=True).start()

    def monitor_processes(self):
        while not self.stopped and any(process.is_alive() for process in self.processes.values()):
            time.sleep(0.1)

    def stop(self):
        self.stopped = True
        for process in self.processes.values():
            process.stopped = True
            process.join()
        logging.info("All source processes have been stopped.")


class SourceProcess(Process):
    def __init__(self, source, config_dict, output_dir, pose_tracker_settings, display_queue, shared_counts):
        super().__init__()
        self.source = source
        self.config_dict = config_dict
        self.output_dir = output_dir
        self.pose_tracker_settings = pose_tracker_settings
        self.display_queue = display_queue
        self.shared_counts = shared_counts
        self.stopped = False

    def run(self):
        logging.basicConfig(level=logging.INFO)

        # Initialize queues for inter-process communication
        frame_queue = Queue(maxsize=10)
        result_queue = Queue()

        # Determine the number of worker processes
        num_workers = 1
        outputs = setup_capture_directories(
            self.source['path'], self.output_dir, 'to_images' in self.config_dict['project'].get('save_video', [])
        )

        # Preallocate shared memory buffers
        expected_frame_size = self.config_dict['pose'].get('input_size', (640, 480))
        expected_frame_size = expected_frame_size[0] * expected_frame_size[1] * 3  # Assuming 3 channels
        num_buffers = 10  # Adjust as needed
        available_buffers = Queue()
        shared_buffers = {}
        for i in range(num_buffers):
            unique_name = f"frame_buffer_{self.source['id']}_{i}"
            shm = shared_memory.SharedMemory(name=unique_name, create=True, size=expected_frame_size)
            shared_buffers[unique_name] = shm
            available_buffers.put(unique_name)

        # Initialize shared counters
        self.shared_counts['queued'] = 0
        self.shared_counts['processed'] = 0

        # Start worker processes
        workers = []
        for _ in range(num_workers):
            worker = WorkerProcess(
                self.config_dict,
                frame_queue,
                result_queue,
                outputs,
                available_buffers,
                shared_buffers,
                self.shared_counts
            )
            worker.start()
            workers.append(worker)

        # Start frame reader thread
        stream = GenericStream(
            self.source,
            self.config_dict,
            frame_queue,
            num_workers,
            available_buffers,
            shared_buffers,
            self.shared_counts
        )
        stream.start()

        active_workers = num_workers
        while not self.stopped:
            try:
                result = result_queue.get(timeout=1)
                if result is None:
                    # A worker has finished
                    active_workers -= 1
                    if active_workers == 0:
                        # All workers have finished
                        break
                else:
                    source_id, img_show = result
                    # Handle display
                    if self.display_queue:
                        self.display_queue.put({source_id: img_show})
            except queue.Empty:
                if not stream.is_alive() and frame_queue.empty():
                    # If stream is done and queue is empty, ensure workers are stopped
                    for _ in range(len(workers)):
                        frame_queue.put(None)
        # Clean up
        for worker in workers:
            worker.stop()
            worker.join()
        stream.stop()
        stream.join()

        # Clean up shared memory
        for shm in shared_buffers.values():
            shm.close()
            shm.unlink()

        logging.info(f"Source {self.source['id']} processing completed.")


class WorkerProcess(Process):
    def __init__(self, config_dict, frame_queue, result_queue, outputs, available_buffers, shared_buffers, shared_counts):
        super().__init__()
        self.config_dict = config_dict
        self.frame_queue = frame_queue
        self.result_queue = result_queue
        self.outputs = outputs
        self.available_buffers = available_buffers
        self.shared_buffers = shared_buffers
        self.shared_counts = shared_counts
        self.stopped = False

    def run(self):
        try:
            # Initialize the pose tracker here
            pose_tracker = setup_pose_tracker(determine_tracker_settings(self.config_dict))

            # Prepare other necessary parameters
            multi_person = self.config_dict['project'].get('multi_person', False)
            save_video = 'to_video' in self.config_dict['project'].get('save_video', [])
            save_images = 'to_images' in self.config_dict['project'].get('save_video', [])
            show_realtime_results = self.config_dict['project'].get('show_realtime_results', False)
            output_format = self.config_dict['project'].get('output_format', 'openpose')
            out_vid = None
            if save_video:
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                fps = self.config_dict['pose'].get('fps', 30)
                input_size = self.config_dict['pose'].get('input_size', (640, 480))
                H, W = input_size[1], input_size[0]
                output_video_path = self.outputs[4] 
                out_vid = cv2.VideoWriter(output_video_path, fourcc, fps, (W, H))

            while not self.stopped:
                try:
                    item = self.frame_queue.get(timeout=1)
                    if item is None:
                        logging.info("A worker has finished")
                        break
                    frame_idx, buffer_name, frame_shape, frame_dtype_str, source_id = item
                    shm = self.shared_buffers[buffer_name]
                    frame = np.ndarray(frame_shape, dtype=np.dtype(frame_dtype_str), buffer=shm.buf)
                    # Process the frame
                    result = process_single_frame(
                        self.config_dict,
                        frame,
                        source_id,
                        frame_idx,
                        self.outputs,
                        pose_tracker,
                        multi_person,
                        save_video,
                        save_images,
                        show_realtime_results,
                        output_format,
                        out_vid
                    )
                    # Release the buffer back to the available queue
                    self.available_buffers.put(buffer_name)
                    with self.shared_counts_lock():
                        self.shared_counts['processed'] += 1
                    self.result_queue.put((source_id, result[1]))
                except queue.Empty:
                    continue
            # Signal that this worker is done
            self.result_queue.put(None)
        except Exception as e:
            logging.error(f"Error in WorkerProcess: {e}")
            self.stopped = True
            self.result_queue.put(None)

    def stop(self):
        self.stopped = True

    def shared_counts_lock(self):
        return multiprocessing.Lock()


class GenericStream(Process):
    def __init__(self, source, config_dict, frame_queue, num_workers, available_buffers, shared_buffers, shared_counts):
        super().__init__(daemon=True)
        self.source = source
        self.config_dict = config_dict
        self.frame_queue = frame_queue
        self.num_workers = num_workers
        self.input_size = config_dict['pose'].get('input_size', (640, 480))
        self.image_extension = config_dict['pose']['vid_img_extension']
        self.stopped = False
        self.frame_idx = 0
        self.total_frames = 0
        self.cap = None
        self.image_files = []
        self.image_index = 0
        self.pbar = None
        self.frame_ranges = None
        self.available_buffers = available_buffers
        self.shared_buffers = shared_buffers
        self.shared_counts = shared_counts 

    def parse_frame_ranges(self, frame_ranges):
        if self.source['type'] != 'webcam':
            if len(frame_ranges) == 2 and all(isinstance(x, int) for x in frame_ranges):
                start_frame, end_frame = frame_ranges
                return set(range(start_frame, end_frame + 1))
            elif len(frame_ranges) == 0:
                return None
            else:
                return set(frame_ranges)
        else:
            return None

    def run(self):
        try:
            if self.source['type'] == 'webcam':
                self.setup_webcam()
                time.sleep(1)
            elif self.source['type'] == 'video':
                self.open_video()
                self.frame_ranges = self.parse_frame_ranges(self.config_dict['project'].get('frame_range', []))
                if self.frame_ranges:
                    self.total_frames = len(self.frame_ranges)
                else:
                    self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
                self.setup_progress_bar()
            elif self.source['type'] == 'images':
                self.load_images()
            else:
                logging.error(f"Unknown source type: {self.source['type']}")
                self.stopped = True
                return
            while not self.stopped:
                frame = self.capture_frame()
                if frame is not None:
                    frame = cv2.resize(frame, self.input_size)
                    try:
                        buffer_name = self.available_buffers.get(timeout=1)
                    except queue.Empty:
                        logging.warning("No available buffers.")
                        continue
                    shm = self.shared_buffers[buffer_name]
                    np_frame = np.ndarray(frame.shape, dtype=frame.dtype, buffer=shm.buf)
                    np.copyto(np_frame, frame)
                    item = (self.frame_idx, buffer_name, frame.shape, frame.dtype.str, self.source['id'])
                    self.frame_queue.put_nowait(item)
                    with self.shared_counts_lock():
                        self.shared_counts['queued'] += 1
                    self.frame_idx += 1
                else:
                    # Signal the workers that there are no more frames
                    for _ in range(self.num_workers):
                        self.frame_queue.put(None)
                    break
        except Exception as e:
            logging.error(f"Error in GenericStream: {e}")
            self.stopped = True

    def shared_counts_lock(self):
        return multiprocessing.Lock()

    def cleanup_shared_memory(self):
        for shm in self.shm_list:
            shm.close()
            shm.unlink()
        self.shm_list.clear()

    def setup_webcam(self):
        self.open_webcam()
        time.sleep(1)

    def open_video(self):
        self.cap = cv2.VideoCapture(self.source['path'])
        if not self.cap.isOpened():
            logging.error(f"Cannot open video file {self.source['path']}")
            self.stopped = True
            return

    def load_images(self):
        path_pattern = os.path.join(self.source['path'], f'*{self.image_extension}')
        self.image_files = sorted(glob.glob(path_pattern))
        self.total_frames = len(self.image_files)
        self.setup_progress_bar()

    def capture_frame(self):
        frame = None
        if self.source['type'] == 'webcam':
            frame = self.read_webcam_frame()
            if frame is not None:
                self.frame_idx += 1
            return frame
        elif self.source['type'] == 'video':
            ret, frame = self.cap.read()
            if not ret:
                logging.info(f"End of video {self.source['path']}")
                self.stopped = True
                if self.pbar:
                    self.pbar.close()
                return None
            if self.frame_ranges and self.frame_idx not in self.frame_ranges:
                logging.debug(f"Skipping frame {self.frame_idx} as it's not in the specified frame range.")
                self.frame_idx += 1
                return self.capture_frame()
            else:
                logging.debug(f"Reading frame {self.frame_idx} from video {self.source['path']}.")
                return frame
        elif self.source['type'] == 'images':
            if self.image_index < len(self.image_files):
                frame = cv2.imread(self.image_files[self.image_index])
                self.image_index += 1
                self.frame_idx += 1
                return frame
            else:
                self.stopped = True
                return None

    def open_webcam(self):
        self.connected = False
        try:
            self.cap = cv2.VideoCapture(int(self.source['id']), cv2.CAP_DSHOW)
            if self.cap.isOpened():
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.input_size[0])
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.input_size[1])
                logging.info(f"Webcam {self.source['id']} opened.")
                self.connected = True
            else:
                logging.error(f"Cannot open webcam {self.source['id']}.")
                self.cap = None
        except Exception as e:
            logging.error(f"Exception occurred while opening webcam {self.source['id']}: {e}")
            self.cap = None

    def read_webcam_frame(self):
        if self.cap is None or not self.cap.isOpened():
            logging.warning(f"Webcam {self.source['id']} not opened. Attempting to open...")
            self.open_webcam()
            if self.cap is None or not self.cap.isOpened():
                return None
        ret, frame = self.cap.read()
        if not ret or frame is None:
            logging.warning(f"Failed to read frame from webcam {self.source['id']}.")
            self.cap.release()
            self.cap = None
            return None
        return frame

    def stop(self):
        self.stopped = True
        if self.cap:
            self.cap.release()
        if self.pbar:
            self.pbar.close()

    def setup_progress_bar(self):
        self.pbar = tqdm(total=self.total_frames, desc=f'Processing {os.path.basename(str(self.source["path"]))}', position=self.source['id'])


def setup_capture_directories(source_path, output_dir, save_images):
    '''
    Set up output directories for saving images and JSON files.

    Returns:
        tuple: (output_dir, output_dir_name, img_output_dir, json_output_dir, output_video_path)
    '''
    if isinstance(source_path, int):
        # Handle webcam source
        current_date = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir_name = f'webcam{source_path}_{current_date}'
    else:
        output_dir_name = os.path.basename(os.path.splitext(str(source_path))[0])

    # Define the full path for the output directory
    output_dir_full = os.path.abspath(os.path.join(output_dir, "pose"))

    # Create output directories if they do not exist
    if not os.path.isdir(output_dir_full):
        os.makedirs(output_dir_full)

    # Prepare directories for images and JSON outputs
    img_output_dir = os.path.join(output_dir_full, f'{output_dir_name}_img')
    json_output_dir = os.path.join(output_dir_full, f'{output_dir_name}_json')
    if save_images and not os.path.isdir(img_output_dir):
        os.makedirs(img_output_dir)
    if not os.path.isdir(json_output_dir):
        os.makedirs(json_output_dir)

    # Define the path for the output video file
    output_video_path = os.path.join(output_dir_full, f'{output_dir_name}_pose.mp4')

    return output_dir, output_dir_name, img_output_dir, json_output_dir, output_video_path

def save_to_openpose(json_file_path, keypoints, scores):
    '''
    Save the keypoints and scores to a JSON file in the OpenPose format

    INPUTS:
    - json_file_path: Path to save the JSON file
    - keypoints: Detected keypoints
    - scores: Confidence scores for each keypoint

    OUTPUTS:
    - JSON file with the detected keypoints and confidence scores in the OpenPose format
    '''

    # Prepare keypoints with confidence scores for JSON output
    nb_detections = len(keypoints)
    detections = []
    for i in range(nb_detections):  # Number of detected people
        keypoints_with_confidence_i = []
        for kp, score in zip(keypoints[i], scores[i]):
            keypoints_with_confidence_i.extend([kp[0].item(), kp[1].item(), score.item()])
        detections.append({
            "person_id": [-1],
            "pose_keypoints_2d": keypoints_with_confidence_i,
            "face_keypoints_2d": [],
            "hand_left_keypoints_2d": [],
            "hand_right_keypoints_2d": [],
            "pose_keypoints_3d": [],
            "face_keypoints_3d": [],
            "hand_left_keypoints_3d": [],
            "hand_right_keypoints_3d": []
        })

    # Create JSON output structure
    json_output = {"version": 1.3, "people": detections}

    # Save JSON output for each frame
    with open(json_file_path, 'w') as json_file:
        json.dump(json_output, json_file)
