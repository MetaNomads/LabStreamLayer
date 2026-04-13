"""
gui/main_window.py  -  dark industrial design
"""

import socket
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from PyQt6.QtCore import Qt, QPointF, QRectF, QSettings, QTimer, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFrame, QMessageBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QPlainTextEdit,
    QPushButton, QSizePolicy, QSpinBox,
    QCheckBox, QSplitter, QVBoxLayout, QWidget,
)

import platform as _platform

from emotibit    import EmotiBitDevice, EmotiBitHandler, EmotiBitStatus
from sync_logger import SyncLogger
from unity       import UnityHandler

if _platform.system() == "Darwin":
    from polar_mac import PolarDevice, PolarHandler, PolarStatus
else:
    from polar     import PolarDevice, PolarHandler, PolarStatus

BG    = "#0f1117"
PANEL = "#1a1d27"
BDR   = "#2a2d3a"
TEXT  = "#e2e4ed"
DIM   = "#5a5e72"
RED   = "#e05c5c"
GREEN = "#3ecf8e"
AMBER = "#f0a040"
BLUE  = "#4a9eff"
WHITE = "#ffffff"


# ── Reusable widgets ────────────────────────────────────────────────────────────

class DeviceRow(QWidget):
    """
    Clean device row: [✓] Name   Status text   [Connect] [Disconnect]
    No box frame, no dot — plain flat row.
    """
    def __init__(self, name: str, on_scan, on_disconnect, parent=None):
        super().__init__(parent)
        self.setFixedHeight(36)
        # Transparent background — sits inside the panel without inner border
        self.setStyleSheet("background:transparent;")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        # Required checkbox (no label — implied by position)
        self._chk = QCheckBox()
        self._chk.setChecked(True)
        self._chk.setToolTip("Required for recording")
        self._chk.setStyleSheet(f"""
            QCheckBox::indicator {{
                width:14px; height:14px;
                border:1px solid {DIM};
                border-radius:3px;
                background:{BG};
            }}
            QCheckBox::indicator:checked {{
                background:{BLUE};
                border-color:{BLUE};
            }}
        """)

        # Device name
        name_lbl = QLabel(name)
        name_lbl.setFixedWidth(80)
        name_lbl.setStyleSheet(
            f"font-size:11px;font-weight:700;color:{TEXT};background:transparent;"
        )

        # Battery label
        self._batt_lbl = QLabel("")
        self._batt_lbl.setFixedWidth(44)
        self._batt_lbl.setStyleSheet(
            f"font-size:10px;color:{DIM};background:transparent;"
        )

        # Status — plain text, no border
        self._status = QLabel("Not connected")
        self._status.setStyleSheet(
            f"font-size:10px;color:{RED};background:transparent;"
        )

        # Buttons
        self._btn_scan = ABtn("Connect", BLUE)
        self._btn_scan.setMinimumHeight(28)
        self._btn_scan.setFixedWidth(84)
        self._btn_disc = GBtn("Disconnect")
        self._btn_disc.setMinimumHeight(28)
        self._btn_disc.setFixedWidth(84)
        self._btn_disc.setEnabled(False)
        self._btn_scan.clicked.connect(on_scan)
        self._btn_disc.clicked.connect(on_disconnect)

        row.addWidget(self._chk)
        row.addWidget(name_lbl)
        row.addWidget(self._batt_lbl)
        row.addWidget(self._status, stretch=1)
        row.addWidget(self._btn_scan)
        row.addWidget(self._btn_disc)

    def set_status(self, text: str, color: str):
        self._status.setStyleSheet(
            f"font-size:10px;font-weight:600;color:{color};background:transparent;"
        )
        self._status.setText(text)
        connected = color == GREEN or "recording" in text.lower() or "connected" in text.lower()
        self._btn_disc.setEnabled(connected)
        self._btn_scan.setText("Re-connect" if connected else "Connect")

    def set_battery(self, pct: int):
        if pct < 0:
            self._batt_lbl.setText("")
            return
        if pct <= 20:
            color = RED
        elif pct <= 50:
            color = AMBER
        else:
            color = GREEN
        self._batt_lbl.setStyleSheet(
            f"font-size:10px;font-weight:600;color:{color};background:transparent;"
        )
        self._batt_lbl.setText(f"🔋{pct}%")

    @property
    def is_required(self) -> bool:
        return self._chk.isChecked()

    @property
    def required_checkbox(self) -> QCheckBox:
        return self._chk


class StreamGraph(QWidget):
    """
    Scrolling 5-second line graph for a single sensor stream.
    Push values via push(value). Redraws at ~20 Hz via internal timer.
    """

    def __init__(self, label: str, color: str, unit: str = "", parent=None):
        super().__init__(parent)
        self._label  = label
        self._color  = QColor(color)
        self._unit   = unit
        self._data   = deque()          # (monotonic_time, value)
        self._window = 8.0              # seconds shown
        self._has_data = False
        self.setFixedHeight(64)
        self.setMinimumWidth(80)
        self.setStyleSheet(
            f"background:#0d0f18;border:1px solid {BDR};border-radius:4px;"
        )
        # Redraw timer
        t = QTimer(self)
        t.setInterval(50)               # 20 Hz
        t.timeout.connect(self.update)
        t.start()

    def push(self, value: float):
        now = time.monotonic()
        self._data.append((now, value))
        self._has_data = True
        # Trim old data
        cutoff = now - self._window - 0.5
        while self._data and self._data[0][0] < cutoff:
            self._data.popleft()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 6, 6, 14, 6

        # Background
        painter.fillRect(0, 0, w, h, QColor("#0d0f18"))

        # Label (top-left)
        painter.setPen(QColor(DIM))
        f = QFont()
        f.setPointSize(8)
        painter.setFont(f)
        painter.drawText(pad_l, 10, self._label)

        if not self._has_data:
            painter.setPen(QColor(DIM))
            painter.drawText(w // 2 - 16, h // 2 + 4, "no data")
            return

        now = time.monotonic()
        cutoff = now - self._window
        pts = [(t, v) for t, v in self._data if t >= cutoff]

        if len(pts) < 2:
            return

        values = [v for _, v in pts]
        times  = [t for t, _ in pts]
        vmin, vmax = min(values), max(values)
        if abs(vmax - vmin) < 1e-9:
            vmax = vmin + 1

        gw = w - pad_l - pad_r
        gh = h - pad_t - pad_b

        def tx(t):  return pad_l + (t - cutoff) / self._window * gw
        def ty(v):  return pad_t + gh - (v - vmin) / (vmax - vmin) * gh

        # Zero line (dimmed)
        if vmin < 0 < vmax:
            painter.setPen(QPen(QColor(BDR), 1))
            zy = ty(0)
            painter.drawLine(QPointF(pad_l, zy), QPointF(w - pad_r, zy))

        # Data line
        path = QPainterPath()
        path.moveTo(QPointF(tx(times[0]), ty(values[0])))
        for i in range(1, len(pts)):
            path.lineTo(QPointF(tx(times[i]), ty(values[i])))

        painter.setPen(QPen(self._color, 1.5))
        painter.drawPath(path)

        # Current value (top-right)
        last_val = values[-1]
        val_str = f"{last_val:.1f} {self._unit}".strip()
        painter.setPen(self._color)
        fm = painter.fontMetrics()
        painter.drawText(w - fm.horizontalAdvance(val_str) - pad_r, 10, val_str)


class Divider(QFrame):
    def __init__(self, p=None):
        super().__init__(p)
        self.setFrameShape(QFrame.Shape.HLine)
        self.setStyleSheet(f"background:{BDR};")
        self.setFixedHeight(1)


class SLabel(QLabel):
    def __init__(self, text, p=None):
        super().__init__(text.upper(), p)
        self.setStyleSheet(
            f"font-size:9px;font-weight:700;color:{DIM};"
            "letter-spacing:2px;background:transparent;"
        )


class ABtn(QPushButton):
    def __init__(self, text, color=BLUE, p=None):
        super().__init__(text, p)
        self.setMinimumHeight(38)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton {{
                background:{color};color:{WHITE};border:none;
                border-radius:6px;font-size:12px;font-weight:700;
                letter-spacing:0.5px;padding:0 16px;
            }}
            QPushButton:hover   {{ background:{color}cc; }}
            QPushButton:pressed {{ background:{color}88; }}
            QPushButton:disabled {{ background:{BDR};color:{DIM}; }}
        """)


class GBtn(QPushButton):
    def __init__(self, text, p=None):
        super().__init__(text, p)
        self.setMinimumHeight(32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton {{
                background:transparent;color:{TEXT};
                border:1px solid {BDR};border-radius:5px;
                font-size:11px;padding:0 10px;
            }}
            QPushButton:hover {{ border-color:{DIM};background:{PANEL}; }}
            QPushButton:disabled {{ color:{DIM}; }}
        """)


class DeviceCard(QWidget):
    """Compact horizontal status card for a single device."""

    def __init__(self, icon, name, p=None):
        super().__init__(p)
        self.setFixedHeight(68)
        self.setStyleSheet(
            f"background:{PANEL};border:1px solid {BDR};border-radius:8px;"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(10)

        left = QVBoxLayout()
        left.setSpacing(1)
        il = QLabel(icon)
        il.setStyleSheet("font-size:16px;border:none;background:transparent;")
        nl = QLabel(name.upper())
        nl.setStyleSheet(
            f"font-size:9px;font-weight:700;color:{DIM};"
            "letter-spacing:1.5px;border:none;background:transparent;"
        )
        left.addWidget(il)
        left.addWidget(nl)

        right = QVBoxLayout()
        right.setSpacing(2)
        right.setAlignment(Qt.AlignmentFlag.AlignRight)

        sr = QHBoxLayout()
        sr.setSpacing(5)
        sr.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._dot = QLabel("●")
        self._dot.setStyleSheet(
            f"font-size:9px;color:{RED};border:none;background:transparent;"
        )
        self._sl = QLabel("NOT FOUND")
        self._sl.setStyleSheet(
            f"font-size:11px;font-weight:700;color:{RED};"
            "letter-spacing:0.5px;border:none;background:transparent;"
        )
        sr.addWidget(self._dot)
        sr.addWidget(self._sl)

        self._dl = QLabel("---")
        self._dl.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._dl.setStyleSheet(
            f"font-size:10px;color:{DIM};border:none;background:transparent;"
        )
        right.addLayout(sr)
        right.addWidget(self._dl)

        row.addLayout(left)
        row.addStretch()
        row.addLayout(right)

    def set_status(self, text, color, detail=""):
        base = "border:none;background:transparent;"
        self._dot.setStyleSheet(f"font-size:9px;color:{color};{base}")
        self._sl.setStyleSheet(
            f"font-size:11px;font-weight:700;color:{color};"
            f"letter-spacing:0.5px;{base}"
        )
        self._sl.setText(text.upper())
        self._dl.setText(detail)


# ── EmotiBit device picker dialog ───────────────────────────────────────────────

class EmotiBitPickerDialog(QDialog):
    """
    Scan dialog showing discovered EmotiBit devices.
    Also supports manual IP entry when devices are on a different subnet.
    """

    def __init__(self, handler: EmotiBitHandler, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to EmotiBit")
        self.setMinimumWidth(500)
        self.setMinimumHeight(380)
        self.setStyleSheet(f"""
            QDialog {{ background:{BG};color:{TEXT}; }}
            QLabel  {{ background:transparent; }}
        """)

        self._handler = handler
        self._devices = []
        self.selected_device = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        # Title
        title = QLabel("EmotiBit Device List")
        title.setStyleSheet(f"font-size:13px;font-weight:700;color:{WHITE};")
        layout.addWidget(title)

        hint = QLabel(
            "Click Scan to search all reachable subnets. "
            "Devices show their IP and MAC address. "
            "If not found, enter the IP manually below "
            "(check your router device list for the EmotiBit IP)."
        )
        hint.setStyleSheet(f"font-size:11px;color:{DIM};")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # Device list
        self._list = QListWidget()
        self._list.setStyleSheet(f"""
            QListWidget {{
                background:{PANEL};border:1px solid {BDR};
                border-radius:6px;color:{TEXT};font-size:12px;outline:none;
            }}
            QListWidget::item {{
                padding:10px 12px;border-bottom:1px solid {BDR};
            }}
            QListWidget::item:selected {{
                background:{BLUE}33;color:{WHITE};border-left:3px solid {BLUE};
            }}
            QListWidget::item:hover {{ background:{PANEL}; }}
        """)
        self._list.itemSelectionChanged.connect(self._on_selection)
        layout.addWidget(self._list, stretch=1)

        # Status
        self._status_lbl = QLabel("No devices found yet. Click Scan.")
        self._status_lbl.setStyleSheet(f"font-size:11px;color:{DIM};")
        layout.addWidget(self._status_lbl)

        # Manual IP entry
        layout.addWidget(Divider())
        manual_lbl = QLabel("Manual IP (if scan fails):")
        manual_lbl.setStyleSheet(f"font-size:11px;color:{DIM};")
        layout.addWidget(manual_lbl)
        manual_row = QHBoxLayout()
        manual_row.setSpacing(8)
        self._ip_edit = QLineEdit()
        self._ip_edit.setPlaceholderText("e.g.  192.168.2.45")
        self._ip_edit.setStyleSheet(
            f"background:{PANEL};border:1px solid {BDR};border-radius:5px;"
            f"color:{TEXT};font-size:12px;padding:5px 8px;"
        )
        self._btn_add = GBtn("Add")
        self._btn_add.setFixedWidth(60)
        self._btn_add.clicked.connect(self._add_manual)
        manual_row.addWidget(self._ip_edit, stretch=1)
        manual_row.addWidget(self._btn_add)
        layout.addLayout(manual_row)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._btn_scan    = ABtn("Scan", BLUE)
        self._btn_connect = ABtn("Connect", GREEN)
        self._btn_cancel  = GBtn("Cancel")
        self._btn_connect.setEnabled(False)
        self._btn_scan.clicked.connect(self._do_scan)
        self._btn_connect.clicked.connect(self._do_connect)
        self._btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self._btn_scan)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_cancel)
        btn_row.addWidget(self._btn_connect)
        layout.addLayout(btn_row)

        self._handler.devices_updated.connect(self._on_devices_updated)

        self._scan_timer = QTimer(self)
        self._scan_timer.setSingleShot(True)
        self._scan_timer.timeout.connect(lambda: self._btn_scan.setEnabled(True))

    def _do_scan(self):
        self._list.clear()
        self._devices.clear()
        self._btn_scan.setEnabled(False)
        self._btn_connect.setEnabled(False)
        self._status_lbl.setText("Scanning all subnets...")
        self._status_lbl.setStyleSheet(f"font-size:11px;color:{AMBER};")
        self._handler.scan(duration=5.0)
        self._scan_timer.start(6000)

    def _on_devices_updated(self, devices: list):
        self._devices = devices
        self._list.clear()
        for dev in devices:
            item = QListWidgetItem(dev.display_name)
            item.setData(Qt.ItemDataRole.UserRole, dev)
            self._list.addItem(item)
        count = len(devices)
        self._status_lbl.setText(
            f"{count} device(s) found" if count else
            "No devices found. Try entering the IP manually below."
        )
        color = GREEN if count else RED
        self._status_lbl.setStyleSheet(f"font-size:11px;color:{color};")

    def _add_manual(self):
        ip = self._ip_edit.text().strip()
        if not ip:
            return
        # Basic validation
        try:
            socket.inet_aton(ip)
        except socket.error:
            self._status_lbl.setText("Invalid IP address.")
            self._status_lbl.setStyleSheet(f"font-size:11px;color:{RED};")
            return
        dev = self._handler.add_manual_device(ip)
        # Select the newly added item automatically
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(Qt.ItemDataRole.UserRole).ip == ip:
                self._list.setCurrentItem(item)
                break
        self._ip_edit.clear()

    def _on_selection(self):
        self._btn_connect.setEnabled(len(self._list.selectedItems()) > 0)

    def _do_connect(self):
        items = self._list.selectedItems()
        if items:
            self.selected_device = items[0].data(Qt.ItemDataRole.UserRole)
            self.accept()



# ── Polar device picker dialog ───────────────────────────────────────────────────

class PolarPickerDialog(QDialog):
    """
    Scan for nearby Polar H10 devices via BLE.
    Shows discovered devices in a list — click to select, then Connect.
    Manual serial number entry as fallback if scan misses the device.
    """

    def __init__(self, handler: PolarHandler, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to Polar H10")
        self.setMinimumWidth(500)
        self.setMinimumHeight(420)
        self.setStyleSheet(f"""
            QDialog {{ background:{BG};color:{TEXT}; }}
            QLabel  {{ background:transparent; }}
        """)
        self._handler = handler
        self.selected_device = None
        self._devices = []

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        # Title
        title = QLabel("Polar H10 — BLE Scan")
        title.setStyleSheet(f"font-size:13px;font-weight:700;color:{WHITE};")
        layout.addWidget(title)

        hint = QLabel(
            "Wear the H10 strap so the electrodes detect skin contact — "
            "the device only advertises when worn. Click Scan to discover nearby devices."
        )
        hint.setStyleSheet(f"font-size:11px;color:{DIM};")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # Device list
        self._list = QListWidget()
        self._list.setStyleSheet(f"""
            QListWidget {{
                background:{PANEL};border:1px solid {BDR};
                border-radius:6px;color:{TEXT};font-size:12px;outline:none;
            }}
            QListWidget::item {{
                padding:10px 12px;border-bottom:1px solid {BDR};
            }}
            QListWidget::item:selected {{
                background:{BLUE}33;color:{WHITE};border-left:3px solid {BLUE};
            }}
        """)
        self._list.itemSelectionChanged.connect(self._on_selection)
        layout.addWidget(self._list, stretch=1)

        # Status label
        self._status_lbl = QLabel("Click Scan to find nearby H10 devices.")
        self._status_lbl.setStyleSheet(f"font-size:11px;color:{DIM};")
        layout.addWidget(self._status_lbl)

        layout.addWidget(Divider())

        # Manual fallback
        manual_lbl = QLabel("Manual entry (if scan misses device):")
        manual_lbl.setStyleSheet(f"font-size:11px;color:{DIM};")
        layout.addWidget(manual_lbl)

        manual_row = QHBoxLayout()
        manual_row.setSpacing(8)

        self._sn_edit = QLineEdit()
        self._sn_edit.setPlaceholderText("Serial number  e.g. EA835125")
        self._sn_edit.setStyleSheet(
            f"background:{PANEL};border:1px solid {BDR};border-radius:5px;"
            f"color:{WHITE};font-size:12px;font-family:monospace;padding:5px 8px;"
        )
        self._sn_edit.textChanged.connect(self._on_manual_text)

        self._mac_edit = QLineEdit()
        self._mac_edit.setPlaceholderText("MAC / UUID (optional)")
        self._mac_edit.setStyleSheet(
            f"background:{PANEL};border:1px solid {BDR};border-radius:5px;"
            f"color:{TEXT};font-size:12px;font-family:monospace;padding:5px 8px;"
        )

        manual_row.addWidget(self._sn_edit, stretch=2)
        manual_row.addWidget(self._mac_edit, stretch=2)
        layout.addLayout(manual_row)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._btn_scan    = ABtn("Scan", BLUE)
        self._btn_connect = ABtn("Connect", GREEN)
        self._btn_cancel  = GBtn("Cancel")
        self._btn_connect.setEnabled(False)
        self._btn_scan.clicked.connect(self._do_scan)
        self._btn_connect.clicked.connect(self._do_connect)
        self._btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self._btn_scan)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_cancel)
        btn_row.addWidget(self._btn_connect)
        layout.addLayout(btn_row)

        # Wire scan results signal
        self._handler.devices_found.connect(self._on_devices_found)

        # Scan timer to re-enable button
        self._scan_timer = QTimer(self)
        self._scan_timer.setSingleShot(True)
        self._scan_timer.timeout.connect(lambda: self._btn_scan.setEnabled(True))

    def _do_scan(self):
        self._list.clear()
        self._devices.clear()
        self._btn_scan.setEnabled(False)
        self._btn_connect.setEnabled(False)
        self._status_lbl.setText("Scanning… (8 seconds)")
        self._status_lbl.setStyleSheet(f"font-size:11px;color:{AMBER};")
        self._handler.scan(duration=8.0)
        self._scan_timer.start(9000)

    def _on_devices_found(self, devices: list):
        self._devices = devices
        self._list.clear()
        for dev in devices:
            item = QListWidgetItem(dev.display_name)
            item.setData(Qt.ItemDataRole.UserRole, dev)
            self._list.addItem(item)
        count = len(devices)
        self._status_lbl.setText(
            f"{count} device(s) found." if count else
            "No devices found — wear the strap and try again, or enter manually below."
        )
        color = GREEN if count else RED
        self._status_lbl.setStyleSheet(f"font-size:11px;color:{color};")

    def _on_selection(self):
        self._btn_connect.setEnabled(
            len(self._list.selectedItems()) > 0 or len(self._sn_edit.text().strip()) >= 4
        )

    def _on_manual_text(self, text):
        self._btn_connect.setEnabled(
            len(text.strip()) >= 4 or len(self._list.selectedItems()) > 0
        )

    def _do_connect(self):
        # Prefer list selection
        items = self._list.selectedItems()
        if items:
            self.selected_device = items[0].data(Qt.ItemDataRole.UserRole)
            self.accept()
            return
        # Fall back to manual entry
        sn  = self._sn_edit.text().strip().upper()
        mac = self._mac_edit.text().strip()
        if sn:
            self.selected_device = PolarDevice(
                name=f"Polar H10 {sn}",
                address=mac,
                serial_number=sn,
            )
            self.accept()


# ── Main window ─────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lab Stream Layer")
        self.setMinimumSize(1100, 700)

        self._session_ts   = None
        self._is_recording = False
        self._output_dir   = Path.home() / "SyncBridge_Recordings"
        self._elapsed      = 0

        self._sync_logger = SyncLogger(self._output_dir)
        self._emotibit    = EmotiBitHandler(self)
        self._polar       = PolarHandler(self._output_dir, self)
        self._unity       = UnityHandler(parent=self)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

        # Auto-ping: 3 pings every 5s starting at t=10s after recording
        self._auto_ping_timer = QTimer(self)
        self._auto_ping_timer.setInterval(5000)   # 5s between pings
        self._auto_ping_timer.timeout.connect(self._auto_ping_tick)
        self._auto_ping_count = 0

        self._build_ui()
        self._wire()

        self._emotibit.start()
        self._polar.start()
        self._unity.start()
        self._log("Sync Bridge started - waiting for devices...")
        self._load_settings()

    # ── UI ──────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.setStyleSheet(f"QMainWindow,QWidget{{background:{BG};color:{TEXT};}}")
        c = QWidget()
        self.setCentralWidget(c)
        root = QVBoxLayout(c)
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)

        # Header
        hdr = QWidget()
        hdr.setFixedHeight(54)
        hdr.setStyleSheet(f"background:{PANEL};border-bottom:1px solid {BDR};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(20, 0, 20, 0)
        title = QLabel("LAB STREAM LAYER")
        title.setStyleSheet(
            f"font-size:15px;font-weight:800;color:{WHITE};letter-spacing:3px;"
        )
        self._sess_lbl = QLabel("No active session")
        self._sess_lbl.setStyleSheet(f"font-size:11px;color:{DIM};")
        self._rec_lbl = QLabel("")
        self._rec_lbl.setStyleSheet(
            f"font-size:11px;color:{AMBER};font-weight:700;"
        )
        hl.addWidget(title)
        hl.addStretch()
        hl.addWidget(self._sess_lbl)
        hl.addSpacing(14)
        hl.addWidget(self._rec_lbl)
        root.addWidget(hdr)

        # Body: two-column layout
        body = QWidget()
        body_row = QHBoxLayout(body)
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(0)

        # ── LEFT: controls ────────────────────────────────────────────────────
        left = QWidget()
        bl = QVBoxLayout(left)
        bl.setContentsMargins(20, 16, 12, 16)
        bl.setSpacing(12)

        # ── Device status rows ─────────────────────────────────────────────────
        bl.addWidget(SLabel("Devices"))
        bl.addSpacing(2)

        self._row_eb = DeviceRow(
            "EmotiBit", self._open_eb_picker, self._eb_disconnect
        )
        self._row_polar = DeviceRow(
            "Polar H10", self._open_polar_picker, self._polar_disconnect
        )
        self._row_unity = DeviceRow(
            "Unity", lambda: None, lambda: None
        )
        self._row_unity.required_checkbox.setChecked(False)
        self._row_unity._btn_scan.setEnabled(False)
        self._row_unity._btn_scan.setText("Auto")

        for row in (self._row_eb, self._row_polar, self._row_unity):
            bl.addWidget(row)
            row.required_checkbox.stateChanged.connect(self._update_start_btn)

        bl.addSpacing(2)
        bl.addWidget(Divider())
        bl.addSpacing(2)

        # ── Recording ──────────────────────────────────────────────────────────
        bl.addWidget(SLabel("Recording"))
        bl.addSpacing(2)
        rr = QHBoxLayout()
        rr.setSpacing(10)
        self._btn_start = ABtn("Start Recording", GREEN)
        self._btn_start.setEnabled(False)
        self._btn_stop  = ABtn("Stop Recording",  RED)
        self._btn_stop.setEnabled(False)
        rr.addWidget(self._btn_start)
        rr.addWidget(self._btn_stop)
        bl.addLayout(rr)

        bl.addSpacing(2)
        bl.addWidget(Divider())
        bl.addSpacing(2)

        # ── Ping ───────────────────────────────────────────────────────────────
        bl.addWidget(SLabel("Sync Marker"))
        bl.addSpacing(2)
        pr = QHBoxLayout()
        pr.setSpacing(10)
        self._btn_ping = ABtn("Send Ping  -  Sync All Devices", BLUE)
        self._btn_ping.setMinimumHeight(50)
        self._ping_lbl = QLabel("0 pings")
        self._ping_lbl.setFixedWidth(76)
        self._ping_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._ping_lbl.setStyleSheet(
            f"background:{PANEL};border:1px solid {BDR};border-radius:6px;"
            f"font-size:13px;font-weight:700;color:{BLUE};"
        )
        pr.addWidget(self._btn_ping, stretch=1)
        pr.addWidget(self._ping_lbl)
        bl.addLayout(pr)

        bl.addSpacing(2)
        bl.addWidget(Divider())
        bl.addSpacing(2)

        # ── Settings ───────────────────────────────────────────────────────────
        bl.addWidget(SLabel("Settings"))
        bl.addSpacing(2)
        sr = QHBoxLayout()
        sr.setSpacing(8)
        dl = QLabel("Output folder:")
        dl.setStyleSheet(f"color:{DIM};font-size:11px;")
        self._dir_edit = QLineEdit(str(self._output_dir))
        self._dir_edit.setReadOnly(True)
        self._dir_edit.setStyleSheet(
            f"background:{PANEL};border:1px solid {BDR};border-radius:5px;"
            f"color:{TEXT};font-size:11px;padding:4px 8px;"
        )
        bb = GBtn("Browse")
        bb.setFixedWidth(70)
        bb.clicked.connect(self._browse)
        pl = QLabel("UDP Port:")
        pl.setStyleSheet(f"color:{DIM};font-size:11px;")
        self._port = QSpinBox()
        self._port.setRange(1024, 65535)
        self._port.setValue(12345)
        self._port.setFixedWidth(80)
        self._port.setStyleSheet(
            f"background:{PANEL};border:1px solid {BDR};border-radius:5px;"
            f"color:{TEXT};font-size:11px;padding:3px 6px;"
        )
        sr.addWidget(dl)
        sr.addWidget(self._dir_edit, stretch=1)
        sr.addWidget(bb)
        sr.addSpacing(8)
        sr.addWidget(pl)
        sr.addWidget(self._port)
        bl.addLayout(sr)

        bl.addSpacing(2)
        bl.addWidget(Divider())
        bl.addSpacing(2)

        # ── Log ────────────────────────────────────────────────────────────────
        bl.addWidget(SLabel("Event Log"))
        bl.addSpacing(2)
        self._log_w = QPlainTextEdit()
        self._log_w.setReadOnly(True)
        self._log_w.setMaximumBlockCount(600)
        f = QFont("Courier New")
        f.setStyleHint(QFont.StyleHint.TypeWriter)
        f.setPointSize(10)
        self._log_w.setFont(f)
        self._log_w.setStyleSheet(
            f"background:{PANEL};color:{TEXT};border:1px solid {BDR};"
            "border-radius:6px;padding:8px;"
        )
        bl.addWidget(self._log_w, stretch=1)

        # ── RIGHT: live monitor (in splitter) ───────────────────────────────
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(12, 16, 20, 16)
        rl.setSpacing(8)

        rl.addWidget(SLabel("Live Data Monitor"))
        rl.addSpacing(2)

        for label, color, unit in [
            ("ECG — Polar H10",     BLUE,      "µV"),
            ("HR — Polar H10",      GREEN,     "bpm"),
            ("RR — Polar H10",      AMBER,     "ms"),
            ("HR — EmotiBit",       GREEN,     "bpm"),
            ("PPG Red — EmotiBit",  "#e05c5c", ""),
        ]:
            row_lbl = QLabel(label.upper())
            row_lbl.setStyleSheet(
                f"font-size:8px;font-weight:700;color:{DIM};letter-spacing:1px;"
            )
            rl.addWidget(row_lbl)
            g = StreamGraph(label, color, unit)
            rl.addWidget(g)
            # Store references
            attr = {
                "ECG — Polar H10":    "_g_ecg",
                "HR — Polar H10":     "_g_hr",
                "RR — Polar H10":     "_g_rr",
                "HR — EmotiBit":      "_g_eb_hr",
                "PPG Red — EmotiBit": "_g_eb_ppg",
            }[label]
            setattr(self, attr, g)

        rl.addStretch()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStyleSheet(f"""
            QSplitter::handle {{
                background: {BDR};
                width: 3px;
            }}
            QSplitter::handle:hover {{
                background: {DIM};
            }}
            QSplitter::handle:pressed {{
                background: {BLUE};
            }}
        """)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([620, 480])
        splitter.setChildrenCollapsible(False)
        body_row.addWidget(splitter)

        root.addWidget(body)

    def _wire(self):
        self._btn_start.clicked.connect(self._start_rec)
        self._btn_stop.clicked.connect(self._stop_rec)
        self._btn_ping.clicked.connect(self._ping)

        self._emotibit.status_changed.connect(self._on_e)
        self._emotibit.calibration_changed.connect(self._on_eb_calib)
        self._emotibit.battery_changed.connect(self._row_eb.set_battery)
        self._emotibit.log_message.connect(self._log)
        self._emotibit.ppg_red_sample.connect(self._g_eb_ppg.push)
        self._emotibit.hr_sample.connect(self._g_eb_hr.push)
        self._polar.status_changed.connect(self._on_p)
        self._polar.calibration_changed.connect(self._on_polar_calib)
        self._polar.battery_changed.connect(self._row_polar.set_battery)
        self._polar.log_message.connect(self._log)
        self._polar.ecg_sample.connect(self._g_ecg.push)
        self._polar.hr_sample.connect(self._g_hr.push)
        self._polar.rr_sample.connect(self._g_rr.push)
        self._unity.ping_requested.connect(self._ping)
        self._unity.status_changed.connect(self._on_u)
        self._unity.calibration_changed.connect(self._on_unity_calib)
        self._unity.log_message.connect(self._log)

    # ── EmotiBit picker ────────────────────────────────────────────────────────

    @pyqtSlot()
    def _open_eb_picker(self):
        # Save current status before opening dialog so closing without
        # connecting never resets it
        dlg = EmotiBitPickerDialog(self._emotibit, self)
        result = dlg.exec()
        if result == QDialog.DialogCode.Accepted and dlg.selected_device:
            self._emotibit.connect(dlg.selected_device)
        # If Rejected/closed: do nothing — status unchanged

    @pyqtSlot()
    def _eb_disconnect(self):
        self._emotibit.disconnect()
        self._update_start_btn()

    @pyqtSlot()
    def _open_polar_picker(self):
        dlg = PolarPickerDialog(self._polar, self)
        result = dlg.exec()
        if result == QDialog.DialogCode.Accepted and dlg.selected_device:
            self._polar.connect_device(dlg.selected_device)
        # If Rejected/closed: do nothing — status unchanged

    @pyqtSlot()
    def _polar_disconnect(self):
        self._polar.stop()
        self._update_start_btn()

    # ── Recording ──────────────────────────────────────────────────────────────

    @pyqtSlot()
    def _start_rec(self):
        # ── SD card safeguard ──────────────────────────────────────────────────
        if self._row_eb.is_required and self._emotibit.status in (
            EmotiBitStatus.CONNECTED, EmotiBitStatus.RECORDING
        ):
            self._log("[EmotiBit] Checking SD card...")
            self._btn_start.setEnabled(False)
            import threading
            sd_result = [None]
            done = __import__("threading").Event()

            def _check():
                sd_result[0] = self._emotibit.check_sd_card(timeout=3.0)
                done.set()

            threading.Thread(target=_check, daemon=True).start()

            # Process Qt events while waiting (keeps UI responsive)
            from PyQt6.QtCore import QEventLoop
            loop = QEventLoop()
            import threading as _t
            _t.Thread(
                target=lambda: (done.wait(), loop.quit()), daemon=True
            ).start()
            loop.exec()

            if not sd_result[0]:
                resp = QMessageBox.warning(
                    self,
                    "SD Card Not Detected",
                    "EmotiBit did not confirm SD card is ready.\n\n"
                    "The card may be missing, unseated, or the device is still initialising.\n\n"
                    "Proceed without EmotiBit SD recording?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if resp != QMessageBox.StandardButton.Yes:
                    self._update_start_btn()
                    return
                self._log("[EmotiBit] ⚠ Proceeding without SD card confirmation")
            else:
                self._log("[EmotiBit] ✓ SD card confirmed")

        self._session_ts = SyncLogger.make_session_timestamp()
        # Pass checkbox state to logger so only required devices get rows
        self._sync_logger.log_emotibit = self._row_eb.is_required
        self._sync_logger.log_polar    = self._row_polar.is_required
        self._sync_logger.log_unity    = self._row_unity.is_required
        path = self._sync_logger.start_session(self._session_ts)
        # start_recording triggers device calibration with 5s internal delay
        self._polar.start_recording(self._session_ts)
        self._emotibit.start_recording()
        self._unity.calibrate(n=5, delay=5.0)
        self._is_recording = True
        self._elapsed = 0
        self._auto_ping_count = 0
        self._timer.start()
        # First auto-ping at t=10s, then every 5s for 3 total
        QTimer.singleShot(10000, self._start_auto_ping_sequence)
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._sess_lbl.setText(f"Session: {self._session_ts}")
        self._sess_lbl.setStyleSheet(f"font-size:11px;color:{GREEN};")
        self._log(f"Session started — auto-ping: 3× starting at t=10s")

    @pyqtSlot()
    def _stop_rec(self):
        self._polar.stop_recording()
        self._emotibit.stop_recording()
        self._sync_logger.close()
        self._is_recording = False
        self._timer.stop()
        self._auto_ping_timer.stop()
        self._auto_ping_count = 0
        self._rec_lbl.setText("")
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._sess_lbl.setStyleSheet(f"font-size:11px;color:{DIM};")
        self._log(
            f"Session stopped - {self._sync_logger.ping_count} pings recorded"
        )

    def _start_auto_ping_sequence(self):
        """Called at t=10s after recording starts. Fires first ping then starts interval timer."""
        if not self._is_recording:
            return
        self._auto_ping_tick()
        if self._auto_ping_count < 3:
            self._auto_ping_timer.start()

    def _auto_ping_tick(self):
        """Called every 5s by the interval timer. Sends up to 3 auto-pings."""
        if not self._is_recording or self._auto_ping_count >= 3:
            self._auto_ping_timer.stop()
            return
        self._auto_ping_count += 1
        self._log(f"Auto-ping {self._auto_ping_count}/3")
        self._ping()
        if self._auto_ping_count >= 3:
            self._auto_ping_timer.stop()
            self._log("Auto-ping sequence complete")

    @pyqtSlot()
    def _ping(self):
        if not self._sync_logger._writer:
            self._session_ts = SyncLogger.make_session_timestamp()
            p = self._sync_logger.start_session(self._session_ts)
            self._log(f"Auto-started outlog - {p.name}")

        next_id = f"ping_{self._sync_logger.ping_count + 1:03d}"

        # Send markers — each returns calibrated one-way latency (constant per session)
        emotibit_latency = self._emotibit.send_marker(next_id)
        polar_latency    = self._polar.send_marker(next_id)
        unity_latency    = self._unity.broadcast_ping(next_id)

        # Log with all latencies — ping_id generated here matches next_id
        pid, ns = self._sync_logger.log_ping(
            emotibit_latency_ns=emotibit_latency,
            polar_latency_ns=polar_latency,
            unity_latency_ns=unity_latency,
        )

        n = self._sync_logger.ping_count
        self._ping_lbl.setText(f"{n} ping{'s' if n != 1 else ''}")
        self._log(
            f"PING  {pid}  "
            f"emotibit={emotibit_latency/1e6:.1f}ms  "
            f"polar={polar_latency/1e6:.1f}ms  "
            f"unity={unity_latency/1e6:.1f}ms"
            if all(x > 0 for x in [emotibit_latency, polar_latency, unity_latency])
            else f"PING  {pid}  sent_utc_ns={ns}"
        )

    # ── Status slots ───────────────────────────────────────────────────────────

    @pyqtSlot(EmotiBitStatus)
    def _on_e(self, s):
        if s == EmotiBitStatus.SCANNING:
            self._row_eb.set_status("Scanning...", AMBER)
        elif s == EmotiBitStatus.IDLE:
            self._row_eb.set_status("Not Connected", RED)
        elif s == EmotiBitStatus.CONNECTED:
            self._row_eb.set_status("Connected — calibrating...", AMBER)
        elif s == EmotiBitStatus.RECORDING:
            self._row_eb.set_status("Recording", AMBER)
        self._update_start_btn()

    @pyqtSlot(bool)
    def _on_eb_calib(self, ok: bool):
        if ok:
            self._row_eb.set_status("Connected — ready", GREEN)
        else:
            if self._emotibit.status == EmotiBitStatus.CONNECTED:
                self._row_eb.set_status("Connected — calibrating...", AMBER)
        self._update_start_btn()

    @pyqtSlot(PolarStatus)
    def _on_p(self, s):
        if s == PolarStatus.IDLE:
            self._row_polar.set_status("Not Connected", RED)
        elif s == PolarStatus.SCANNING:
            self._row_polar.set_status("Scanning...", AMBER)
        elif s == PolarStatus.CONNECTED:
            self._row_polar.set_status("Connected — calibrating...", AMBER)
        elif s == PolarStatus.RECORDING:
            self._row_polar.set_status("Recording", AMBER)
        self._update_start_btn()

    @pyqtSlot(bool)
    def _on_polar_calib(self, ok: bool):
        if ok:
            self._row_polar.set_status("Connected — ready", GREEN)
        else:
            if self._polar.status == PolarStatus.CONNECTED:
                self._row_polar.set_status("Connected — calibrating...", AMBER)
        self._update_start_btn()

    @pyqtSlot(str)
    def _on_u(self, s):
        if s == "connected":
            self._row_unity.set_status("Connected — calibrating...", AMBER)
        else:
            self._row_unity.set_status("Waiting", DIM)
        self._update_start_btn()

    @pyqtSlot(bool)
    def _on_unity_calib(self, ok: bool):
        if ok:
            self._row_unity.set_status("Connected — ready", GREEN)
        else:
            if self._unity._clients:
                self._row_unity.set_status("Connected — calibrating...", AMBER)
        self._update_start_btn()

    def _update_start_btn(self):
        """Enable Start only when all required devices are connected AND calibrated."""
        if self._is_recording:
            return

        def _connected(k):
            if k == "emotibit":
                return self._emotibit.status in (
                    EmotiBitStatus.CONNECTED, EmotiBitStatus.RECORDING
                )
            if k == "polar":
                return self._polar.status in (
                    PolarStatus.CONNECTED, PolarStatus.RECORDING
                )
            return bool(self._unity._clients)  # unity

        def _calibrated(k):
            if k == "emotibit":
                return self._emotibit.calibrated_latency_ns > 0
            if k == "polar":
                return self._polar.calibrated_latency_ns > 0
            return self._unity.calibrated_latency_ns > 0  # unity

        rows = {
            "emotibit": self._row_eb,
            "polar":    self._row_polar,
            "unity":    self._row_unity,
        }

        not_connected  = [k for k, r in rows.items() if r.is_required and not _connected(k)]
        not_calibrated = [k for k, r in rows.items() if r.is_required and _connected(k) and not _calibrated(k)]

        all_ready = not not_connected and not not_calibrated
        self._btn_start.setEnabled(all_ready)

        if not_connected:
            self._btn_start.setToolTip(f"Not connected: {', '.join(not_connected)}")
        elif not_calibrated:
            self._btn_start.setToolTip(f"Calibrating: {', '.join(not_calibrated)}")
        else:
            self._btn_start.setToolTip("")

    @pyqtSlot(str)
    def _log(self, msg):
        # Suppress EmotiBit high-frequency EM status updates from the log
        if "[EmotiBit] Status:" in msg:
            return
        ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        self._log_w.appendPlainText(f"{ts}  {msg}")
        sb = self._log_w.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _tick(self):
        self._elapsed += 1
        h, r = divmod(self._elapsed, 3600)
        m, s = divmod(r, 60)
        self._rec_lbl.setText(f"REC  {h:02d}:{m:02d}:{s:02d}")

    def _browse(self):
        p = QFileDialog.getExistingDirectory(
            self, "Select output folder", str(self._output_dir)
        )
        if p:
            self._output_dir = Path(p)
            self._dir_edit.setText(p)
            self._sync_logger = SyncLogger(self._output_dir)
            self._polar._output_dir = self._output_dir

    def _load_settings(self):
        s = QSettings("XRLabs", "LabStreamLayer")
        # Output folder
        folder = s.value("output_dir", str(self._output_dir))
        self._output_dir = Path(folder)
        self._dir_edit.setText(folder)
        self._sync_logger = SyncLogger(self._output_dir)
        self._polar._output_dir = self._output_dir
        # UDP port
        port = int(s.value("udp_port", 12345))
        self._port.setValue(port)
        # Device checkboxes
        self._row_eb.required_checkbox.setChecked(
            s.value("required_emotibit", True, type=bool)
        )
        self._row_polar.required_checkbox.setChecked(
            s.value("required_polar", True, type=bool)
        )
        self._row_unity.required_checkbox.setChecked(
            s.value("required_unity", False, type=bool)
        )
        self._update_start_btn()

    def _save_settings(self):
        s = QSettings("XRLabs", "LabStreamLayer")
        s.setValue("output_dir",        str(self._output_dir))
        s.setValue("udp_port",          self._port.value())
        s.setValue("required_emotibit", self._row_eb.required_checkbox.isChecked())
        s.setValue("required_polar",    self._row_polar.required_checkbox.isChecked())
        s.setValue("required_unity",    self._row_unity.required_checkbox.isChecked())

    def closeEvent(self, event):
        self._save_settings()
        if self._is_recording:
            self._stop_rec()
        self._emotibit.stop()
        self._polar.stop()
        self._unity.stop()
        event.accept()
