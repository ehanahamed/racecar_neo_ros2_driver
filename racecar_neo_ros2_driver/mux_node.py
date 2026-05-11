"""Command mux: gates /gamepad_drive (LB) or /drive (RB) onto /mux_out.

Timer-driven so the Maestro stays fed and the watchdog sees a steady publish
rate. Zeroes on joy disconnect or stale upstream commands (>0.5s).
"""

import time
from enum import Enum, auto

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSReliabilityPolicy, QoSProfile
from sensor_msgs.msg import Joy
from ackermann_msgs.msg import AckermannDriveStamped


class MuxMode(Enum):
    IDLE = auto()
    GAMEPAD = auto()
    AUTONOMY = auto()


class MuxNode(Node):
    def __init__(self):
        super().__init__('mux_node')

        self.declare_parameter('gamepad_enable_button', 4)
        self.declare_parameter('autonomy_enable_button', 5)
        self.declare_parameter('joystick_timeout_sec', 0.5)
        self.declare_parameter('command_timeout_sec', 0.5)
        self.declare_parameter('publish_rate_hz', 50.0)

        self._gamepad_btn = self.get_parameter('gamepad_enable_button').value
        self._auto_btn = self.get_parameter('autonomy_enable_button').value
        self._joy_timeout = self.get_parameter('joystick_timeout_sec').value
        self._cmd_timeout = self.get_parameter('command_timeout_sec').value
        publish_rate = self.get_parameter('publish_rate_hz').value

        self._latest_joy: Joy = None
        self._joy_stamp = 0.0
        self._joy_connected = False

        self._latest_gamepad: AckermannDriveStamped = None
        self._gamepad_stamp = 0.0

        self._latest_auto: AckermannDriveStamped = None
        self._auto_stamp = 0.0

        self._last_mode = MuxMode.IDLE

        qos = QoSProfile(
            depth=1,
            history=QoSHistoryPolicy.KEEP_LAST,
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            durability=QoSDurabilityPolicy.VOLATILE,
        )
        self._pub = self.create_publisher(AckermannDriveStamped, '/mux_out', qos)

        self.create_subscription(Joy, '/joy', self._joy_cb, qos)
        self.create_subscription(
            AckermannDriveStamped, '/gamepad_drive', self._gamepad_cb, qos
        )
        self.create_subscription(
            AckermannDriveStamped, '/drive', self._auto_cb, qos
        )

        self.create_timer(1.0 / publish_rate, self._publish)

        self.get_logger().info(
            f'Mux ready: gamepad btn={self._gamepad_btn}, autonomy btn={self._auto_btn}, '
            f'joy timeout={self._joy_timeout}s, cmd timeout={self._cmd_timeout}s, '
            f'rate={publish_rate}Hz'
        )

    def _joy_cb(self, msg: Joy):
        self._latest_joy = msg
        self._joy_stamp = time.monotonic()
        if not self._joy_connected:
            self._joy_connected = True
            self.get_logger().info('Controller connected')

    def _gamepad_cb(self, msg: AckermannDriveStamped):
        self._latest_gamepad = msg
        self._gamepad_stamp = time.monotonic()

    def _auto_cb(self, msg: AckermannDriveStamped):
        self._latest_auto = msg
        self._auto_stamp = time.monotonic()

    def _publish(self):
        now = time.monotonic()
        out = AckermannDriveStamped()

        joy = self._latest_joy
        if joy is None or (now - self._joy_stamp) > self._joy_timeout:
            if self._joy_connected and joy is not None:
                self._joy_connected = False
                self.get_logger().warn('Controller disconnected — publishing zero')
            self._pub.publish(out)
            self._last_mode = MuxMode.IDLE
            return

        gamepad_held = (
            len(joy.buttons) > self._gamepad_btn
            and bool(joy.buttons[self._gamepad_btn])
        )
        auto_held = (
            len(joy.buttons) > self._auto_btn
            and bool(joy.buttons[self._auto_btn])
        )
        if gamepad_held and not auto_held:
            mode = MuxMode.GAMEPAD
        elif auto_held and not gamepad_held:
            mode = MuxMode.AUTONOMY
        else:
            mode = MuxMode.IDLE

        if mode == MuxMode.GAMEPAD:
            if (
                self._latest_gamepad is not None
                and (now - self._gamepad_stamp) <= self._cmd_timeout
            ):
                out = self._latest_gamepad
        elif mode == MuxMode.AUTONOMY:
            if (
                self._latest_auto is not None
                and (now - self._auto_stamp) <= self._cmd_timeout
            ):
                out = self._latest_auto

        if mode != self._last_mode:
            self.get_logger().info(f'Mode → {mode.name}')
            self._last_mode = mode

        self._pub.publish(out)


def main(args=None):
    rclpy.init(args=args)
    node = MuxNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
