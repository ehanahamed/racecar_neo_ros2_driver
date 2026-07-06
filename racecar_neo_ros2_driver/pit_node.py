"""
NEO-PIT board driver: the Pi's single owner of the Teensy UART link.

Replaces the Maestro actuator path (pwm_node + maestro.py) and the I2C IMU
reader (imu_node). One node owns the serial port because only one process can:

  - subscribes /motor (AckermannDriveStamped, normalized [-1, 1]) and streams
    command frames to the Teensy at command_rate_hz (normalized passthrough:
    the Teensy maps the values to servo/ESC PWM);
  - reads telemetry frames and republishes the LSM9DS1 as /imu/lsm9ds1 + /mag
    (imu_fusion_node blends /imu/lsm9ds1 with the RealSense /imu/realsense into
    /imu/fused, the single IMU the library reads); both topics are parameters.

Battery/current, encoder, and RC-channel fields are decoded but not published
yet (deferred; see config/pit.yaml). Command values are forwarded raw with a
per-axis sign so steering/throttle polarity can be corrected on hardware without
reflashing. On a stale or missing /motor command the node sends neutral, and it
tolerates a missing/again-missing serial device (retries, never crashes).

IMU axis order/sign and the gyro/mag unit scales default to identity/pass-through
and MUST be verified against the LSM9DS1 mounting on the PIT PCB and the units
the firmware's Adafruit driver emits; the physical-axis convention students rely
on is documented in racecar-neo-library physics.py.
"""

import threading
import time

from ackermann_msgs.msg import AckermannDriveStamped
import numpy as np
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSProfile,
    QoSReliabilityPolicy,
)
from sensor_msgs.msg import Imu, MagneticField
import serial

from . import pit_protocol as pit


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def remap_axes(vec, order, sign) -> np.ndarray:
    """Reorder and sign-flip a 3-vector: out[i] = sign[i] * vec[order[i]]."""
    v = np.asarray(vec, dtype=float)
    return np.array([sign[i] * v[order[i]] for i in range(3)])


def transform_accel(raw, order, sign, scale, bias) -> np.ndarray:
    """Raw accel (m/s^2 from the firmware) to the body frame, minus bias."""
    return remap_axes(raw, order, sign) * scale - np.asarray(bias, dtype=float)


def transform_gyro(raw, order, sign, scale, bias) -> np.ndarray:
    """Raw gyro to the body frame in rad/s (scale=deg->rad if needed), minus bias."""
    return remap_axes(raw, order, sign) * scale - np.asarray(bias, dtype=float)


def transform_mag(raw, order, sign, scale, hard_iron, soft_iron) -> np.ndarray:
    """Raw mag to Tesla in the body frame, hard/soft-iron corrected."""
    tesla = remap_axes(raw, order, sign) * scale
    return np.asarray(soft_iron, dtype=float).reshape(3, 3) @ (
        tesla - np.asarray(hard_iron, dtype=float)
    )


class PitNode(Node):
    def __init__(self):
        super().__init__('pit_node')

        self.declare_parameter('serial_port', '/dev/neo-pit-pcb')
        self.declare_parameter('baud', 921600)
        self.declare_parameter('command_rate_hz', 60.0)
        self.declare_parameter('command_timeout_sec', 0.5)
        self.declare_parameter('reconnect_period_sec', 2.0)
        self.declare_parameter('steering_sign', 1)
        self.declare_parameter('speed_sign', 1)
        self.declare_parameter('require_crc', False)

        self.declare_parameter('frame_id', 'imu_link')
        self.declare_parameter('publish_mag', True)
        # imu_fusion_node blends this with /imu/realsense into /imu/fused.
        self.declare_parameter('imu_topic', '/imu/lsm9ds1')
        self.declare_parameter('mag_topic', '/mag')
        # Axis remap + unit scales. Identity/pass-through until verified on the PCB.
        self.declare_parameter('imu.accel_gyro_axis_order', [0, 1, 2])
        self.declare_parameter('imu.accel_gyro_axis_sign', [1.0, 1.0, 1.0])
        self.declare_parameter('imu.mag_axis_order', [0, 1, 2])
        self.declare_parameter('imu.mag_axis_sign', [1.0, 1.0, 1.0])
        self.declare_parameter('imu.accel_scale', 1.0)
        self.declare_parameter('imu.gyro_scale', 1.0)      # deg/s -> rad/s = 0.01745329
        self.declare_parameter('imu.mag_scale', 1.0e-6)    # firmware uT -> Tesla
        # Reuse the lsm9ds1 calibration YAMLs (same keys as imu_node).
        self.declare_parameter('accelerometer.bias', [0.0, 0.0, 0.0])
        self.declare_parameter('gyroscope.bias', [0.0, 0.0, 0.0])
        self.declare_parameter('magnetometer.hard_iron_bias', [0.0, 0.0, 0.0])
        self.declare_parameter(
            'magnetometer.soft_iron_matrix.data', np.identity(3).flatten().tolist()
        )

        self._port = self.get_parameter('serial_port').value
        self._baud = int(self.get_parameter('baud').value)
        self._cmd_timeout = float(self.get_parameter('command_timeout_sec').value)
        self._reconnect_period = float(self.get_parameter('reconnect_period_sec').value)
        self._steer_sign = int(self.get_parameter('steering_sign').value)
        self._speed_sign = int(self.get_parameter('speed_sign').value)
        self._require_crc = bool(self.get_parameter('require_crc').value)
        self._frame = self.get_parameter('frame_id').value
        self._publish_mag = bool(self.get_parameter('publish_mag').value)
        self._imu_topic = self.get_parameter('imu_topic').value
        self._mag_topic = self.get_parameter('mag_topic').value

        self._ag_order = list(self.get_parameter('imu.accel_gyro_axis_order').value)
        self._ag_sign = list(self.get_parameter('imu.accel_gyro_axis_sign').value)
        self._mag_order = list(self.get_parameter('imu.mag_axis_order').value)
        self._mag_sign = list(self.get_parameter('imu.mag_axis_sign').value)
        self._accel_scale = float(self.get_parameter('imu.accel_scale').value)
        self._gyro_scale = float(self.get_parameter('imu.gyro_scale').value)
        self._mag_scale = float(self.get_parameter('imu.mag_scale').value)
        self._accel_bias = np.array(self.get_parameter('accelerometer.bias').value, float)
        self._gyro_bias = np.array(self.get_parameter('gyroscope.bias').value, float)
        self._mag_hard = np.array(
            self.get_parameter('magnetometer.hard_iron_bias').value, float
        )
        self._mag_soft = np.array(
            self.get_parameter('magnetometer.soft_iron_matrix.data').value, float
        ).reshape(3, 3)

        qos = QoSProfile(
            depth=1,
            history=QoSHistoryPolicy.KEEP_LAST,
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            durability=QoSDurabilityPolicy.VOLATILE,
        )
        self._pub_imu = self.create_publisher(Imu, self._imu_topic, qos)
        self._pub_mag = self.create_publisher(MagneticField, self._mag_topic, qos)
        self.create_subscription(AckermannDriveStamped, '/motor', self._motor_cb, qos)

        self._latest_speed = 0.0
        self._latest_steer = 0.0
        self._cmd_stamp = 0.0
        self._ser = None
        self._write_lock = threading.Lock()
        self._crc_fail_count = 0
        self._running = True

        self._open_serial()

        rate = float(self.get_parameter('command_rate_hz').value)
        self.create_timer(1.0 / rate, self._send_command)

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        self.get_logger().info(
            f'PIT ready: port={self._port}@{self._baud}, cmd_rate={rate}Hz, '
            f'require_crc={self._require_crc}'
        )
        self.get_logger().warn(
            'Verify IMU axis order/sign and gyro/mag scales against the PIT board '
            f'before trusting {self._imu_topic} and {self._mag_topic}.'
        )

    def _open_serial(self) -> bool:
        try:
            self._ser = serial.Serial(self._port, self._baud, timeout=0.05)
            self.get_logger().info(f'Opened {self._port}')
            return True
        except (serial.SerialException, OSError) as exc:
            self._ser = None
            self.get_logger().warn(f'Cannot open {self._port}: {exc}; retrying')
            return False

    def _motor_cb(self, msg: AckermannDriveStamped):
        self._latest_speed = clamp(msg.drive.speed)
        self._latest_steer = clamp(msg.drive.steering_angle)
        self._cmd_stamp = time.monotonic()

    def _send_command(self):
        if self._ser is None or not self._ser.is_open:
            return
        fresh = (time.monotonic() - self._cmd_stamp) <= self._cmd_timeout
        speed = self._speed_sign * self._latest_speed if fresh else 0.0
        steer = self._steer_sign * self._latest_steer if fresh else 0.0
        wire = pit.encode_command(clamp(steer), clamp(speed))
        try:
            with self._write_lock:
                self._ser.write(wire)
        except (serial.SerialException, OSError) as exc:
            self.get_logger().warn(f'Serial write failed: {exc}; reconnecting')
            self._close_serial()

    def _read_loop(self):
        buffer = bytearray()
        while self._running:
            if self._ser is None or not self._ser.is_open:
                time.sleep(self._reconnect_period)
                self._open_serial()
                continue
            try:
                chunk = self._ser.read(max(1, self._ser.in_waiting))
            except (serial.SerialException, OSError) as exc:
                self.get_logger().warn(f'Serial read failed: {exc}; reconnecting')
                self._close_serial()
                continue
            if not chunk:
                continue
            buffer.extend(chunk)
            while True:
                telem, consumed = pit.scan_for_packet(buffer)
                if telem is not None:
                    self._publish_telemetry(telem)
                elif consumed == 0:
                    break

    def _publish_telemetry(self, telem: pit.Telemetry):
        if self._require_crc and not telem.crc_ok:
            self._crc_fail_count += 1
            if self._crc_fail_count % 100 == 1:
                self.get_logger().warn(
                    f'Dropping telemetry on CRC mismatch (count={self._crc_fail_count})'
                )
            return

        stamp = self.get_clock().now().to_msg()
        accel = transform_accel(
            telem.accel, self._ag_order, self._ag_sign, self._accel_scale, self._accel_bias
        )
        gyro = transform_gyro(
            telem.gyro, self._ag_order, self._ag_sign, self._gyro_scale, self._gyro_bias
        )

        imu = Imu()
        imu.header.stamp = stamp
        imu.header.frame_id = self._frame
        imu.linear_acceleration.x = float(accel[0])
        imu.linear_acceleration.y = float(accel[1])
        imu.linear_acceleration.z = float(accel[2])
        imu.angular_velocity.x = float(gyro[0])
        imu.angular_velocity.y = float(gyro[1])
        imu.angular_velocity.z = float(gyro[2])
        self._pub_imu.publish(imu)

        if self._publish_mag:
            mag_vec = transform_mag(
                telem.mag, self._mag_order, self._mag_sign, self._mag_scale,
                self._mag_hard, self._mag_soft,
            )
            mag = MagneticField()
            mag.header.stamp = stamp
            mag.header.frame_id = self._frame
            mag.magnetic_field.x = float(mag_vec[0])
            mag.magnetic_field.y = float(mag_vec[1])
            mag.magnetic_field.z = float(mag_vec[2])
            self._pub_mag.publish(mag)

    def _close_serial(self):
        try:
            if self._ser is not None:
                self._ser.close()
        except (serial.SerialException, OSError):
            pass
        self._ser = None

    def shutdown(self):
        self._running = False
        if self._ser is not None and self._ser.is_open:
            try:
                with self._write_lock:
                    self._ser.write(pit.encode_command(0.0, 0.0))
            except (serial.SerialException, OSError):
                pass
        self._close_serial()


def main(args=None):
    rclpy.init(args=args)
    node = PitNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
