"""Standalone throttle_node launch (watchdog restart target)."""

import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    pkg_dir = get_package_share_directory('racecar_neo_ros2_driver')
    default_cfg = os.path.join(pkg_dir, 'config', 'throttle.yaml')

    cfg_arg = DeclareLaunchArgument(
        'throttle_config',
        default_value=default_cfg,
        description='Path to throttle_node config YAML',
    )

    throttle = Node(
        package='racecar_neo_ros2_driver',
        executable='throttle_node',
        name='throttle_node',
        output='screen',
        parameters=[LaunchConfiguration('throttle_config')],
    )

    return LaunchDescription([cfg_arg, throttle])
