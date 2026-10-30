# -*- coding: utf-8 -*-
"""
屏幕区域二维码实时捕获助手（单文件版）
=========================================

用途
----
在 Windows 桌面上框选一小块区域，程序在后台定时截图并在本地识别二维码。
检测到二维码后，会把原始区域截图保存到 captures/，并把二维码裁剪、放大显示，
方便用手机扫描。程序不联网、不自动打开链接、不自动登录，也不上传任何内容。

运行
----
    python -m pip install -r requirements.txt
    python main.py

单文件版便于直接运行。模块化拆分建议见 README.md。

重要说明
--------
- 只在本地处理图像；源码中没有网络请求逻辑。
- 请仅用于核对自己本来就有权查看的二维码。不要用它进行未经授权的扫码、
  登录或规避平台规则。使用本软件造成的后果由使用者自行承担。
- 某些受 DRM/系统保护的播放器窗口可能无法被 BitBlt/mss 截取，这是系统限制。
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import threading
import time
import traceback
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 第三方依赖。若缺少依赖，Python 会在启动时直接提示 ModuleNotFoundError。
# requirements.txt 中给出了固定版本；Pillow 用来保存原始截图，OpenCV 用来识别。
# ---------------------------------------------------------------------------
import cv2
import mss
import numpy as np
from PIL import Image
from PySide6.QtCore import (
    QEvent,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
    QObject,
)
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QCursor,
    QDesktopServices,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QMoveEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
    QScreen,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


# ===========================================================================
# 常量与全局路径
# ===========================================================================

APP_NAME = "屏幕区域二维码实时捕获助手"
APP_VERSION = "1.0.0"

DEFAULT_INTERVAL_MS = 150          # 默认截图/检测间隔
HISTORY_LIMIT = 50                 # 历史记录上限
DEDUPE_SECONDS = 2.0               # 相同内容 2 秒内不重复新增记录
ABSENCE_RESET_SECONDS = 1.2        # 二维码消失多久后视为“离开画面”
REPEAT_UPDATE_SECONDS = 0.8        # 持续可见时，更新时间的最小间隔
PREVIEW_INTERVAL_SECONDS = 0.8     # 左侧预览刷新间隔（不影响后台识别频率）
METRICS_INTERVAL_SECONDS = 0.7     # FPS/耗时状态刷新间隔
MIN_REGION_DIP = 8                 # 框选时最小 DIP 尺寸
QR_DISPLAY_TARGET = 760            # 放大二维码图片的目标最长边（像素）

DISCLAIMER_SHORT = "仅限本地核对 · 不上传 · 不自动打开链接"
DISCLAIMER_TEXT = (
    "本软件只在本机完成“屏幕区域截图 + 二维码识别 + 放大显示”。\n\n"
    "请仅用于核对自己本来就有权查看的二维码，不要用于未经授权的账号扫码、"
    "登录或规避平台规则。软件不会自动打开二维码内容、不会自动登录，也不会"
    "上传截图或二维码内容。"
)


def get_app_directory() -> Path:
    """返回程序所在目录。打包后使用 exe 所在目录，而不是临时解包目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = get_app_directory()
CAPTURES_DIR = Path(
    os.environ.get("QR_ASSISTANT_CAPTURE_DIR", str(APP_DIR / "captures"))
).expanduser()


def _check_writable_directory(directory: Path) -> None:
    """通过实际写入探针文件确认目录可写。失败时抛出 OSError。"""
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / f".write_probe_{os.getpid()}_{time.time_ns()}"
    try:
        probe.write_bytes(b"qr-screen-assistant")
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass


def resolve_capture_dir() -> tuple[Path, Optional[str]]:
    """
    选择截图目录。优先使用程序目录下的 captures/，不可写时回退到用户目录。
    返回 (目录, 警告信息或 None)。
    """
    candidates: list[Path] = [CAPTURES_DIR]
    home_fallback = Path.home() / "QrScreenCaptureAssistant" / "captures"
    if home_fallback != CAPTURES_DIR:
        candidates.append(home_fallback)
    temp_fallback = Path(tempfile.gettempdir()) / "QrScreenCaptureAssistant" / "captures"
    if temp_fallback not in candidates:
        candidates.append(temp_fallback)

    first_error: Optional[str] = None
    for index, candidate in enumerate(candidates):
        try:
            _check_writable_directory(candidate)
        except OSError as exc:
            if first_error is None:
                first_error = str(exc)
            continue
        if index == 0:
            return candidate, None
        return (
            candidate,
            f"无法写入 {CAPTURES_DIR}，截图将保存到 {candidate}。原因：{first_error}",
        )

    # 理论上极少发生；仍返回一个路径，让截图失败以非致命错误的形式呈现。
    return CAPTURES_DIR, f"无法创建可写截图目录：{first_error or '未知错误'}"


def make_capture_path(directory: Path, when: Optional[datetime] = None) -> Path:
    """生成唯一的 PNG 文件名，格式包含毫秒时间戳。"""
    when = when or datetime.now()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = when.strftime("%Y%m%d_%H%M%S_%f")[:-3]
    candidate = directory / f"capture_{stamp}.png"
    if not candidate.exists():
        return candidate
    for index in range(2, 10000):
        candidate = directory / f"capture_{stamp}_{index}.png"
        if not candidate.exists():
            return candidate
    return directory / f"capture_{stamp}_{time.time_ns()}.png"


def format_timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


# ===========================================================================
# 数据结构
# ===========================================================================


@dataclass(frozen=True)
class NativeMonitor:
    """Windows 物理像素坐标中的显示器矩形。"""

    device: str
    left: int
    top: int
    width: int
    height: int
    handle: int = 0

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height


@dataclass(frozen=True)
class ScreenRegion:
    """一次监控区域，坐标始终使用物理屏幕像素，便于直接交给 mss。"""

    left: int
    top: int
    width: int
    height: int
    screen_name: str = ""
    device_name: str = ""
    dip_width: int = 0
    dip_height: int = 0

    def as_mss_region(self) -> dict[str, int]:
        return {
            "left": int(self.left),
            "top": int(self.top),
            "width": int(self.width),
            "height": int(self.height),
        }

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    def description(self) -> str:
        display = self.device_name or self.screen_name or "显示器"
        return (
            f"{display}  ·  {self.width} × {self.height} 像素  ·  "
            f"左上角 ({self.left}, {self.top})"
        )


@dataclass
class QrObservation:
    """一帧内的二维码检测结果。content 为空表示检测到二维码但解码失败。"""

    content: str
    points: Optional[np.ndarray]
    backend: str = "opencv"


@dataclass
class DetectionPayload:
    """后台线程发给主线程的检测结果。QImage 已复制，跨线程使用安全。"""

    content: str
    content_key: str
    qr_image: QImage
    screenshot_path: Optional[Path]
    detected_at: datetime
    region_width: int
    region_height: int
    screen_name: str
    save_error: str = ""


@dataclass
class HistoryRecord:
    content: str
    content_key: str
    qr_image: QImage
    screenshot_path: Optional[Path]
    first_seen: datetime
    last_seen: datetime
    region_width: int
    region_height: int
    screen_name: str
    save_error: str = ""

    def summary(self, limit: int = 76) -> str:
        if not self.content:
            return "（检测到二维码，但未能解码内容）"
        compact = " ".join(self.content.splitlines()).strip()
        compact = "".join(ch if ch >= " " else " " for ch in compact)
        if len(compact) > limit:
            return compact[: limit - 1] + "…"
        return compact or "（空内容）"


# ===========================================================================
# Windows 显示器物理坐标辅助
# ===========================================================================


def _normalise_monitor_name(value: str) -> str:
    """把 \\\\.\\DISPLAY1 之类名称归一化，方便匹配 Qt 与 Win32。"""
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def enumerate_windows_monitors() -> list[NativeMonitor]:
    """
    使用 Win32 EnumDisplayMonitors 获取显示器的物理像素矩形。

    Qt 6 在 Windows 上默认是 Per-Monitor DPI Aware；在这个模式下 Win32 返回
    的是物理像素，正好与 mss 使用的坐标一致。若不在 Windows 或调用失败则返回空。
    """
    if os.name != "nt":
        return []

    try:
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32),
            ]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        monitor_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(RECT),
            ctypes.c_void_p,
        )

        monitors: list[NativeMonitor] = []

        @monitor_proc
        def _callback(hmonitor, _hdc, _lprect, _lparam):  # type: ignore[no-untyped-def]
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                rect = info.rcMonitor
                width = int(rect.right - rect.left)
                height = int(rect.bottom - rect.top)
                if width > 0 and height > 0:
                    monitors.append(
                        NativeMonitor(
                            device=str(info.szDevice),
                            left=int(rect.left),
                            top=int(rect.top),
                            width=width,
                            height=height,
                            handle=int(hmonitor or 0),
                        )
                    )
            return True

        user32.EnumDisplayMonitors(None, None, _callback, 0)
        return monitors
    except Exception:
        # 找不到物理坐标时仍可使用 Qt 的 DPI 倍率作为后备，不应因此导致程序崩溃。
        return []


def find_native_monitor(
    screen: QScreen, native_monitors: list[NativeMonitor]
) -> Optional[NativeMonitor]:
    """把 Qt QScreen 匹配到 Win32 显示器。优先使用原生 HMONITOR，最可靠。"""
    if not native_monitors:
        return None

    # Qt 的 Windows 平台插件可通过 QScreen.nativeInterface().handle() 暴露
    # HMONITOR。用它可以精确对应显示器的物理矩形，且不受混合 DPI 坐标影响。
    try:
        native_interface = screen.nativeInterface()
        handle_getter = getattr(native_interface, "handle", None)
        if callable(handle_getter):
            native_handle = int(handle_getter())
            if native_handle:
                for monitor in native_monitors:
                    if monitor.handle and monitor.handle == native_handle:
                        return monitor
    except Exception:
        pass

    wanted = _normalise_monitor_name(screen.name())
    if wanted:
        for monitor in native_monitors:
            if _normalise_monitor_name(monitor.device) == wanted:
                return monitor

    screens = QGuiApplication.screens()
    if len(native_monitors) == 1 and len(screens) == 1:
        return native_monitors[0]

    # 名称不匹配时，用“Qt 逻辑尺寸 × DPR”与物理尺寸做唯一匹配。
    dpr = max(0.1, float(screen.devicePixelRatio()))
    expected_w = int(round(screen.geometry().width() * dpr))
    expected_h = int(round(screen.geometry().height() * dpr))
    size_candidates = [
        m
        for m in native_monitors
        if abs(m.width - expected_w) <= 4 and abs(m.height - expected_h) <= 4
    ]
    if len(size_candidates) == 1:
        return size_candidates[0]
    return None


# ===========================================================================
# 图像处理、二维码识别与保存
# ===========================================================================


def qimage_from_bgr(image: np.ndarray) -> QImage:
    """把 OpenCV BGR/BGRA 图像转为独立的 QImage，避免引用临时 NumPy 内存。"""
    if image is None or image.size == 0:
        return QImage()
    if image.dtype != np.uint8:
        image = np.clip(image, 0, 255).astype(np.uint8)
    image = np.ascontiguousarray(image)
    if image.ndim == 2:
        rgb = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
    elif image.ndim == 3 and image.shape[2] == 4:
        rgb = cv2.cvtColor(image, cv2.COLOR_BGRA2RGB)
    elif image.ndim == 3 and image.shape[2] == 3:
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    else:
        return QImage()
    height, width = rgb.shape[:2]
    qimage = QImage(
        rgb.data,
        int(width),
        int(height),
        int(rgb.strides[0]),
        QImage.Format.Format_RGB888,
    )
    return qimage.copy()


def make_preview_qimage(frame_bgr: np.ndarray) -> QImage:
    """生成左侧小预览，控制跨线程传输量。"""
    if frame_bgr is None or frame_bgr.size == 0:
        return QImage()
    height, width = frame_bgr.shape[:2]
    scale = min(1.0, 560.0 / max(1, width), 360.0 / max(1, height))
    preview = frame_bgr
    if scale < 1.0:
        preview = cv2.resize(
            frame_bgr,
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    return qimage_from_bgr(preview)


def _order_points(points: np.ndarray) -> Optional[np.ndarray]:
    """把四个二维码角点整理为左上、右上、右下、左下。"""
    try:
        pts = np.asarray(points, dtype=np.float32).reshape(-1, 2)
        pts = pts[np.isfinite(pts).all(axis=1)]
        if len(pts) != 4:
            return None
        ordered = np.zeros((4, 2), dtype=np.float32)
        sums = pts.sum(axis=1)
        diffs = np.diff(pts, axis=1).reshape(-1)
        ordered[0] = pts[np.argmin(sums)]
        ordered[2] = pts[np.argmax(sums)]
        ordered[1] = pts[np.argmin(diffs)]
        ordered[3] = pts[np.argmax(diffs)]

        # 防止检测器返回退化/重复角点。
        unique: list[np.ndarray] = []
        for point in ordered:
            if all(float(np.linalg.norm(point - old)) > 2.0 for old in unique):
                unique.append(point)
        if len(unique) != 4:
            return None
        return np.asarray(unique, dtype=np.float32)
    except Exception:
        return None


def _crop_axis_aligned(frame_bgr: np.ndarray, points: Optional[np.ndarray]) -> np.ndarray:
    """兜底裁剪：按角点外接矩形裁剪，并留出少量白边。"""
    height, width = frame_bgr.shape[:2]
    if points is None:
        return frame_bgr.copy()
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 2)
    pts = pts[np.isfinite(pts).all(axis=1)]
    if len(pts) < 2:
        return frame_bgr.copy()
    x, y, w, h = cv2.boundingRect(pts.astype(np.int32))
    pad = max(8, int(max(w, h) * 0.12))
    x1 = max(0, x - pad)
    y1 = max(0, y - pad)
    x2 = min(width, x + w + pad)
    y2 = min(height, y + h + pad)
    if x2 <= x1 or y2 <= y1:
        return frame_bgr.copy()
    return frame_bgr[y1:y2, x1:x2].copy()


def extract_qr_crop(frame_bgr: np.ndarray, points: Optional[np.ndarray]) -> np.ndarray:
    """
    按四个角点做透视校正，得到正放的二维码裁剪图。
    失败时退化为按外接矩形裁剪；仍失败则返回整个区域。
    """
    if frame_bgr is None or frame_bgr.size == 0:
        return frame_bgr

    height, width = frame_bgr.shape[:2]
    ordered = _order_points(points) if points is not None else None
    if ordered is None:
        return _crop_axis_aligned(frame_bgr, points)

    try:
        ordered[:, 0] = np.clip(ordered[:, 0], 0, max(0, width - 1))
        ordered[:, 1] = np.clip(ordered[:, 1], 0, max(0, height - 1))

        # 稍微向外扩一点，尽量保留二维码四周的静区。
        center = ordered.mean(axis=0)
        expanded = (ordered - center) * 1.10 + center
        expanded[:, 0] = np.clip(expanded[:, 0], 0, max(0, width - 1))
        expanded[:, 1] = np.clip(expanded[:, 1], 0, max(0, height - 1))

        side = int(
            max(
                np.linalg.norm(expanded[0] - expanded[1]),
                np.linalg.norm(expanded[1] - expanded[2]),
                np.linalg.norm(expanded[2] - expanded[3]),
                np.linalg.norm(expanded[3] - expanded[0]),
            )
        )
        side = max(24, min(side, 1600))
        dst = np.array(
            [[0, 0], [side - 1, 0], [side - 1, side - 1], [0, side - 1]],
            dtype=np.float32,
        )
        transform = cv2.getPerspectiveTransform(expanded, dst)
        crop = cv2.warpPerspective(
            frame_bgr,
            transform,
            (side, side),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
        if crop is None or crop.size == 0:
            return _crop_axis_aligned(frame_bgr, points)
        return crop
    except Exception:
        return _crop_axis_aligned(frame_bgr, points)


def build_enlarged_qr(frame_bgr: np.ndarray, points: Optional[np.ndarray]) -> QImage:
    """裁剪二维码、加白色静区并放大；输出图片只用于显示/手动保存。"""
    try:
        crop = extract_qr_crop(frame_bgr, points)
        if crop is None or crop.size == 0:
            return QImage()

        side = max(crop.shape[:2])
        pad = max(14, int(side * 0.09))
        padded = cv2.copyMakeBorder(
            crop,
            pad,
            pad,
            pad,
            pad,
            borderType=cv2.BORDER_CONSTANT,
            value=(255, 255, 255),
        )
        height, width = padded.shape[:2]
        longest = max(height, width)
        if longest <= 0:
            return QImage()

        target_scale = QR_DISPLAY_TARGET / float(longest)
        if target_scale > 1.0:
            interpolation = cv2.INTER_NEAREST  # 小图放大时尽量保持模块边缘清晰
        else:
            interpolation = cv2.INTER_AREA
        resized = cv2.resize(
            padded,
            (
                max(1, int(round(width * target_scale))),
                max(1, int(round(height * target_scale))),
            ),
            interpolation=interpolation,
        )
        return qimage_from_bgr(resized)
    except Exception:
        try:
            return qimage_from_bgr(frame_bgr)
        except Exception:
            return QImage()


def save_region_screenshot(
    frame_bgr: np.ndarray, directory: Path, when: datetime
) -> Path:
    """把完整监控区域保存为 PNG。Pillow 保存，低压缩等级以缩短响应时间。"""
    if frame_bgr is None or frame_bgr.size == 0:
        raise ValueError("截图内容为空")
    path = make_capture_path(directory, when)
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb)
    image.save(path, format="PNG", compress_level=1)
    if not path.exists() or path.stat().st_size == 0:
        raise OSError("截图文件未成功写入")
    return path


def _decode_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return ""
    return str(value)


class QrCodeEngine:
    """
    OpenCV 二维码识别器。

    主路径始终是 cv2.QRCodeDetector；若有可选的 WeChatQRCode 模型，
    则在标准检测器没有解出内容时再做一次备用解码。没有 contrib 包或没有模型
    文件时完全不影响主功能。
    """

    WECHAT_MODEL_FILES = (
        "detect.prototxt",
        "detect.caffemodel",
        "sr.prototxt",
        "sr.caffemodel",
    )

    def __init__(self) -> None:
        self._detector = cv2.QRCodeDetector()
        self._wechat = None
        self.wechat_unavailable_reason = ""
        self._init_optional_wechat()

    def _wechat_model_dir(self) -> Optional[Path]:
        env_dir = os.environ.get("QR_ASSISTANT_WECHAT_MODEL_DIR", "").strip()
        candidates = []
        if env_dir:
            candidates.append(Path(env_dir).expanduser())
        candidates.extend(
            [
                APP_DIR / "models" / "wechat_qrcode",
                Path.cwd() / "models" / "wechat_qrcode",
            ]
        )
        for candidate in candidates:
            if all((candidate / filename).is_file() for filename in self.WECHAT_MODEL_FILES):
                return candidate
        return None

    def _init_optional_wechat(self) -> None:
        """尝试启用 cv2.wechat_qrcode；任何缺失都静默降级。"""
        try:
            if not hasattr(cv2, "wechat_qrcode"):
                self.wechat_unavailable_reason = (
                    "当前 OpenCV 不含 wechat_qrcode（如需备用解码，请按 README 安装 "
                    "opencv-contrib-python 并提供模型文件）"
                )
                return
            model_dir = self._wechat_model_dir()
            if model_dir is None:
                self.wechat_unavailable_reason = "未找到 wechat_qrcode 模型文件"
                return
            module = getattr(cv2, "wechat_qrcode")
            decoder_class = getattr(module, "WeChatQRCode", None) or getattr(
                cv2, "wechat_qrcode_WeChatQRCode", None
            )
            if decoder_class is None:
                self.wechat_unavailable_reason = "当前 OpenCV 无法创建 WeChatQRCode"
                return
            self._wechat = decoder_class(
                str(model_dir / "detect.prototxt"),
                str(model_dir / "detect.caffemodel"),
                str(model_dir / "sr.prototxt"),
                str(model_dir / "sr.caffemodel"),
            )
        except Exception as exc:
            self._wechat = None
            self.wechat_unavailable_reason = f"WeChatQRCode 初始化失败：{exc}"

    def _safe_standard_detect(
        self, gray: np.ndarray
    ) -> tuple[str, Optional[np.ndarray]]:
        content = ""
        points: Optional[np.ndarray] = None
        try:
            result = self._detector.detectAndDecode(gray)
            if isinstance(result, tuple) and len(result) >= 2:
                content = _decode_text(result[0])
                raw_points = result[1]
                if raw_points is not None:
                    points = np.asarray(raw_points, dtype=np.float32).reshape(-1, 2)
            else:
                content = _decode_text(result)
        except Exception:
            content = ""
            points = None

        # detectAndDecode 偶尔在解码失败时不返回角点；单独 detect 一次拿角点。
        if points is None:
            try:
                ok, raw_points = self._detector.detect(gray)
                if ok and raw_points is not None:
                    points = np.asarray(raw_points, dtype=np.float32).reshape(-1, 2)
            except Exception:
                points = None
        if len(content) == 0:
            content = ""
        return content, points

    def _safe_wechat_detect(
        self, frame_bgr: np.ndarray
    ) -> tuple[str, Optional[np.ndarray]]:
        if self._wechat is None:
            return "", None
        try:
            result = self._wechat.detectAndDecode(frame_bgr)
            if not isinstance(result, tuple) or len(result) < 2:
                return "", None
            texts, points_seq = result
            text_list = list(texts) if texts is not None else []
            point_list = list(points_seq) if points_seq is not None else []
            for index, text_value in enumerate(text_list):
                text = _decode_text(text_value)
                if not text:
                    continue
                points = None
                if index < len(point_list) and point_list[index] is not None:
                    points = np.asarray(
                        point_list[index], dtype=np.float32
                    ).reshape(-1, 2)
                return text, points
            return "", None
        except Exception:
            # 备用解码器出错不应拖垮主流程；下一帧仍然尝试。
            return "", None

    def detect(self, frame_bgr: np.ndarray) -> Optional[QrObservation]:
        if frame_bgr is None or frame_bgr.size == 0:
            return None
        try:
            gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        except Exception:
            return None

        content, points = self._safe_standard_detect(gray)
        if not content and self._wechat is not None:
            fallback_content, fallback_points = self._safe_wechat_detect(frame_bgr)
            if fallback_content:
                return QrObservation(
                    fallback_content,
                    fallback_points if fallback_points is not None else points,
                    backend="opencv+wechat",
                )
            if points is None and fallback_points is not None:
                points = fallback_points

        if content or points is not None:
            return QrObservation(content, points, backend="opencv")
        return None


# ===========================================================================
# 后台监控线程
# ===========================================================================


class MonitorWorker(QThread):
    """
    在独立线程中执行 mss 截图和 OpenCV 检测。

    所有 Qt 控件仍由主线程创建和更新；本类只通过信号发送数据。
    QImage 本身可在非 GUI 线程构造，发送前已复制，不依赖临时 NumPy 缓冲区。
    """

    qr_detected = Signal(object)              # DetectionPayload
    repeated_seen = Signal(str, object)       # content_key, datetime
    preview_ready = Signal(object)            # QImage
    metrics = Signal(float, float, bool)      # 采集 FPS, 平均检测耗时 ms, 当前是否检测到
    warning = Signal(str)                     # 非致命异常/保存失败
    fatal_error = Signal(str)                 # 线程无法继续的错误

    def __init__(
        self,
        region: ScreenRegion,
        capture_dir: Path,
        interval_ms: int = DEFAULT_INTERVAL_MS,
    ) -> None:
        super().__init__()
        self._region = region
        self._capture_dir = capture_dir
        self._interval = max(50, int(interval_ms)) / 1000.0
        self._stop_event = threading.Event()
        self._last_warning_at = 0.0

    def request_stop(self) -> None:
        self._stop_event.set()

    def _warn(self, message: str) -> None:
        now = time.monotonic()
        if now - self._last_warning_at >= 3.0:
            self._last_warning_at = now
            self.warning.emit(message)

    def _is_region_in_virtual_screen(self, virtual: dict, region: ScreenRegion) -> bool:
        if region.width <= 0 or region.height <= 0:
            return False
        return (
            region.left >= int(virtual["left"])
            and region.top >= int(virtual["top"])
            and region.left + region.width
            <= int(virtual["left"]) + int(virtual["width"])
            and region.top + region.height
            <= int(virtual["top"]) + int(virtual["height"])
        )

    def run(self) -> None:  # noqa: C901 - 主循环逻辑集中在此，便于随线程退出
        engine: Optional[QrCodeEngine] = None
        consecutive_capture_errors = 0
        active_key: Optional[str] = None
        last_observation_at = 0.0
        last_repeat_emit = 0.0
        last_preview_at = 0.0
        recent: "OrderedDict[str, float]" = OrderedDict()
        detect_samples: deque[float] = deque(maxlen=60)
        frames_for_metrics = 0
        metrics_started_at = time.monotonic()
        last_metrics_at = metrics_started_at

        try:
            engine = QrCodeEngine()
            with mss.mss() as sct:
                virtual = sct.monitors[0]
                if not self._is_region_in_virtual_screen(virtual, self._region):
                    self.fatal_error.emit(
                        "选定区域已不在当前桌面范围内。可能是显示器断开、分辨率改变或"
                        "缩放变化，请重新点击“选择监控区域”。"
                    )
                    return

                region_dict = self._region.as_mss_region()

                while not self._stop_event.is_set():
                    loop_started = time.monotonic()
                    try:
                        shot = sct.grab(region_dict)
                        frame = np.asarray(shot, dtype=np.uint8)
                        if (
                            frame.ndim != 3
                            or frame.shape[2] < 3
                            or frame.shape[0] == 0
                            or frame.shape[1] == 0
                        ):
                            raise RuntimeError("截图返回了空图像")
                        # mss 通常返回 BGRA；转成紧凑 BGR 供 OpenCV 使用。
                        if frame.shape[2] == 4:
                            frame_bgr = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                        else:
                            frame_bgr = np.ascontiguousarray(frame[:, :, :3])
                        consecutive_capture_errors = 0
                    except Exception as exc:
                        consecutive_capture_errors += 1
                        self._warn(f"区域截图失败（第 {consecutive_capture_errors} 次）：{exc}")
                        if consecutive_capture_errors >= 5:
                            self.fatal_error.emit(
                                "连续多次截图失败，监控已停止。请确认区域仍在屏幕内，"
                                "并重新选择监控区域。"
                            )
                            return
                        self._stop_event.wait(0.35)
                        continue

                    now = time.monotonic()
                    observation: Optional[QrObservation] = None
                    detect_start = time.perf_counter()
                    try:
                        observation = engine.detect(frame_bgr)
                    except Exception as exc:
                        self._warn(f"二维码检测异常：{exc}")
                    detect_ms = (time.perf_counter() - detect_start) * 1000.0
                    detect_samples.append(detect_ms)

                    # 左侧实时预览，约 1.25 帧/秒，不占网络、不上传。
                    if now - last_preview_at >= PREVIEW_INTERVAL_SECONDS:
                        last_preview_at = now
                        try:
                            self.preview_ready.emit(make_preview_qimage(frame_bgr))
                        except Exception:
                            pass

                    if observation is not None:
                        last_observation_at = now
                        if observation.content:
                            content = observation.content
                            content_key = "decoded:" + content
                        else:
                            content = ""
                            # 未解码二维码使用固定键，避免角点抖动导致不断新增记录。
                            content_key = "undecoded"

                        if active_key == content_key:
                            # 同一二维码持续存在于画面中：只更新时间，不重复截图/入历史。
                            if now - last_repeat_emit >= REPEAT_UPDATE_SECONDS:
                                self.repeated_seen.emit(content_key, datetime.now())
                                last_repeat_emit = now
                        else:
                            previous_saved = recent.get(content_key)
                            if (
                                previous_saved is not None
                                and now - previous_saved < DEDUPE_SECONDS
                            ):
                                # 2 秒去重窗口内相同内容再次出现：更新时间但不新增记录。
                                if now - last_repeat_emit >= 0.2:
                                    self.repeated_seen.emit(content_key, datetime.now())
                                    last_repeat_emit = now
                                active_key = content_key
                            else:
                                detected_at = datetime.now()
                                qr_image = build_enlarged_qr(
                                    frame_bgr, observation.points
                                )
                                screenshot_path: Optional[Path] = None
                                save_error = ""
                                try:
                                    screenshot_path = save_region_screenshot(
                                        frame_bgr, self._capture_dir, detected_at
                                    )
                                except Exception as exc:
                                    save_error = str(exc)
                                    self._warn(
                                        f"原始截图保存失败（识别结果仍会显示）：{exc}"
                                    )

                                payload = DetectionPayload(
                                    content=content,
                                    content_key=content_key,
                                    qr_image=qr_image,
                                    screenshot_path=screenshot_path,
                                    detected_at=detected_at,
                                    region_width=self._region.width,
                                    region_height=self._region.height,
                                    screen_name=self._region.device_name
                                    or self._region.screen_name,
                                    save_error=save_error,
                                )
                                self.qr_detected.emit(payload)
                                active_key = content_key
                                last_repeat_emit = now
                                recent[content_key] = now
                                recent.move_to_end(content_key)

                            # 清理过期的去重缓存，避免长期运行时无限增长。
                            cutoff = now - 90.0
                            for old_key, old_time in list(recent.items()):
                                if old_time < cutoff:
                                    recent.pop(old_key, None)
                            while len(recent) > 100:
                                recent.popitem(last=False)
                    else:
                        if (
                            active_key is not None
                            and now - last_observation_at > ABSENCE_RESET_SECONDS
                        ):
                            active_key = None

                    frames_for_metrics += 1
                    if now - last_metrics_at >= METRICS_INTERVAL_SECONDS:
                        elapsed = max(1e-6, now - metrics_started_at)
                        fps = frames_for_metrics / elapsed
                        average_detect = (
                            sum(detect_samples) / len(detect_samples)
                            if detect_samples
                            else 0.0
                        )
                        self.metrics.emit(
                            float(fps),
                            float(average_detect),
                            observation is not None or active_key is not None,
                        )
                        frames_for_metrics = 0
                        metrics_started_at = now
                        last_metrics_at = now

                    # 控制频率；使用 Event.wait 以便退出/暂停能快速生效。
                    elapsed_loop = time.monotonic() - loop_started
                    delay = self._interval - elapsed_loop
                    if delay > 0:
                        self._stop_event.wait(delay)

        except Exception as exc:
            self.fatal_error.emit(f"监控线程异常：{exc}")
            try:
                print(
                    "MonitorWorker fatal error:\n" + traceback.format_exc(),
                    file=sys.stderr,
                )
            except Exception:
                pass
        finally:
            if not self._stop_event.is_set():
                self._stop_event.set()


# ===========================================================================
# 屏幕区域框选
# ===========================================================================


def _map_selection_to_physical(
    overlay_size: QSize,
    selection: QRect,
    screen: QScreen,
    native: Optional[NativeMonitor],
) -> Optional[ScreenRegion]:
    """
    将 Qt 逻辑坐标(DIP)中的框选矩形映射到物理屏幕像素。

    有 Win32 物理显示器矩形时，用 overlay 尺寸与物理尺寸的比例精确换算。
    这能正确处理 Windows 高 DPI 缩放和多显示器；匹配不到时使用 QScreen 的
    DPR 作为后备。
    """
    if selection.width() <= 0 or selection.height() <= 0:
        return None
    rect = selection.intersected(QRect(0, 0, overlay_size.width(), overlay_size.height()))
    if rect.width() <= 0 or rect.height() <= 0:
        return None

    if native is not None:
        sx = native.width / float(max(1, overlay_size.width()))
        sy = native.height / float(max(1, overlay_size.height()))
        x1 = native.left + int(round(rect.left() * sx))
        y1 = native.top + int(round(rect.top() * sy))
        x2 = native.left + int(round((rect.left() + rect.width()) * sx))
        y2 = native.top + int(round((rect.top() + rect.height()) * sy))
        left_limit = native.left
        top_limit = native.top
        right_limit = native.right
        bottom_limit = native.bottom
        device_name = native.device
    else:
        # 后备路径：以 Qt 的 DPR 和屏幕逻辑原点推算物理坐标。
        dpr = max(0.1, float(screen.devicePixelRatio()))
        origin_x = int(round(screen.geometry().left() * dpr))
        origin_y = int(round(screen.geometry().top() * dpr))
        sx = dpr
        sy = dpr
        x1 = origin_x + int(round(rect.left() * sx))
        y1 = origin_y + int(round(rect.top() * sy))
        x2 = origin_x + int(round((rect.left() + rect.width()) * sx))
        y2 = origin_y + int(round((rect.top() + rect.height()) * sy))
        left_limit = origin_x
        top_limit = origin_y
        right_limit = origin_x + int(round(overlay_size.width() * sx))
        bottom_limit = origin_y + int(round(overlay_size.height() * sy))
        device_name = ""

    x1 = max(left_limit, min(x1, right_limit))
    y1 = max(top_limit, min(y1, bottom_limit))
    x2 = max(left_limit, min(x2, right_limit))
    y2 = max(top_limit, min(y2, bottom_limit))
    width = x2 - x1
    height = y2 - y1
    if width < 4 or height < 4:
        return None

    return ScreenRegion(
        left=int(x1),
        top=int(y1),
        width=int(width),
        height=int(height),
        screen_name=screen.name(),
        device_name=device_name,
        dip_width=int(rect.width()),
        dip_height=int(rect.height()),
    )


class SelectionOverlay(QWidget):
    """覆盖单个显示器的透明框选窗口。每个显示器一个，互不干扰。"""

    def __init__(
        self,
        controller: "RegionSelector",
        screen: QScreen,
        native: Optional[NativeMonitor],
    ) -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.NoDropShadowWindowHint
        )
        super().__init__(None, flags)
        self.controller = controller
        self.screen = screen
        self.native = native
        self._start_point: Optional[QPoint] = None
        self._current_point: Optional[QPoint] = None
        self._selection = QRect()
        self._hint_text = ""
        self._hint_until = 0.0
        self._closing_for_controller = False

        self.setObjectName("selectionOverlay")
        self.setWindowTitle("选择监控区域")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        try:
            self.setScreen(screen)
        except Exception:
            pass
        self.setGeometry(screen.geometry())

    # ------------------------------------------------------------------
    # 鼠标与键盘
    # ------------------------------------------------------------------
    def _clamp_point(self, point: QPoint) -> QPoint:
        return QPoint(
            min(max(0, point.x()), max(0, self.width() - 1)),
            min(max(0, point.y()), max(0, self.height() - 1)),
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self.controller.cancel()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._start_point = self._clamp_point(event.position().toPoint())
            self._current_point = self._start_point
            self._selection = QRect(self._start_point, self._start_point).normalized()
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._start_point is not None:
            self._current_point = self._clamp_point(event.position().toPoint())
            self._selection = QRect(
                self._start_point, self._current_point
            ).normalized()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._start_point is None:
            super().mouseReleaseEvent(event)
            return

        self._current_point = self._clamp_point(event.position().toPoint())
        self._selection = QRect(self._start_point, self._current_point).normalized()
        self._start_point = None
        self._current_point = None

        selection = self._selection
        if (
            selection.width() < MIN_REGION_DIP
            or selection.height() < MIN_REGION_DIP
        ):
            self._selection = QRect()
            self._hint_text = "区域太小，请拖拽一个更大的矩形，或按 Esc 取消"
            self._hint_until = time.monotonic() + 2.0
            self.update()
            QTimer.singleShot(2100, self.update)
            return

        region = _map_selection_to_physical(
            self.size(), selection, self.screen, self.native
        )
        if region is None:
            self._selection = QRect()
            self._hint_text = "无法换算该区域，请重新框选"
            self._hint_until = time.monotonic() + 2.0
            self.update()
            QTimer.singleShot(2100, self.update)
            return
        self.controller.complete(region, self)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.controller.cancel()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._closing_for_controller:
            QTimer.singleShot(0, self.controller.cancel)
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def _physical_size_for_label(self, selection: QRect) -> tuple[int, int]:
        region = _map_selection_to_physical(
            self.size(), selection, self.screen, self.native
        )
        if region is None:
            return selection.width(), selection.height()
        return region.width, region.height

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        dim_color = QColor(8, 15, 30, 125)
        selection = self._selection if self._selection.isValid() else QRect()
        if selection.width() > 0 and selection.height() > 0:
            # 只给选区之外加暗色，选区内部保持完全透明，可看到实时桌面。
            left = selection.left()
            top = selection.top()
            right = selection.left() + selection.width()
            bottom = selection.top() + selection.height()
            painter.fillRect(QRect(0, 0, self.width(), max(0, top)), dim_color)
            painter.fillRect(
                QRect(0, bottom, self.width(), max(0, self.height() - bottom)),
                dim_color,
            )
            painter.fillRect(QRect(0, top, max(0, left), selection.height()), dim_color)
            painter.fillRect(
                QRect(right, top, max(0, self.width() - right), selection.height()),
                dim_color,
            )

            painter.setPen(QPen(QColor("#38BDF8"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(selection.adjusted(0, 0, -1, -1))

            # 四角强调线
            painter.setPen(QPen(QColor("#E0F2FE"), 3))
            handle_len = min(22, max(10, selection.width() // 12))
            corners = [
                selection.topLeft(),
                selection.topRight(),
                selection.bottomLeft(),
                selection.bottomRight(),
            ]
            for corner in corners:
                x = corner.x()
                y = corner.y()
                dx = 1 if x > selection.center().x() else -1
                dy = 1 if y > selection.center().y() else -1
                painter.drawLine(x, y, x + dx * handle_len, y)
                painter.drawLine(x, y, x, y + dy * handle_len)

            physical_w, physical_h = self._physical_size_for_label(selection)
            label = f"{physical_w} × {physical_h} 像素"
            font = QFont(self.font())
            font.setPointSize(10)
            font.setBold(True)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            text_width = metrics.horizontalAdvance(label)
            text_height = metrics.height()
            # 优先放在选区右上方外侧，避免遮住左上角拖动点。
            label_x = selection.left() + selection.width() + 10
            label_y = selection.top()
            if label_x + text_width + 20 > self.width() - 8:
                label_x = selection.left()
                label_y = selection.top() - text_height - 18
                if label_y < 8:
                    label_y = selection.top() + 10
            label_x = min(max(8, label_x), max(8, self.width() - text_width - 28))
            label_rect = QRectF(
                float(label_x),
                float(label_y),
                float(text_width + 20),
                float(text_height + 12),
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(2, 6, 23, 210))
            painter.drawRoundedRect(label_rect, 7, 7)
            painter.setPen(QColor("#E0F2FE"))
            painter.drawText(
                label_rect,
                Qt.AlignmentFlag.AlignCenter,
                label,
            )

        # 顶部操作提示
        title = "拖拽鼠标，框选二维码可能出现的区域"
        subtitle = "支持多显示器 · 每个显示器可单独框选 · Esc / 右键取消"
        title_font = QFont(self.font())
        title_font.setPointSize(12)
        title_font.setBold(True)
        sub_font = QFont(self.font())
        sub_font.setPointSize(9)

        painter.setFont(title_font)
        title_width = painter.fontMetrics().horizontalAdvance(title)
        painter.setFont(sub_font)
        sub_width = painter.fontMetrics().horizontalAdvance(subtitle)
        box_width = min(self.width() - 24, max(title_width, sub_width) + 44)
        box_height = 68
        box_rect = QRectF(
            (self.width() - box_width) / 2.0,
            24.0,
            float(box_width),
            float(box_height),
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(2, 6, 23, 220))
        painter.drawRoundedRect(box_rect, 12, 12)
        painter.setFont(title_font)
        painter.setPen(QColor("#F8FAFC"))
        painter.drawText(
            box_rect.adjusted(0, 10, 0, -30),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            title,
        )
        painter.setFont(sub_font)
        painter.setPen(QColor("#94A3B8"))
        painter.drawText(
            box_rect.adjusted(0, 40, 0, -8),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            subtitle,
        )

        if self._hint_text and time.monotonic() < self._hint_until:
            hint_font = QFont(self.font())
            hint_font.setPointSize(11)
            hint_font.setBold(True)
            painter.setFont(hint_font)
            hint_width = painter.fontMetrics().horizontalAdvance(self._hint_text)
            hint_rect = QRectF(
                (self.width() - min(self.width() - 24, hint_width + 48)) / 2.0,
                self.height() / 2.0 - 26,
                min(self.width() - 24, hint_width + 48),
                52,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(127, 29, 29, 230))
            painter.drawRoundedRect(hint_rect, 10, 10)
            painter.setPen(QColor("#FEE2E2"))
            painter.drawText(hint_rect, Qt.AlignmentFlag.AlignCenter, self._hint_text)

        painter.end()


class RegionSelector(QObject):
    """管理所有显示器的框选窗口，并统一发出完成/取消信号。"""

    region_selected = Signal(object)   # ScreenRegion
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._overlays: list[SelectionOverlay] = []
        self._active = False
        self._tearing_down = False
        self._app = QApplication.instance()
        self._cursor_screen_geometry = None

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> None:
        if self._active:
            return
        screens = QGuiApplication.screens()
        if not screens:
            self.failed.emit("未检测到可用显示器，无法选择监控区域。")
            return
        self._active = True
        try:
            if self._app is not None:
                self._app.installEventFilter(self)
            native_monitors = enumerate_windows_monitors()
            for screen in screens:
                native = find_native_monitor(screen, native_monitors)
                overlay = SelectionOverlay(self, screen, native)
                self._overlays.append(overlay)
                overlay.show()
                overlay.raise_()
            QTimer.singleShot(80, self._activate_overlay_under_cursor)
        except Exception as exc:
            self._cleanup()
            self.failed.emit(f"无法显示区域选择层：{exc}")

    def _activate_overlay_under_cursor(self) -> None:
        if not self._active or not self._overlays:
            return
        screen = QGuiApplication.screenAt(QCursor.pos())
        chosen = None
        if screen is not None:
            for overlay in self._overlays:
                if overlay.screen is screen or overlay.screen.name() == screen.name():
                    chosen = overlay
                    break
        chosen = chosen or self._overlays[0]
        try:
            chosen.raise_()
            chosen.activateWindow()
            chosen.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        except Exception:
            pass

    def complete(self, region: ScreenRegion, overlay: SelectionOverlay) -> None:
        if not self._active:
            return
        self._active = False
        self._teardown_overlays()
        self.region_selected.emit(region)

    def cancel(self) -> None:
        if not self._active:
            return
        self._active = False
        self._teardown_overlays()
        self.cancelled.emit()

    def _teardown_overlays(self) -> None:
        self._tearing_down = True
        try:
            if self._app is not None:
                self._app.removeEventFilter(self)
        except Exception:
            pass
        overlays = self._overlays
        self._overlays = []
        for overlay in overlays:
            try:
                overlay._closing_for_controller = True
                overlay.hide()
                overlay.close()
                overlay.deleteLater()
            except Exception:
                pass
        self._tearing_down = False

    def _cleanup(self) -> None:
        self._active = False
        self._teardown_overlays()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        """在没有窗口焦点的极端情况下，也尽量能响应 Esc 取消。"""
        if (
            self._active
            and event.type() == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
            and event.key() == Qt.Key.Key_Escape
        ):
            self.cancel()
            return True
        return super().eventFilter(watched, event)

    def shutdown(self) -> None:
        """应用退出时清理，不发出 cancel 信号，避免恢复已关闭的主窗口。"""
        self._active = False
        self._teardown_overlays()


# ===========================================================================
# 可缩放的图片显示控件
# ===========================================================================


class ImageView(QLabel):
    """保持宽高比的自适应图片区域，用占位文本提示当前状态。"""

    def __init__(
        self,
        placeholder: str,
        object_name: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._placeholder = placeholder
        self._source_pixmap: Optional[QPixmap] = None
        self.setObjectName(object_name)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setText(placeholder)
        self.setContentsMargins(8, 8, 8, 8)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(280, 220)

    def set_image(self, image: QImage) -> bool:
        if image is None or image.isNull():
            return False
        self._source_pixmap = QPixmap.fromImage(image)
        self._placeholder = ""
        self.setText("")
        self._refresh_pixmap()
        return True

    def clear_image(self, placeholder: Optional[str] = None) -> None:
        if placeholder is not None:
            self._placeholder = placeholder
        self._source_pixmap = None
        self.setPixmap(QPixmap())
        self.setText(self._placeholder)

    def _refresh_pixmap(self) -> None:
        if self._source_pixmap is None or self._source_pixmap.isNull():
            return
        target = self.contentsRect().size()
        target.setWidth(max(1, target.width() - 6))
        target.setHeight(max(1, target.height() - 6))
        if target.width() <= 1 or target.height() <= 1:
            return
        scaled = self._source_pixmap.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_pixmap()


# ===========================================================================
# 主窗口
# ===========================================================================


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(create_app_icon())
        self.setMinimumSize(940, 600)

        self._capture_dir, capture_warning = resolve_capture_dir()
        self._capture_warning = capture_warning or ""
        self._region: Optional[ScreenRegion] = None
        self._records: list[HistoryRecord] = []
        self._current_record: Optional[HistoryRecord] = None
        self._worker: Optional[MonitorWorker] = None
        self._selector: Optional[RegionSelector] = None
        self._monitoring = False
        self._selecting = False
        self._closing = False
        self._resume_after_select = False
        self._last_worker_error = ""
        self._has_started_once = False

        self._setup_ui()
        self._apply_theme()
        # 主窗口移动/缩放后，延迟检查它是否挡住了监控区域。
        self._region_hint_timer = QTimer(self)
        self._region_hint_timer.setSingleShot(True)
        self._region_hint_timer.setInterval(220)
        self._region_hint_timer.timeout.connect(self._refresh_region_hint)
        self._update_controls()
        self._set_scan_state("未监控", "#94A3B8")
        if self._capture_warning:
            self._set_status(self._capture_warning, "warning")
        else:
            self._set_status(
                "就绪：点击“选择监控区域”，在屏幕上拖拽框选二维码区域。"
                f"  截图目录：{self._capture_dir}"
            )

        self._fit_initial_window()

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _setup_ui(self) -> None:
        central = QWidget(self)
        central.setObjectName("centralWidget")
        # 给内容一个合理的最小高度；窗口更矮时由 QScrollArea 提供滚动，
        # 而不是把右侧图片/内容挤压重叠。高 DPI 小屏幕上尤其重要。
        central.setMinimumSize(900, 800)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 14, 18, 12)
        root.setSpacing(11)

        # 顶部品牌区
        header = QHBoxLayout()
        header.setSpacing(12)
        icon_label = QLabel()
        icon_label.setPixmap(create_app_icon().pixmap(42, 42))
        icon_label.setFixedSize(42, 42)
        icon_label.setObjectName("brandIcon")
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel(APP_NAME)
        title.setObjectName("appTitle")
        subtitle = QLabel(f"本地截图 · 本地识别 · {DISCLAIMER_SHORT}")
        subtitle.setObjectName("appSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addWidget(icon_label)
        header.addLayout(title_box)
        header.addStretch(1)
        version = QLabel(f"v{APP_VERSION}")
        version.setObjectName("versionLabel")
        header.addWidget(version)
        root.addLayout(header)

        # 控制栏
        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.start_button = QPushButton("开始监控")
        self.start_button.setProperty("role", "primary")
        self.start_button.setMinimumWidth(122)
        self.start_button.clicked.connect(self._on_start_pause_clicked)

        self.select_button = QPushButton("选择监控区域")
        self.select_button.setMinimumWidth(126)
        self.select_button.clicked.connect(self._on_select_region_clicked)

        self.clear_button = QPushButton("清空历史")
        self.clear_button.setMinimumWidth(92)
        self.clear_button.clicked.connect(self._clear_history)

        self.open_folder_button = QPushButton("打开截图目录")
        self.open_folder_button.setMinimumWidth(112)
        self.open_folder_button.setToolTip(str(self._capture_dir))
        self.open_folder_button.clicked.connect(self._open_capture_directory)

        self.top_checkbox = QCheckBox("窗口置顶")
        self.top_checkbox.setToolTip("让本窗口保持在其他窗口上方，便于边看视频边扫描。")
        self.top_checkbox.toggled.connect(self._set_always_on_top)

        self.about_button = QPushButton("使用说明 / 免责声明")
        self.about_button.setMinimumWidth(154)
        self.about_button.clicked.connect(self._show_about)

        controls.addWidget(self.start_button)
        controls.addWidget(self.select_button)
        controls.addSpacing(3)
        controls.addWidget(self.clear_button)
        controls.addWidget(self.open_folder_button)
        controls.addStretch(1)
        controls.addWidget(self.top_checkbox)
        controls.addWidget(self.about_button)
        root.addLayout(controls)

        # 上方主工作区：左预览，右二维码大图
        top_splitter = QSplitter(Qt.Orientation.Horizontal)
        top_splitter.setChildrenCollapsible(False)
        top_splitter.setHandleWidth(9)

        left_group = QGroupBox("监控区域预览 / 状态")
        left_layout = QVBoxLayout(left_group)
        left_layout.setContentsMargins(11, 18, 11, 8)
        left_layout.setSpacing(6)
        self.preview_view = ImageView(
            "尚未选择监控区域\n\n点击上方“选择监控区域”，\n在屏幕上拖拽框选二维码区域。",
            "previewView",
        )
        self.preview_view.setMinimumHeight(180)
        left_layout.addWidget(self.preview_view, 1)

        self.region_info = QLabel("区域：尚未选择")
        self.region_info.setObjectName("regionInfo")
        self.region_info.setWordWrap(True)
        left_layout.addWidget(self.region_info)

        self.region_hint = QLabel("可选任意显示器；每次框选负责一个显示器内的矩形。")
        self.region_hint.setObjectName("hintText")
        left_layout.addWidget(self.region_hint)

        preview_footer = QHBoxLayout()
        preview_footer.setSpacing(8)
        self.scan_state = QLabel("● 未监控")
        self.scan_state.setObjectName("scanState")
        preview_footer.addWidget(self.scan_state)
        preview_footer.addStretch(1)
        self.preview_rate = QLabel("选择区域后显示预览")
        self.preview_rate.setObjectName("hintText")
        preview_footer.addWidget(self.preview_rate)
        left_layout.addLayout(preview_footer)

        right_group = QGroupBox("最近识别到的二维码")
        right_layout = QVBoxLayout(right_group)
        right_layout.setContentsMargins(11, 18, 11, 8)
        right_layout.setSpacing(6)
        self.qr_view = ImageView(
            "等待检测二维码…\n\n识别后，这里会显示放大的二维码。\n"
            "二维码消失后仍会保留最近一次结果。",
            "qrView",
        )
        self.qr_view.setMinimumHeight(200)
        right_layout.addWidget(self.qr_view, 1)

        self.current_meta = QLabel("尚未捕获二维码")
        self.current_meta.setObjectName("currentMeta")
        self.current_meta.setWordWrap(True)
        self.current_meta.setFixedHeight(40)
        right_layout.addWidget(self.current_meta)

        content_label = QLabel("二维码内容（只读，仅供核对，不会自动打开）")
        content_label.setObjectName("sectionLabel")
        right_layout.addWidget(content_label)

        self.content_edit = QPlainTextEdit()
        self.content_edit.setReadOnly(True)
        self.content_edit.setPlaceholderText("解码后的文本会显示在这里。")
        self.content_edit.setFixedHeight(68)
        right_layout.addWidget(self.content_edit)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.save_button = QPushButton("保存当前二维码图片")
        self.save_button.setMinimumWidth(156)
        self.save_button.clicked.connect(self._save_current_qr)
        self.copy_button = QPushButton("复制二维码内容")
        self.copy_button.setMinimumWidth(130)
        self.copy_button.clicked.connect(self._copy_current_content)
        action_row.addWidget(self.save_button)
        action_row.addWidget(self.copy_button)
        action_row.addStretch(1)
        right_layout.addLayout(action_row)

        top_splitter.addWidget(left_group)
        top_splitter.addWidget(right_group)
        top_splitter.setStretchFactor(0, 4)
        top_splitter.setStretchFactor(1, 5)
        top_splitter.setSizes([520, 660])

        # 下方历史记录
        self.history_group = QGroupBox("历史记录（0/50）")
        history_layout = QVBoxLayout(self.history_group)
        history_layout.setContentsMargins(11, 18, 11, 8)
        history_layout.setSpacing(4)
        self.history_table = QTableWidget(0, 3)
        self.history_table.setHorizontalHeaderLabels(
            ["最近识别", "二维码内容摘要", "缩略图"]
        )
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.history_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.history_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.history_table.setAlternatingRowColors(True)
        self.history_table.setShowGrid(False)
        self.history_table.setIconSize(QSize(92, 52))
        self.history_table.setSortingEnabled(False)
        self.history_table.setWordWrap(False)
        self.history_table.verticalHeader().setDefaultSectionSize(62)
        header_view = self.history_table.horizontalHeader()
        header_view.setHighlightSections(False)
        header_view.setSectionsClickable(False)
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.history_table.setColumnWidth(0, 170)
        self.history_table.setColumnWidth(2, 126)
        self.history_table.setMinimumHeight(108)
        self.history_table.currentCellChanged.connect(self._on_history_current_changed)
        history_layout.addWidget(self.history_table, 1)

        history_hint = QLabel(
            "仅保留本次运行的最近 50 条记录。截图文件保存在 "
            f"{self._capture_dir}，清空历史不会删除已保存的截图。"
        )
        history_hint.setObjectName("hintText")
        history_hint.setWordWrap(True)
        history_layout.addWidget(history_hint)

        main_splitter = QSplitter(Qt.Orientation.Vertical)
        main_splitter.setChildrenCollapsible(False)
        main_splitter.setHandleWidth(9)
        main_splitter.addWidget(top_splitter)
        main_splitter.addWidget(self.history_group)
        main_splitter.setStretchFactor(0, 4)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setSizes([590, 190])
        root.addWidget(main_splitter, 1)

        # 可滚动容器：窗口较矮/高 DPI 小屏时滚动查看，而不压缩到控件重叠。
        scroll = QScrollArea(self)
        scroll.setObjectName("mainScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(central)
        scroll.viewport().setAutoFillBackground(False)
        self.setCentralWidget(scroll)

        # 状态栏
        status_bar = QStatusBar()
        status_bar.setSizeGripEnabled(True)
        self.setStatusBar(status_bar)
        self._status_bar = status_bar
        self.status_message = QLabel("")
        self.status_message.setObjectName("statusMessage")
        status_bar.addWidget(self.status_message, 1)
        self.fps_label = QLabel("采集 -- FPS")
        self.fps_label.setObjectName("statusChip")
        self.latency_label = QLabel("识别 -- ms")
        self.latency_label.setObjectName("statusChip")
        self.history_count_label = QLabel("历史 0/50")
        self.history_count_label.setObjectName("statusChip")
        status_bar.addPermanentWidget(self.fps_label)
        status_bar.addPermanentWidget(self.latency_label)
        status_bar.addPermanentWidget(self.history_count_label)

        self._update_action_buttons()

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                color: #172033;
                font-size: 13px;
            }
            QMainWindow, QWidget#centralWidget {
                background: #F2F5F9;
            }
            QLabel#brandIcon {
                background: transparent;
            }
            QLabel#appTitle {
                font-size: 24px;
                font-weight: 700;
                color: #101828;
            }
            QLabel#appSubtitle {
                color: #667085;
                font-size: 12px;
            }
            QLabel#versionLabel {
                color: #98A2B3;
                font-size: 11px;
                padding-right: 2px;
            }
            QGroupBox {
                background: #FFFFFF;
                border: 1px solid #DFE6EF;
                border-radius: 10px;
                margin-top: 12px;
                padding: 14px 4px 4px 4px;
                font-weight: 600;
                color: #344054;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 13px;
                padding: 0 6px;
                color: #475467;
                background: #F2F5F9;
            }
            QPushButton {
                background: #FFFFFF;
                border: 1px solid #CFD8E5;
                border-radius: 7px;
                padding: 7px 13px;
                min-height: 20px;
                color: #344054;
            }
            QPushButton:hover {
                background: #F8FAFC;
                border-color: #B6C3D5;
            }
            QPushButton:pressed {
                background: #EEF3F9;
            }
            QPushButton:disabled {
                color: #98A2B3;
                background: #F3F5F8;
                border-color: #E1E7EF;
            }
            QPushButton[role="primary"] {
                background: #2563EB;
                border-color: #2563EB;
                color: #FFFFFF;
                font-weight: 600;
            }
            QPushButton[role="primary"]:hover {
                background: #1D4ED8;
                border-color: #1D4ED8;
            }
            QPushButton[role="primary"]:pressed {
                background: #1E40AF;
                border-color: #1E40AF;
            }
            QPushButton[role="primary"]:disabled {
                background: #BFD2F8;
                border-color: #BFD2F8;
                color: #FFFFFF;
            }
            QCheckBox {
                color: #475467;
                spacing: 6px;
                padding: 4px 2px;
            }
            QLabel#previewView {
                background: #0B1220;
                border: 1px solid #1E293B;
                border-radius: 8px;
                color: #94A3B8;
                font-size: 13px;
            }
            QLabel#qrView {
                background: #FFFFFF;
                border: 1px solid #E4EAF2;
                border-radius: 8px;
                color: #98A2B3;
                font-size: 13px;
            }
            QLabel#regionInfo {
                color: #1D4ED8;
                font-size: 12px;
                font-weight: 600;
                padding: 1px 2px;
            }
            QLabel#currentMeta {
                color: #667085;
                font-size: 11px;
                padding: 0 2px;
            }
            QLabel#hintText {
                color: #98A2B3;
                font-size: 11px;
            }
            QLabel#scanState {
                font-size: 12px;
                font-weight: 600;
                color: #94A3B8;
            }
            QLabel#sectionLabel {
                color: #475467;
                font-size: 12px;
                font-weight: 600;
                padding-top: 1px;
            }
            QPlainTextEdit {
                background: #F8FAFC;
                border: 1px solid #E1E8F0;
                border-radius: 7px;
                padding: 7px;
                color: #1D2939;
                font-size: 12px;
                selection-background-color: #BFDBFE;
                selection-color: #0F172A;
            }
            QTableWidget {
                background: #FFFFFF;
                alternate-background-color: #F9FBFD;
                border: 1px solid #E5EAF1;
                border-radius: 8px;
                gridline-color: #EEF2F7;
                selection-background-color: #E8F0FF;
                selection-color: #172033;
                outline: 0;
            }
            QTableWidget::item {
                padding: 5px 8px;
                border-bottom: 1px solid #F1F4F8;
            }
            QTableWidget::item:selected {
                background: #E8F0FF;
                color: #172033;
            }
            QHeaderView::section {
                background: #F7F9FC;
                color: #667085;
                border: none;
                border-bottom: 1px solid #E5EAF1;
                padding: 8px;
                font-size: 11px;
                font-weight: 600;
            }
            QFrame, QSplitter {
                background: transparent;
            }
            QSplitter::handle {
                background: transparent;
            }
            QSplitter::handle:hover {
                background: #E4ECF6;
            }
            QStatusBar {
                background: #FFFFFF;
                border-top: 1px solid #E4EAF2;
                color: #667085;
            }
            QStatusBar::item {
                border: none;
            }
            QLabel#statusMessage {
                color: #667085;
                font-size: 11px;
                padding-left: 4px;
            }
            QLabel#statusChip {
                color: #475467;
                background: #F2F5F9;
                border: 1px solid #E4EAF2;
                border-radius: 6px;
                font-size: 11px;
                padding: 3px 8px;
                margin-left: 5px;
            }
            QToolTip {
                background: #101828;
                color: #F8FAFC;
                border: 1px solid #344054;
                padding: 5px;
            }
            QScrollBar:vertical {
                background: #EEF2F7;
                width: 11px;
                margin: 1px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical {
                background: #AAB7C8;
                border-radius: 4px;
                min-height: 24px;
                margin: 2px;
            }
            QScrollBar::handle:vertical:hover {
                background: #7C8CA3;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
            QScrollBar:horizontal {
                background: #EEF2F7;
                height: 11px;
                margin: 1px;
                border-radius: 5px;
            }
            QScrollBar::handle:horizontal {
                background: #AAB7C8;
                border-radius: 4px;
                min-width: 24px;
                margin: 2px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #7C8CA3;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0;
            }
            QScrollArea#mainScroll {
                border: none;
                background: #F2F5F9;
            }
            QScrollArea#mainScroll > QWidget > QWidget {
                background: #F2F5F9;
            }
            """
        )

    def _fit_initial_window(self) -> None:
        screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen else QRect(0, 0, 1280, 800)
        width = min(1320, max(940, available.width() - 80))
        height = min(900, max(600, available.height() - 70))
        self.resize(width, height)
        x = available.left() + max(0, (available.width() - width) // 2)
        y = available.top() + max(0, (available.height() - height) // 2)
        self.move(x, y)

    # ------------------------------------------------------------------
    # 状态与按钮
    # ------------------------------------------------------------------
    def _set_status(self, message: str, level: str = "info") -> None:
        colors = {
            "info": "#667085",
            "success": "#067647",
            "warning": "#B54708",
            "error": "#B42318",
        }
        self.status_message.setText(message)
        self.status_message.setStyleSheet(
            f"color: {colors.get(level, colors['info'])}; font-size: 11px; padding-left: 4px;"
        )
        self.status_message.setToolTip(message)

    def _set_scan_state(self, text: str, color: str) -> None:
        self.scan_state.setText(f"● {text}")
        self.scan_state.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {color};"
        )

    def _update_controls(self) -> None:
        if self._selecting:
            self.start_button.setEnabled(False)
            self.select_button.setEnabled(False)
        else:
            self.start_button.setEnabled(self._region is not None)
            self.select_button.setEnabled(True)

        if self._monitoring:
            self.start_button.setText("暂停监控")
            self.start_button.setProperty("role", "primary")
        else:
            self.start_button.setText(
                "继续监控" if self._has_started_once and self._region else "开始监控"
            )
            self.start_button.setProperty("role", "primary")
        self.start_button.style().unpolish(self.start_button)
        self.start_button.style().polish(self.start_button)

        self.clear_button.setEnabled(bool(self._records))
        self._update_action_buttons()
        self.history_count_label.setText(
            f"历史 {len(self._records)}/{HISTORY_LIMIT}"
        )

    def _update_action_buttons(self) -> None:
        has_image = (
            self._current_record is not None
            and not self._current_record.qr_image.isNull()
        )
        has_content = bool(
            self._current_record is not None and self._current_record.content
        )
        self.save_button.setEnabled(has_image)
        self.copy_button.setEnabled(has_content)

    def _update_region_widgets(self) -> None:
        if self._region is None:
            self.region_info.setText("区域：尚未选择")
            self.region_hint.setText("可选任意显示器；每次框选负责一个显示器内的矩形。")
            self.region_hint.setStyleSheet("")
            return
        self.region_info.setText(f"区域：{self._region.description()}")
        self._refresh_region_hint()
        self.preview_rate.setText("预览约 1 帧/秒")
        self.open_folder_button.setToolTip(str(self._capture_dir))

    def _main_window_overlaps_region(self) -> bool:
        """粗略检测主窗口是否遮挡监控区域（使用 Win32 物理窗口矩形）。"""
        if (
            self._region is None
            or os.name != "nt"
            or not self.isVisible()
            or self.isMinimized()
        ):
            return False
        try:
            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            rect = RECT()
            hwnd = int(self.winId())
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return False
            overlap_width = min(
                int(rect.right), self._region.left + self._region.width
            ) - max(int(rect.left), self._region.left)
            overlap_height = min(
                int(rect.bottom), self._region.top + self._region.height
            ) - max(int(rect.top), self._region.top)
            return overlap_width > 2 and overlap_height > 2
        except Exception:
            return False

    def _refresh_region_hint(self) -> None:
        if self._region is None:
            self.region_hint.setText("可选任意显示器；每次框选负责一个显示器内的矩形。")
            self.region_hint.setStyleSheet("")
            return
        if self._main_window_overlaps_region():
            self.region_hint.setText(
                "⚠ 主窗口与监控区域有重叠，可能遮挡二维码；请把本窗口移到区域外。"
            )
            self.region_hint.setStyleSheet(
                "color: #B54708; font-size: 11px; font-weight: 600;"
            )
        elif self._region.area > 4_000_000:
            self.region_hint.setText(
                "区域较大：二维码检测会更耗 CPU，建议缩小框选范围到二维码附近。"
            )
            self.region_hint.setStyleSheet("")
        else:
            self.region_hint.setText(
                "可选任意显示器；每次框选负责一个显示器内的矩形。请勿让主窗口遮挡监控区域。"
            )
            self.region_hint.setStyleSheet("")

    def _schedule_region_hint_refresh(self) -> None:
        timer = getattr(self, "_region_hint_timer", None)
        if timer is not None and self._region is not None:
            timer.start()

    def moveEvent(self, event: QMoveEvent) -> None:
        super().moveEvent(event)
        self._schedule_region_hint_refresh()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._schedule_region_hint_refresh()

    # ------------------------------------------------------------------
    # 选择监控区域
    # ------------------------------------------------------------------
    def _on_select_region_clicked(self) -> None:
        if self._selecting:
            return
        if self._closing:
            return
        was_monitoring = self._monitoring
        self._resume_after_select = was_monitoring
        self._stop_worker()
        self._monitoring = False
        if was_monitoring:
            self._set_status("已暂停监控，请框选新的区域。")
            self._set_scan_state("已暂停", "#F59E0B")
        else:
            self._set_status("请在屏幕上拖拽框选二维码区域；按 Esc 可取消。")
        self._selecting = True
        self._update_controls()
        # 隐藏主窗口，避免它遮挡框选区域；所有框选层都是置顶窗口。
        self.hide()
        QTimer.singleShot(120, self._launch_selector)

    def _launch_selector(self) -> None:
        if self._closing or not self._selecting:
            self.show()
            return
        try:
            selector = RegionSelector(self)
            self._selector = selector
            selector.region_selected.connect(self._on_region_selected)
            selector.cancelled.connect(self._on_region_selection_cancelled)
            selector.failed.connect(self._on_region_selection_failed)
            selector.start()
            # start() 可能在失败时同步发出 failed；槽函数已恢复窗口并释放 selector。
            # 这里不能再解引用可能已经置空的 self._selector。
        except Exception as exc:
            self._selecting = False
            if self._selector is not None:
                try:
                    self._selector.shutdown()
                except Exception:
                    pass
            self._selector = None
            self.show()
            self.raise_()
            QMessageBox.warning(self, "无法选择区域", f"启动框选层失败：{exc}")
            self._update_controls()
            self._set_status("区域选择失败，请重试。", "error")

    def _restore_main_window(self) -> None:
        if self._closing:
            return
        self.show()
        self.raise_()
        self.activateWindow()

    def _release_selector(self) -> None:
        selector = self._selector
        self._selector = None
        if selector is not None:
            selector.deleteLater()

    def _on_region_selected(self, region: ScreenRegion) -> None:
        if self._closing:
            return
        self._selecting = False
        self._release_selector()
        self._resume_after_select = False
        self._region = region
        self._update_region_widgets()
        self.preview_view.clear_image("正在获取区域预览…")
        self.preview_rate.setText("正在获取预览…")
        self._restore_main_window()
        self._region_hint_timer.start(80)
        self._set_status(
            "区域已选好，正在启动监控。请让二维码出现在框选区域内。",
            "success",
        )
        self._has_started_once = True
        self._start_monitoring()

    def _on_region_selection_cancelled(self) -> None:
        if self._closing:
            return
        self._selecting = False
        self._release_selector()
        self._restore_main_window()
        resume = self._resume_after_select and self._region is not None
        self._resume_after_select = False
        if resume:
            self._start_monitoring()
            self._set_status("已取消选择，继续使用原区域监控。", "warning")
        else:
            self._update_controls()
            self._set_status("已取消区域选择。", "warning")
            self._set_scan_state("已暂停" if self._region else "未监控", "#94A3B8")

    def _on_region_selection_failed(self, message: str) -> None:
        if self._closing:
            return
        self._selecting = False
        self._release_selector()
        self._restore_main_window()
        resume = self._resume_after_select and self._region is not None
        self._resume_after_select = False
        if resume:
            self._start_monitoring()
        else:
            self._update_controls()
            self._set_scan_state("未监控", "#94A3B8")
        self._set_status(f"区域选择失败：{message}", "error")
        QMessageBox.warning(self, "区域选择失败", message)

    def _set_always_on_top(self, checked: bool) -> None:
        """切换窗口置顶，保持窗口可见性/最大化状态不变。"""
        try:
            # setWindowFlag 会立即隐藏原生窗口；必须在调用前记录可见状态，
            # 否则之后再判断 isVisible() 会得到 False，窗口就不会重新显示。
            was_visible = self.isVisible()
            was_maximized = self.isMaximized()
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(checked))
            if was_visible:
                if was_maximized:
                    self.showMaximized()
                else:
                    self.show()
                self.raise_()
        except Exception as exc:
            self._set_status(f"切换窗口置顶失败：{exc}", "warning")

    # ------------------------------------------------------------------
    # 监控线程控制
    # ------------------------------------------------------------------
    def _on_start_pause_clicked(self) -> None:
        if self._monitoring:
            self._pause_monitoring()
        else:
            if self._region is None:
                QMessageBox.information(
                    self,
                    "尚未选择区域",
                    "请先点击“选择监控区域”，在屏幕上拖拽框选二维码区域。",
                )
                return
            self._start_monitoring()

    def _start_monitoring(self) -> None:
        if self._closing or self._monitoring:
            return
        if self._region is None:
            self._update_controls()
            return
        self._stop_worker()
        self._monitoring = True
        self._has_started_once = True
        self._last_worker_error = ""
        try:
            worker = MonitorWorker(
                region=self._region,
                capture_dir=self._capture_dir,
                interval_ms=DEFAULT_INTERVAL_MS,
            )
            worker.qr_detected.connect(self._on_detection)
            worker.repeated_seen.connect(self._on_repeated_seen)
            worker.preview_ready.connect(self._on_preview)
            worker.metrics.connect(self._on_metrics)
            worker.warning.connect(self._on_worker_warning)
            worker.fatal_error.connect(self._on_worker_fatal_error)
            worker.finished.connect(self._on_worker_finished)
            self._worker = worker
            worker.start()
        except Exception as exc:
            self._worker = None
            self._monitoring = False
            self._set_status(f"无法启动监控线程：{exc}", "error")
            self._update_controls()
            return

        self.preview_rate.setText("预览约 1 帧/秒")
        self.fps_label.setText("采集 -- FPS")
        self.latency_label.setText("识别 -- ms")
        self._set_scan_state("正在扫描", "#2563EB")
        if self._capture_warning:
            self._set_status(self._capture_warning, "warning")
        else:
            self._set_status("监控中：检测到二维码后会自动截图并放大显示。", "success")
        self._update_controls()

    def _pause_monitoring(self) -> None:
        if not self._monitoring and self._worker is None:
            self._update_controls()
            return
        self._stop_worker()
        self._monitoring = False
        self.fps_label.setText("采集 0.0 FPS")
        self.latency_label.setText("识别 -- ms")
        self.preview_rate.setText("预览已暂停")
        self._set_scan_state("已暂停", "#F59E0B")
        self._set_status("已暂停监控。已选择区域会保留，可随时继续。")
        self._update_controls()

    def _disconnect_worker_signals(self, worker: MonitorWorker) -> None:
        signals = (
            worker.qr_detected,
            worker.repeated_seen,
            worker.preview_ready,
            worker.metrics,
            worker.warning,
            worker.fatal_error,
            worker.finished,
        )
        for signal in signals:
            try:
                signal.disconnect()
            except Exception:
                pass

    def _stop_worker(self, wait_ms: int = 4000) -> None:
        worker = self._worker
        if worker is None:
            return
        self._worker = None
        self._disconnect_worker_signals(worker)
        if worker.isRunning():
            worker.request_stop()
            if not worker.wait(wait_ms):
                # 极端情况下的兜底，避免退出时出现 QThread destroyed while running。
                try:
                    worker.terminate()
                    worker.wait(1000)
                except Exception:
                    pass
        try:
            worker.deleteLater()
        except Exception:
            pass

    def _on_worker_finished(self) -> None:
        worker = self.sender()
        if worker is not self._worker:
            return
        self._worker = None
        try:
            worker.deleteLater()
        except Exception:
            pass
        self._monitoring = False
        self.fps_label.setText("采集 0.0 FPS")
        self.latency_label.setText("识别 -- ms")
        self.preview_rate.setText("预览已暂停")
        if self._last_worker_error:
            self._set_scan_state("监控已停止", "#B42318")
            self._set_status(
                f"监控已停止：{self._last_worker_error}", "error"
            )
        else:
            self._set_scan_state("已停止", "#94A3B8")
            self._set_status("监控线程已停止。可以重新开始监控。", "warning")
        self._update_controls()

    def _on_worker_warning(self, message: str) -> None:
        self._set_status(message, "warning")

    def _on_worker_fatal_error(self, message: str) -> None:
        self._last_worker_error = message
        self._set_status(f"监控异常：{message}", "error")
        self._set_scan_state("发生错误", "#B42318")

    # ------------------------------------------------------------------
    # 后台信号 -> 界面
    # ------------------------------------------------------------------
    def _on_preview(self, image: QImage) -> None:
        if self._closing:
            return
        if image is not None and not image.isNull():
            self.preview_view.set_image(image)

    def _on_metrics(self, fps: float, detect_ms: float, found: bool) -> None:
        if self._closing or not self._monitoring:
            return
        self.fps_label.setText(f"采集 {fps:.1f} FPS")
        self.latency_label.setText(f"识别 {detect_ms:.1f} ms")
        if found:
            self._set_scan_state("检测到二维码", "#067647")
        else:
            self._set_scan_state("正在扫描", "#2563EB")

    def _on_detection(self, payload: DetectionPayload) -> None:
        if self._closing:
            return
        record = HistoryRecord(
            content=payload.content,
            content_key=payload.content_key,
            qr_image=payload.qr_image,
            screenshot_path=payload.screenshot_path,
            first_seen=payload.detected_at,
            last_seen=payload.detected_at,
            region_width=payload.region_width,
            region_height=payload.region_height,
            screen_name=payload.screen_name,
            save_error=payload.save_error,
        )
        self._insert_history_record(record)
        self._display_record(record)
        self._set_scan_state("检测到二维码", "#067647")
        if self._monitoring:
            if record.screenshot_path is not None:
                self._set_status(
                    f"识别成功，已保存区域截图：{record.screenshot_path.name}",
                    "success",
                )
            elif record.save_error:
                self._set_status(
                    f"识别成功，但原始截图保存失败：{record.save_error}",
                    "warning",
                )
            else:
                self._set_status("识别成功，二维码已放大显示。", "success")
        # 让历史表在视觉上跟随最新记录。
        if self._records:
            self.history_table.selectRow(0)

    def _insert_history_record(self, record: HistoryRecord) -> None:
        self._records.insert(0, record)
        self.history_table.blockSignals(True)
        try:
            self.history_table.insertRow(0)
            time_item = QTableWidgetItem(format_timestamp(record.last_seen))
            time_item.setData(Qt.ItemDataRole.UserRole, 0)
            time_item.setToolTip(
                f"首次识别：{format_timestamp(record.first_seen)}\n"
                f"最近识别：{format_timestamp(record.last_seen)}\n"
                f"截图：{record.screenshot_path or '未保存'}"
            )
            content_item = QTableWidgetItem(record.summary())
            tooltip = record.content or "（未能解码内容）"
            if len(tooltip) > 3000:
                tooltip = tooltip[:3000] + "\n…（内容较长，完整内容见右侧只读文本框）"
            content_item.setToolTip(tooltip)
            thumb = QPixmap.fromImage(record.qr_image).scaled(
                QSize(92, 52),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            thumb_item = QTableWidgetItem()
            thumb_item.setIcon(QIcon(thumb))
            thumb_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            thumb_item.setToolTip(str(record.screenshot_path or "未保存截图"))
            self.history_table.setItem(0, 0, time_item)
            self.history_table.setItem(0, 1, content_item)
            self.history_table.setItem(0, 2, thumb_item)
        finally:
            self.history_table.blockSignals(False)

        while len(self._records) > HISTORY_LIMIT:
            self._records.pop()
            self.history_table.removeRow(self.history_table.rowCount() - 1)
        if self._current_record is not None and self._current_record not in self._records:
            # 当前大图仍可显示，但历史条目已按 50 条上限淘汰；保持右侧显示即可。
            pass
        self.history_group.setTitle(f"历史记录（{len(self._records)}/{HISTORY_LIMIT}）")
        self._update_controls()

    def _on_repeated_seen(self, content_key: str, when: object) -> None:
        """同一二维码持续可见/短时重复：只更新已有记录时间，不新增行。"""
        if self._closing or not isinstance(when, datetime):
            return
        for index, record in enumerate(self._records):
            if record.content_key != content_key:
                continue
            record.last_seen = when
            time_item = self.history_table.item(index, 0)
            if time_item is not None:
                time_item.setText(format_timestamp(when))
                time_item.setToolTip(
                    f"首次识别：{format_timestamp(record.first_seen)}\n"
                    f"最近识别：{format_timestamp(record.last_seen)}\n"
                    f"截图：{record.screenshot_path or '未保存'}"
                )
            if self._current_record is record:
                self._refresh_current_meta()
            break

    def _refresh_current_meta(self) -> None:
        record = self._current_record
        if record is None:
            self.current_meta.setText("尚未捕获二维码")
            return
        size_text = (
            f"{record.region_width} × {record.region_height} 像素"
            if record.region_width > 0 and record.region_height > 0
            else "未知区域尺寸"
        )
        if record.screenshot_path is not None:
            screenshot_name = record.screenshot_path.name
            screenshot_tooltip = str(record.screenshot_path)
        elif record.save_error:
            screenshot_name = "保存失败"
            screenshot_tooltip = record.save_error
        else:
            screenshot_name = "未保存"
            screenshot_tooltip = ""
        self.current_meta.setText(
            f"首次 {format_timestamp(record.first_seen)}  ·  "
            f"最近 {format_timestamp(record.last_seen)}\n"
            f"{size_text}  ·  原始截图：{screenshot_name}"
        )
        self.current_meta.setToolTip(screenshot_tooltip)

    def _display_record(self, record: HistoryRecord) -> None:
        self._current_record = record
        shown = self.qr_view.set_image(record.qr_image)
        if not shown:
            self.qr_view.clear_image("无法显示二维码图片")
        if record.content:
            self.content_edit.setPlainText(record.content)
        else:
            self.content_edit.setPlainText("")
            self.content_edit.setPlaceholderText(
                "已检测到二维码，但 OpenCV 未能解码内容。二维码大图仍可保存。"
            )
        self._refresh_current_meta()
        self._update_action_buttons()

    def _on_history_current_changed(
        self, current_row: int, _current_column: int, _previous_row: int, _previous_column: int
    ) -> None:
        if 0 <= current_row < len(self._records):
            self._display_record(self._records[current_row])

    # ------------------------------------------------------------------
    # 历史记录与文件操作
    # ------------------------------------------------------------------
    def _clear_history(self) -> None:
        if not self._records:
            return
        answer = QMessageBox.question(
            self,
            "清空历史",
            "确定清空当前会话的历史记录吗？\n\n"
            "captures/ 中已经保存的截图不会被删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.history_table.setRowCount(0)
        self._records.clear()
        self._current_record = None
        self.qr_view.clear_image(
            "等待检测二维码…\n\n识别后，这里会显示放大的二维码。"
        )
        self.content_edit.clear()
        self.content_edit.setPlaceholderText("解码后的文本会显示在这里。")
        self.current_meta.setText("尚未捕获二维码")
        self.history_group.setTitle(f"历史记录（0/{HISTORY_LIMIT}）")
        self._update_controls()
        self._set_status("历史记录已清空（截图文件未删除）。", "info")

    def _open_capture_directory(self) -> None:
        try:
            _check_writable_directory(self._capture_dir)
        except OSError as exc:
            QMessageBox.warning(
                self,
                "无法打开截图目录",
                f"截图目录不可写：\n{self._capture_dir}\n\n{exc}",
            )
            return
        opened = QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self._capture_dir))
        )
        if not opened:
            QMessageBox.warning(
                self,
                "无法打开目录",
                f"系统未能打开文件资源管理器。请手动打开：\n{self._capture_dir}",
            )

    def _save_current_qr(self) -> None:
        record = self._current_record
        if record is None or record.qr_image.isNull():
            QMessageBox.information(self, "没有可保存的二维码", "请先捕获一个二维码。")
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = self._capture_dir / f"qr_{stamp}.png"
        path_text, selected_filter = QFileDialog.getSaveFileName(
            self,
            "保存当前二维码图片",
            str(default_path),
            "PNG 图片 (*.png);;JPEG 图片 (*.jpg *.jpeg)",
        )
        if not path_text:
            return
        path = Path(path_text)
        suffix = path.suffix.lower()
        if not suffix:
            path = path.with_suffix(".jpg" if "JPEG" in selected_filter else ".png")
            suffix = path.suffix.lower()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            image_format = "PNG"
            if suffix in (".jpg", ".jpeg"):
                image_format = "JPG"
            success = record.qr_image.save(str(path), image_format)
            if not success:
                raise OSError("Qt 图片编码器返回失败")
            self._set_status(f"二维码图片已保存：{path}", "success")
        except Exception as exc:
            QMessageBox.warning(
                self,
                "保存失败",
                f"无法保存二维码图片：\n{path}\n\n{exc}",
            )
            self._set_status(f"二维码图片保存失败：{exc}", "error")

    def _copy_current_content(self) -> None:
        record = self._current_record
        if record is None or not record.content:
            QMessageBox.information(
                self,
                "没有可复制的内容",
                "当前二维码没有成功解码的文本内容。",
            )
            return
        clipboard = QGuiApplication.clipboard()
        clipboard.setText(record.content)
        self._set_status("二维码内容已复制（程序不会自动打开它）。", "success")

    def _show_about(self) -> None:
        QMessageBox.information(
            self,
            "使用说明 / 免责声明",
            f"{DISCLAIMER_TEXT}\n\n"
            "使用方法：\n"
            "1. 点击“选择监控区域”，在屏幕上拖拽框选。\n"
            "2. 程序每 150 毫秒在后台截取一次该区域并本地识别。\n"
            "3. 检测到二维码后，右侧显示放大图，同时保存原始区域截图。\n"
            "4. 同一二维码持续出现时只更新时间，不重复新增历史。\n\n"
            f"截图目录：{self._capture_dir}\n"
            f"版本：{APP_VERSION}",
        )

    # ------------------------------------------------------------------
    # 窗口事件
    # ------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self._selecting = False
        try:
            self._region_hint_timer.stop()
        except Exception:
            pass
        if self._selector is not None:
            try:
                self._selector.shutdown()
            except Exception:
                pass
            self._selector = None
        self._stop_worker()
        self._monitoring = False
        event.accept()


# ===========================================================================
# 应用图标与入口
# ===========================================================================


def create_app_icon() -> QIcon:
    """在内存中绘制一个简洁的二维码图标，避免依赖外部资源文件。"""
    sizes = [16, 24, 32, 48, 64, 128, 256]
    icon = QIcon()
    for size in sizes:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        radius = max(2.0, size * 0.20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#1D4ED8"))
        painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

        color = QColor("#FFFFFF")
        painter.setBrush(color)
        margin = size * 0.17
        finder = size * 0.14
        gap = size * 0.055
        painter.drawRoundedRect(
            QRectF(margin, margin, finder, finder), size * 0.03, size * 0.03
        )
        painter.setBrush(QColor("#1D4ED8"))
        painter.drawRoundedRect(
            QRectF(
                margin + finder * 0.28,
                margin + finder * 0.28,
                finder * 0.44,
                finder * 0.44,
            ),
            size * 0.015,
            size * 0.015,
        )
        painter.setBrush(color)
        painter.drawRoundedRect(
            QRectF(size - margin - finder, margin, finder, finder),
            size * 0.03,
            size * 0.03,
        )
        painter.setBrush(QColor("#1D4ED8"))
        painter.drawRoundedRect(
            QRectF(
                size - margin - finder + finder * 0.28,
                margin + finder * 0.28,
                finder * 0.44,
                finder * 0.44,
            ),
            size * 0.015,
            size * 0.015,
        )
        painter.setBrush(color)
        painter.drawRoundedRect(
            QRectF(margin, size - margin - finder, finder, finder),
            size * 0.03,
            size * 0.03,
        )
        painter.setBrush(QColor("#1D4ED8"))
        painter.drawRoundedRect(
            QRectF(
                margin + finder * 0.28,
                size - margin - finder + finder * 0.28,
                finder * 0.44,
                finder * 0.44,
            ),
            size * 0.015,
            size * 0.015,
        )

        painter.setBrush(color)
        # 右下角的几个“数据模块”，让图标更像二维码。
        block = max(1.2, size * 0.055)
        start_x = size - margin - finder
        start_y = size - margin - finder
        for row in range(2):
            for col in range(2):
                if (row + col) % 2 == 0:
                    painter.drawRoundedRect(
                        QRectF(
                            start_x + col * (block + gap),
                            start_y + row * (block + gap),
                            block,
                            block,
                        ),
                        block * 0.2,
                        block * 0.2,
                    )
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def install_exception_hook() -> None:
    """把未捕获异常尽量显示给用户，而不是只打印到可能不存在的控制台。"""

    def _hook(exc_type, exc_value, exc_tb):  # type: ignore[no-untyped-def]
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        details = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            print(details, file=sys.stderr)
        except Exception:
            pass
        try:
            QMessageBox.critical(
                None,
                "程序发生未处理的错误",
                f"{exc_type.__name__}: {exc_value}\n\n"
                "如果问题持续出现，请查看 README 的故障排查部分。",
            )
        except Exception:
            pass

    sys.excepthook = _hook


def _configure_windows_app_id() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "LocalTools.QrScreenCaptureAssistant.1"
        )
    except Exception:
        pass


def main() -> int:
    _configure_windows_app_id()

    # Qt 6 默认启用高 DPI。PassThrough 可避免 125%/150% 等比例被四舍五入，
    # 框选坐标随后会通过物理显示器矩形做精确换算。
    try:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("LocalTools")
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")
    app.setWindowIcon(create_app_icon())
    try:
        app.setFont(QFont("Microsoft YaHei UI", 10))
    except Exception:
        pass

    install_exception_hook()
    window = MainWindow()
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
