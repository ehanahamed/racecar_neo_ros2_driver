"""Standalone pit_node launch — owns /dev/neo-pit-pcb (Teensy UART); watchdog restart target."""

from racecar_neo_ros2_driver.launch_common import single_node_launch


def generate_launch_description():
    return single_node_launch(
        arg_name='pit_config',
        default_yaml='pit.yaml',
        package='racecar_neo_ros2_driver',
        executable='pit_node',
    )
