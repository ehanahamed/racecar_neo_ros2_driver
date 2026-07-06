"""
Dot-matrix rasterizer that composites text/pixels into a Teensy frame.

Reads /dotmatrix/text and /dotmatrix/pixels and publishes an 8x24 frame on
/dotmatrix/frame for pit_node to forward to the Teensy.
The physical MAX7219 moved onto the NEO-PIT board (v0.3.0), so this node no
longer drives SPI. The Teensy renders the frame, and owns the idle splash +
drive-mode glyph; this node only rasterizes student content (text -> pixels) and
passes per-pixel bitmaps through. It publishes only while there IS content, so
that when the student sends nothing the frame goes stale and the Teensy falls
back to its idle display.
"""

import time

from luma.core.legacy import text
from luma.core.legacy.font import proportional, TINY_FONT as _LUMA_TINY_FONT
from PIL import Image, ImageDraw
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import String, UInt8MultiArray


# Local copy of TINY_FONT with a more legible diagonal-stroke 'N' (luma's stock
# glyph reads as a notched pillar on an 8-px-tall matrix).
TINY_FONT = list(_LUMA_TINY_FONT)
TINY_FONT[ord('N')] = [62, 4, 8, 62]


_RENDERED_WIDTH_CACHE: dict = {}


def rendered_text_width(message: str, font, height: int = 8) -> int:
    """Pixel width of `message` as luma's `text()` actually paints it (memoized)."""
    key = (message, id(font), height)
    cached = _RENDERED_WIDTH_CACHE.get(key)
    if cached is not None:
        return cached
    scratch = Image.new('1', (max(1, len(message)) * 16, height))
    text(ImageDraw.Draw(scratch), (0, 0), message, fill='white', font=font)
    bbox = scratch.getbbox()
    width = 0 if bbox is None else bbox[2]
    _RENDERED_WIDTH_CACHE[key] = width
    return width


def decode_pixel_array(data, expected_height: int, expected_width: int):
    """
    Decode a flat 0/1 (or 0/255) byte sequence into row strings of '.'/'X'.

    Produces `expected_height` rows. Pads short rows/frames with off, truncates
    long ones. Raises ValueError if there is not even one full row.
    """
    values = list(data)
    if len(values) < expected_width:
        raise ValueError(
            f'pixel array has {len(values)} entries; need at least '
            f'{expected_width} (one full row of {expected_width} pixels)'
        )
    rows = []
    for r in range(expected_height):
        start = r * expected_width
        chunk = values[start:start + expected_width] if start < len(values) else []
        if len(chunk) < expected_width:
            chunk = chunk + [0] * (expected_width - len(chunk))
        rows.append(''.join('X' if v else '.' for v in chunk))
    return rows


def scroll_offset(elapsed: float, total_width: int, viewport_width: int,
                  scroll_period_s: float) -> int:
    """Pixel offset for a left-scrolling message; 0 when it fits the viewport."""
    if total_width <= viewport_width or scroll_period_s <= 0:
        return 0
    travel = total_width + viewport_width
    phase = (elapsed % scroll_period_s) / scroll_period_s
    return int(phase * travel) - viewport_width


class DotMatrixNode(Node):
    def __init__(self):
        super().__init__('dotmatrix_node')

        self.declare_parameter('cascaded', 3)          # 8-px modules -> width
        self.declare_parameter('refresh_rate_hz', 15.0)
        self.declare_parameter('scroll_period_sec', 4.0)
        self.declare_parameter('pixels_timeout_sec', 5.0)
        self.declare_parameter('frame_topic', '/dotmatrix/frame')

        self._height = 8
        self._width = int(self.get_parameter('cascaded').value) * 8
        refresh_rate = float(self.get_parameter('refresh_rate_hz').value)
        self._scroll_period = float(self.get_parameter('scroll_period_sec').value)
        self._pixels_timeout = float(self.get_parameter('pixels_timeout_sec').value)
        self._font = proportional(TINY_FONT)

        self._user_text = ''
        self._text_start = time.monotonic()
        self._pixels_rows: list = []
        self._pixels_stamp = 0.0

        qos = QoSProfile(
            depth=1,
            history=QoSHistoryPolicy.KEEP_LAST,
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            durability=QoSDurabilityPolicy.VOLATILE,
        )
        self._pub = self.create_publisher(
            UInt8MultiArray, self.get_parameter('frame_topic').value, qos
        )
        self.create_subscription(String, '/dotmatrix/text', self._text_cb, qos)
        self.create_subscription(
            UInt8MultiArray, '/dotmatrix/pixels', self._pixels_cb, qos
        )
        self.create_timer(1.0 / refresh_rate, self._render)

        self.get_logger().info(
            f'DotMatrix rasterizer ready: {self._width}x{self._height}, '
            f'refresh={refresh_rate}Hz -> {self.get_parameter("frame_topic").value}'
        )

    def _text_cb(self, msg: String):
        if msg.data != self._user_text:
            self._user_text = msg.data
            self._text_start = time.monotonic()

    def _pixels_cb(self, msg: UInt8MultiArray):
        try:
            self._pixels_rows = decode_pixel_array(
                msg.data, expected_height=self._height, expected_width=self._width
            )
            self._pixels_stamp = time.monotonic()
        except ValueError as e:
            self.get_logger().warn(f'Invalid /dotmatrix/pixels message: {e}')

    def _rows_to_frame(self, rows) -> list:
        """8 row strings of '.'/'X' -> flat row-major 0/1 list of width*height."""
        frame = []
        for r in range(self._height):
            row = rows[r] if r < len(rows) else ''
            for c in range(self._width):
                frame.append(1 if c < len(row) and row[c] == 'X' else 0)
        return frame

    def _text_frame(self, message: str) -> list:
        """Rasterize `message` (static or scrolling) into a frame."""
        width = rendered_text_width(message, self._font)
        offset = 0 if width <= self._width else scroll_offset(
            time.monotonic() - self._text_start, width, self._width, self._scroll_period
        )
        img = Image.new('1', (self._width, self._height))
        text(ImageDraw.Draw(img), (-offset, 1), message, fill='white', font=self._font)
        px = img.load()
        return [1 if px[c, r] else 0
                for r in range(self._height) for c in range(self._width)]

    def _render(self):
        # Publish only when there is student content. Pixels win over text;
        # when neither is active, publish nothing so the Teensy shows its idle
        # splash/glyph (pit_node lets the frame go stale).
        if self._pixels_rows and (
            time.monotonic() - self._pixels_stamp
        ) <= self._pixels_timeout:
            frame = self._rows_to_frame(self._pixels_rows)
        elif self._user_text:
            frame = self._text_frame(self._user_text)
        else:
            return
        self._pub.publish(UInt8MultiArray(data=frame))


def main(args=None):
    rclpy.init(args=args)
    node = DotMatrixNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
