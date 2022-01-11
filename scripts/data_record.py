import logging
import math
import os
import sys
import time
from argparse import ArgumentParser
from collections import OrderedDict

import h5py
import numpy as np
import yaml

if sys.version_info[0] >= 3:
    from queue import Empty, Full, Queue
else:
    from Queue import Empty, Full, Queue

import message_filters
from msg_synchronizer import ApproximateTimeSynchronizer, TimeSynchronizer
import ros_numpy
import rospy
from ambf_msgs.msg import RigidBodyState
from cv_bridge import CvBridge, CvBridgeError
from sensor_msgs.msg import Image, PointCloud2
from utils import init_ambf


def depth_gen(depth_msg):
    """
    generate depth
    :param depth_msg:
    :return: HxW, z-values
    """
    xyz_array = ros_numpy.point_cloud2.pointcloud2_to_array(depth_msg)
    xcol = xyz_array['x'][:, None] * scale
    ycol = xyz_array['y'][:, None] * scale
    zcol = xyz_array['z'][:, None] * scale

    scaled_depth = np.concatenate([xcol, ycol, zcol], axis=-1)
    # halve precision to save storage
    scaled_depth = scaled_depth.astype(np.float16)
    # reverse height direction due to AMBF reshaping
    scaled_depth = np.ascontiguousarray(scaled_depth.reshape([h, w, 3])[::-1])
    # convert to cv convention
    scaled_depth = np.einsum(
        'ab,hwb->hwa', extrinsic[:3, :3], scaled_depth)[..., -1]

    return scaled_depth


def image_gen(image_msg):
    try:
        cv2_img = bridge.imgmsg_to_cv2(image_msg, "bgr8")
        return cv2_img
    except CvBridgeError as e:
        print(e)
        return None


def pose_gen(pose_msg):
    pose = pose_msg.pose
    pose_np = np.array([
        pose.position.x * scale,
        pose.position.y * scale,
        pose.position.z * scale,
        pose.orientation.x,
        pose.orientation.y,
        pose.orientation.z,
        pose.orientation.w
    ])

    return pose_np


def init_hdf5(args, stereo):
    adf = args.world_adf

    world_adf = open(adf, "r")
    world_params = yaml.safe_load(world_adf)
    world_adf.close()

    s = world_params["conversion factor"]
    main_camera = world_params["main_camera"]

    # perspective camera intrinsics
    fva = main_camera["field view angle"]
    img_height = main_camera["publish image resolution"]["height"]
    img_width = main_camera["publish image resolution"]["width"]

    focal = img_height / (2 * math.tan(fva / 2))
    c_x = img_width / 2
    c_y = img_height / 2
    intrinsic = np.array([[focal, 0, c_x], [0, focal, c_y], [0, 0, 1]])

    # Create hdf5 file with date
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)
    time_str = time.strftime("%Y%m%d_%H%M%S")
    file = h5py.File("./data/" + time_str + ".hdf5", "w")

    metadata = file.create_group("metadata")
    metadata.create_dataset("camera_intrinsic", data=intrinsic)
    metadata.create_dataset("camera_extrinsic", data=extrinsic)
    metadata.create_dataset("README", data="All position information is in meters unless specified otherwise. \n"
                                           "Quaternion is a list in the order of [qx, qy, qz, qw]. \n"
                                           "Poses are defined to be T_world_obj. \n"
                                           "Depth in CV convention (corrected by extrinsic, T_cv_ambf). \n")

    # baseline info from stereo adf
    if stereo:
        adf = args.stereo_adf
        stereo_adf = open(adf, "r")
        stereo_params = yaml.safe_load(stereo_adf)
        baseline = math.fabs(
            stereo_params['stereoL']['location']['y'] - stereo_params['stereoR']['location']['y']) * s
        metadata.create_dataset("baseline", data=baseline)

    file.create_group("data")

    return file, img_height, img_width, s


def callback(*inputs):
    """
    Current implementation strictly enforces the ordering
    ordering - l_img, depth, r_img, segm, pose_A, pose_B, ..., data_keys

    :param inputs:
    :return:
    """
    log.log(logging.DEBUG, "msg callback")

    keys = list(inputs[-1])
    data = dict(time=inputs[0].header.stamp.to_sec())

    if num_data % 5 == 0:
        print("Recording data: " + '#' * (num_data // 10), end='\r')

    for idx, key in enumerate(keys[1:]):  # skip time
        if 'l_img' == key or 'r_img' == key or 'segm' == key:
            data[key] = image_gen(inputs[idx])
        if 'depth' == key:
            # print("depth")
            data[key] = depth_gen(inputs[idx])
        if 'pose_' in key:
            # print("pose")
            data[key] = pose_gen(inputs[idx])

    try:
        data_queue.put_nowait(data)
    except Full:
        log.log(logging.DEBUG, "Queue full")


def write_to_hdf5():
    data_group = f["data"]
    for key, value in container.items():
        if len(value) > 0:
            data_group.create_dataset(
                key, data=np.stack(value, axis=0))  # write to disk
            log.log(logging.INFO, (key, f["data"][key].shape))
        container[key] = []  # reset list to empty memory
    f.close()

    return


def timer_callback(_):
    log.log(logging.DEBUG, "timer callback")
    try:
        data_dict = data_queue.get_nowait()
    except Empty:
        log.log(logging.NOTSET, "Empty queue")
        return

    global num_data, f
    for key, data in data_dict.items():
        container[key].append(data)

    num_data = num_data + 1
    if num_data >= chunk:
        log.log(logging.INFO, "Write data to disk")
        write_to_hdf5()
        f, _, _, _ = init_hdf5(args, stereo)
        num_data = 0


def setup_subscriber(args):
    active_topics = [n for [n, _] in rospy.get_published_topics()]
    subscribers = []
    topics = []

    if len(active_topics) <= 2:
        log.log(logging.WARNING, 'Launch simulation before recording!')
        exit()

    if args.stereoL_topic != 'None':
        if args.stereoL_topic in active_topics:
            stereoL_sub = message_filters.Subscriber(args.stereoL_topic, Image)
            subscribers += [stereoL_sub]
            container['l_img'] = []
            topics += [args.stereoL_topic]
        else:
            print("Failed to subscribe to", args.stereoL_topic)

    if args.depth_topic != 'None':
        if args.depth_topic in active_topics:
            depth_sub = message_filters.Subscriber(args.depth_topic, PointCloud2)
            subscribers += [depth_sub]
            container['depth'] = []
            topics += [args.depth_topic]
        else:
            print("Failed to subscribe to", args.depth_topic)

    if args.stereoR_topic != 'None':
        if args.stereoR_topic in active_topics:
            stereoR_sub = message_filters.Subscriber(args.stereoR_topic, Image)
            subscribers += [stereoR_sub]
            container['r_img'] = []
            topics += [args.stereoR_topic]
        else:
            print("Failed to subscribe to", args.stereoR_topic)

    if args.segm_topic != 'None':
        if args.segm_topic in active_topics:
            segm_sub = message_filters.Subscriber(args.segm_topic, Image)
            subscribers += [segm_sub]
            container['segm'] = []
            topics += [args.segm_topic]
        else:
            print("Failed to subscribe to", args.segm_topic)

    # poses
    for name, _ in objects.items():
        subname = name
        if 'camera' in name:
            if name == 'main_camera':
                subname = 'cameras/' + name
            else:
                continue
        topic = '/ambf/env/' + subname + '/State'
        pose_sub = message_filters.Subscriber(topic, RigidBodyState)

        if topic in active_topics:
            container['pose_' + name] = []
            subscribers += [pose_sub]
            topics += [topic]
        else:
            print("Failed to subscribe to", topic)

    log.log(logging.INFO, '\n'.join(["Subscribed to the following topics:"] + topics))
    return subscribers


def main(args):
    container['time'] = []

    subscribers = setup_subscriber(args)

    print("Synchronous? : ", args.sync)
    # NOTE: don't set queue size to a large number (e.g. 1000).
    # Otherwise, the time taken to compute synchronization becomes very long and no more message will be spit out.
    if args.sync is False:
        ats = ApproximateTimeSynchronizer(subscribers, queue_size=50, slop=0.01)
        ats.registerCallback(callback, container.keys())
    else:
        ats = TimeSynchronizer(subscribers, queue_size=50)
        ats.registerCallback(callback, container.keys())

    # separate thread for writing to hdf5 to release memory
    rospy.Timer(rospy.Duration(0, 500000), timer_callback)  # set to 2Khz such that we don't miss pose data
    print("Writing to HDF5 every chunk of %d data" % args.chunk_size)

    try:
        print("Recording started, press Q to quit")

        while not rospy.core.is_shutdown():
            rospy.rostime.wallsleep(0.5)
            keypress = input("")
            if keypress == "Q" or keypress == "q":
                break
    except KeyboardInterrupt:
        rospy.core.signal_shutdown('keyboard interrupt')

    write_to_hdf5()  # save when user exits


def verify_cv_bridge():
    arr = np.zeros([480, 640])
    msg = bridge.cv2_to_imgmsg(arr)
    try:
        bridge.imgmsg_to_cv2(msg)
    except ImportError:
        log.log(logging.WARNING, "libcv_bridge.so: cannot open shared object file. Please source ros env first.")
        return False

    return True


if __name__ == '__main__':
    parser = ArgumentParser()

    parser.add_argument(
        '--output_dir', default='data', type=str)

    parser.add_argument(
        '--world_adf', default='../ADF/world/world.yaml', type=str)
    parser.add_argument(
        '--stereo_adf', default='../ADF/stereo_cameras.yaml', type=str)

    parser.add_argument('--chunk_size', type=int, default=200,
                        help='Write to disk every chunk size')

    parser.add_argument(
        '--stereoL_topic', default='/ambf/env/cameras/stereoL/ImageData', type=str)
    parser.add_argument(
        '--depth_topic', default='/ambf/env/cameras/segmentation_camera/DepthData', type=str)
    parser.add_argument(
        '--stereoR_topic', default='/ambf/env/cameras/stereoR/ImageData', type=str)
    parser.add_argument(
        '--segm_topic', default='/ambf/env/cameras/segmentation_camera/ImageData', type=str)
    parser.add_argument(
        '--sync', type=str, default='True')
    parser.add_argument('--debug', action='store_true')

    # TODO: record voxels (either what has been removed, or what is the current voxels) as asked by pete?

    args = parser.parse_args()

    # init cv bridge for data conversion
    bridge = CvBridge()
    valid = verify_cv_bridge()
    if not valid:
        exit()

    # init logger
    log = logging.getLogger('logger')
    log.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(message)s')
    ch = logging.StreamHandler()
    if args.debug:
        ch.setLevel(logging.DEBUG)
    else:
        ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    log.addHandler(ch)

    # read object groups
    _client, objects = init_ambf('data_record')
    chunk = args.chunk_size

    # camera extrinsics, the transformation that pre-multiplies recorded poses to match opencv convention
    extrinsic = np.array([[0, 1, 0, 0], [0, 0, -1, 0],
                          [-1, 0, 0, 0], [0, 0, 0, 1]])  # T_cv_ambf

    # check topics and see if we need to read stereo adf for baseline
    if args.stereoL_topic is not None and args.stereoR_topic is not None:
        stereo = True
    else:
        stereo = False
    f, h, w, scale = init_hdf5(args, stereo)

    data_queue = Queue(chunk * 2)
    num_data = 0
    container = OrderedDict()

    if args.sync in ['True', 'true', '1']:
        args.sync = True
    else:
        args.sync = False

    main(args)
    _client.clean_up()
