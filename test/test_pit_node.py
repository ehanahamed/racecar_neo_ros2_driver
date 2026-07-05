"""Unit tests for pit_node's pure IMU-transform helpers."""

import numpy as np

from racecar_neo_ros2_driver.pit_node import (
    clamp,
    remap_axes,
    transform_accel,
    transform_gyro,
    transform_mag,
)

_IDENTITY = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


class TestClamp:
    def test_within(self):
        assert clamp(0.3) == 0.3

    def test_saturates(self):
        assert clamp(2.0) == 1.0
        assert clamp(-2.0) == -1.0


class TestRemap:
    def test_identity(self):
        out = remap_axes([1.0, 2.0, 3.0], [0, 1, 2], [1.0, 1.0, 1.0])
        assert np.allclose(out, [1.0, 2.0, 3.0])

    def test_reorder_and_flip(self):
        # Map raw (x=front, y=right, z=up) into a z=front frame with a sign flip.
        out = remap_axes([1.0, 2.0, 3.0], [2, 1, 0], [1.0, 1.0, -1.0])
        assert np.allclose(out, [3.0, 2.0, -1.0])


class TestTransformAccel:
    def test_bias_and_scale(self):
        out = transform_accel(
            [9.81, 0.0, 0.0], [0, 1, 2], [1.0, 1.0, 1.0], 1.0, [0.81, 0.0, 0.0]
        )
        assert np.allclose(out, [9.0, 0.0, 0.0])


class TestTransformGyro:
    def test_deg_to_rad_scale(self):
        # gyro_scale converts deg/s -> rad/s; 180 deg/s -> pi rad/s.
        out = transform_gyro(
            [180.0, 0.0, 0.0], [0, 1, 2], [1.0, 1.0, 1.0], np.pi / 180.0, [0.0, 0.0, 0.0]
        )
        assert np.allclose(out, [np.pi, 0.0, 0.0])


class TestTransformMag:
    def test_microtesla_to_tesla(self):
        out = transform_mag(
            [50.0, 0.0, 0.0], [0, 1, 2], [1.0, 1.0, 1.0], 1e-6,
            [0.0, 0.0, 0.0], _IDENTITY,
        )
        assert np.allclose(out, [50e-6, 0.0, 0.0])

    def test_hard_and_soft_iron(self):
        # Hard-iron offset removed, then a soft-iron scale on x.
        soft = [2.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        out = transform_mag(
            [3.0, 1.0, 0.0], [0, 1, 2], [1.0, 1.0, 1.0], 1.0,
            [1.0, 0.0, 0.0], soft,
        )
        # (3-1)*2 = 4 on x; (1-0)*1 = 1 on y.
        assert np.allclose(out, [4.0, 1.0, 0.0])
