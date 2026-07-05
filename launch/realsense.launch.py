"""Intel RealSense D435i as the RACECAR Neo v2 forward camera."""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    ExecuteProcess,
    IncludeLaunchDescription,
    TimerAction,
)
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import SetRemap


def generate_launch_description():
    realsense_dir = get_package_share_directory('realsense2_camera')

    pointcloud_arg = DeclareLaunchArgument(
        'pointcloud_enable',
        default_value='false',
        description='Enable point cloud generation (CPU intensive on Pi 5)'
    )

    align_depth_arg = DeclareLaunchArgument(
        'align_depth_enable',
        default_value='false',
        description='Align depth frames to color camera'
    )

    depth_profile_arg = DeclareLaunchArgument(
        'depth_profile',
        default_value='640x480x15',
        description='Depth and infrared stream profile (widthxheightxfps)'
    )

    color_profile_arg = DeclareLaunchArgument(
        'color_profile',
        default_value='640x480x15',
        description='Color stream profile (widthxheightxfps)'
    )

    # Fix IMU IIO permissions before launching the camera node.
    # The D435i IMU uses HID-sensor IIO devices whose sysfs attributes
    # default to root-only on the Pi 5.
    fix_imu_permissions = ExecuteProcess(
        cmd=['sudo', '/usr/local/bin/fix-realsense-imu.sh'],
        output='screen',
    )

    # Include the stock realsense launch with UAV Neo defaults (delayed
    # slightly to allow the permission fix to complete)
    realsense_launch = TimerAction(
        period=1.0,
        actions=[
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    os.path.join(realsense_dir, 'launch', 'rs_launch.py')
                ),
                launch_arguments={
                    'camera_namespace': '/',
                    'camera_name': 'camera',
                    # Streams
                    'enable_depth': 'true',
                    'enable_color': 'true',
                    'enable_infra1': 'false',
                    'enable_infra2': 'false',
                    'enable_gyro': 'true',
                    'enable_accel': 'true',
                    # Profiles
                    'depth_module.depth_profile':
                        LaunchConfiguration('depth_profile'),
                    'rgb_camera.color_profile':
                        LaunchConfiguration('color_profile'),
                    'depth_module.infra_profile':
                        LaunchConfiguration('depth_profile'),
                    'gyro_fps': '200',
                    'accel_fps': '63',
                    'unite_imu_method': '2',
                    # Sync and alignment
                    'enable_sync': 'true',
                    'align_depth.enable':
                        LaunchConfiguration('align_depth_enable'),
                    # Filters
                    'decimation_filter.enable': 'true',
                    'spatial_filter.enable': 'true',
                    'temporal_filter.enable': 'true',
                    # Point cloud
                    'pointcloud.enable':
                        LaunchConfiguration('pointcloud_enable'),
                    # TF
                    'publish_tf': 'true',
                    'tf_publish_rate': '0.0',
                    # Diagnostics
                    'diagnostics_period': '1.0',
                }.items(),
            ),
        ],
    )

    return LaunchDescription([
        pointcloud_arg,
        align_depth_arg,
        depth_profile_arg,
        color_profile_arg,
        fix_imu_permissions,
        # Publish the color stream as /camera/forward, the forward-camera topic
        # edgetpu_node and the student library (camera_real.py) already read, so
        # replacing the Logitech BRIO with the RealSense needs no downstream
        # change. SetRemap applies to the node inside the included rs_launch.
        # VERIFY on the Pi that this propagates; if not, fall back to a
        # topic relay or a direct realsense2_camera_node with remappings.
        SetRemap(src='/camera/color/image_raw', dst='/camera/forward'),
        realsense_launch,
    ])
