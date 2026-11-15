"""The Qt window, the tray icon, and the 1 Hz control loop.

All audio state lives in systemd units and snapserver; this window is a
remote control for them plus the place where latency converges.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import calibration, config, lipsync, preload, safety
from . import codec as codec_mod
from . import library as library_mod
from .acoustic_sync import AcousticLatencyApplier
from .backend import Backend, BackendError, server_ready
from .discovery import Discovery
from .health import ClientTracker, RemoteUnderrunWatcher, UnderrunWatcher
from .latency import ControllerConfig, LatencyController, PreRollGate

IDLE, HOSTING, JOINING, STREAMING, LOST = "idle", "hosting", "joining", "streaming", "lost"


class PreloadThread(QThread):
    """Copy/verify/preroll without blocking the Qt event loop."""

    progress = Signal(str)
    failed = Signal(str)
    done = Signal(int)

    def __init__(self, job: preload.Job, parent=None) -> None:
        super().__init__(parent)
        self.job = job

    def run(self) -> None:
        try:
            count = self.job.run(self.progress.emit)
        except preload.PreloadCancelled:
            self.done.emit(0)
            return
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            self.failed.emit(str(exc))
            return
        self.done.emit(count)


class PreloadStopThread(QThread):
    """Close remote players without freezing the window."""

    failed = Signal(str)
    done = Signal(int)

    def __init__(self, targets: list[preload.Target], parent=None) -> None:
        super().__init__(parent)
        self.targets = targets

    def run(self) -> None:
        try:
            killed = preload.stop(self.targets)
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            self.failed.emit(str(exc))
            return
        self.done.emit(killed)


class LibrarySyncThread(QThread):
    """Copy one media folder to every device without freezing the window."""

    progress = Signal(str)
    failed = Signal(str)
    skipped = Signal(str)
    done = Signal(int)

    def __init__(self, job: library_mod.FolderSyncJob, parent=None) -> None:
        super().__init__(parent)
        self.job = job

    def run(self) -> None:
        try:
            count = self.job.run(self.progress.emit)
        except library_mod.LibrarySyncCancelled:
            self.done.emit(0)
            return
        except library_mod.LibraryUnavailable as exc:
            # An offline mount is not a failure; the root is skipped.
            self.skipped.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            self.failed.emit(str(exc))
            return
        self.done.emit(count)


class CalibrationThread(QThread):
    """Run one acoustic round without blocking the Qt event loop."""

    progress = Signal(str)
    failed = Signal(str)
    done = Signal(object)

    def __init__(self, job: calibration.AcousticCalibration, parent=None) -> None:
        super().__init__(parent)
        self.job = job

    def run(self) -> None:
        try:
            result = self.job.run(self.progress.emit)
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


class SourceDiscoveryThread(QThread):
    """List physical microphone sources without blocking the window."""

    done = Signal(object)

    def __init__(self, targets: list[preload.Target], parent=None) -> None:
        super().__init__(parent)
        self.targets = targets

    def run(self) -> None:
        discovered: dict[str, list[str]] = {}
        for target in self.targets:
            discovered[target.name] = calibration.list_physical_sources(target)
        self.done.emit(discovered)


class RemoteHealthThread(QThread):
    """Poll remote client journals off the GUI thread."""

    counts = Signal(object)

    def __init__(self, watchers: dict[str, RemoteUnderrunWatcher], parent=None) -> None:
        super().__init__(parent)
        self.watchers = watchers

    def run(self) -> None:
        self.counts.emit({name: watcher.poll() for name, watcher in self.watchers.items()})


class MicrophoneDialog(QDialog):
    """Choose one physical input source per device."""

    def __init__(
        self,
        targets: list[preload.Target],
        discovered: dict[str, list[str]],
        selected: dict[str, str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Microphones")
        self.resize(620, 280)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Choose a physical microphone near each speaker. Monitor and virtual "
            "sources are intentionally excluded; they cannot measure acoustic delay "
            "and can feed captured output back into the capture path."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.combos: dict[str, QComboBox] = {}
        for target in targets:
            combo = QComboBox()
            names = discovered.get(target.name, [])
            if not names:
                combo.addItem("No physical source found", "")
            else:
                for name in names:
                    combo.addItem(name, name)
                preferred = selected.get(target.name)
                index = combo.findData(preferred) if preferred else -1
                if index >= 0:
                    combo.setCurrentIndex(index)
            combo.setToolTip("Physical input, not a monitor or virtual loopback")
            self.combos[target.name] = combo
            form.addRow(calibration.target_label(target), combo)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict[str, str]:
        return {
            name: str(combo.currentData() or "")
            for name, combo in self.combos.items()
            if combo.currentData()
        }


class MainWindow(QMainWindow):
    def __init__(self, cfg: config.Config, silent: bool = False) -> None:
        super().__init__()
        self.cfg = cfg
        #: Headless runs must not block on dialogs; _alert/_confirm notice this.
        self.silent = silent
        self.backend = Backend(cfg)
        self.discovery = Discovery()
        self.controller = LatencyController(
            buffer_ms=cfg.effective_buffer_ms(),
            learned_floor_ms=cfg.learned_floor(cfg.preferred_host),
        )
        self._latency_applier = AcousticLatencyApplier(cfg, self.backend, self.controller)
        self.gate = PreRollGate()
        self.state = IDLE
        self.host_ip = "127.0.0.1"
        self._client_widgets: dict[str, dict] = {}
        self._output_note = ""
        self._gate_ids: dict[str, str] = {}
        self._gate_muted: set[str] = set()
        self._preload_thread: PreloadThread | None = None
        self._preload_stop_thread: PreloadStopThread | None = None
        self._preload_cancelled = False
        self._library_thread: LibrarySyncThread | None = None
        self._library_cancelled = False
        self._calibration_thread: CalibrationThread | None = None
        self._calibration_cancelled = False
        self._calibration_manual = False
        self._mic_thread: SourceDiscoveryThread | None = None
        self._remote_health_thread: RemoteHealthThread | None = None
        self._remote_watchers: dict[str, RemoteUnderrunWatcher] = {}
        self._last_rows: list[dict] = []
        self._acoustic_next = 0.0
        self._acoustic_note = ""
        self._mic_active = False
        self._acoustic_result: dict | None = None
        self._library_result = ""
        self._library_config_mtime = 0
        self._playing_job: preload.Job | None = None
        self._notice = ""
        self._notice_until = 0.0
        self._preroll_active = False
        self._saved_volumes: dict[str, int] = {}
        self._armed: set[str] = set()
        self._pending: dict[str, float] = {}
        self._known_peers: set[str] = set()
        self._unit_states = ""
        self._ticks = 0
        self._last_retry = 0.0
        self._ramp: dict[str, tuple[float, int]] = {}
        self._last_health = 0.0
        self._ignore_drops_until = 0.0
        self._last_diagnosis = ""
        self._last_clock: float | None = None
        self.underruns = UnderrunWatcher()
        self.tracker = ClientTracker()

        if cfg.preset == "deep":
            self.controller.set_preset("deep", self._now())

        self._build_ui()
        self._refresh_acoustic_label()
        try:
            self._refresh_output()
        except BackendError:
            pass
        self.discovery.start(role="idle", buf_ms=self.controller.buffer_ms, preset=cfg.preset)
        for host in cfg.extra_hosts:
            self.discovery.add_manual(host)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._refresh)
        self._refresh()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.setWindowTitle("Audiosync")
        self.resize(640, 600)
        self.setMinimumSize(560, 480)

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(10)

        self.header = QLabel()
        self.header.setTextFormat(Qt.TextFormat.RichText)
        self.header.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        outer.addWidget(self.header)

        host_box = QGroupBox("Audio host")
        host_layout = QHBoxLayout(host_box)
        host_layout.setContentsMargins(12, 8, 12, 8)
        host_layout.setSpacing(8)
        self.host_combo = QComboBox()
        self.host_combo.setAccessibleName("Audio host")
        self.host_combo.setToolTip("This device or a discovered host")
        self.host_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.host_combo.setMinimumContentsLength(18)
        self.host_combo.currentIndexChanged.connect(self._on_host_changed)
        host_layout.addWidget(self.host_combo, 1)
        add_button = QPushButton("Add by IP")
        add_button.setToolTip("For paths mDNS does not cross (another subnet or VPN)")
        add_button.setAccessibleName("Add device by IP")
        add_button.clicked.connect(self._on_add_manual)
        host_layout.addWidget(add_button)
        host_layout.addSpacing(4)
        self.start_button = QPushButton("&Start")
        self.start_button.setToolTip("Host here, or join the selected device")
        self.start_button.setAccessibleName("Start hosting or joining")
        self.start_button.setDefault(True)
        self.start_button.setMinimumWidth(80)
        self.start_button.clicked.connect(self._on_start)
        self.stop_button = QPushButton("S&top")
        self.stop_button.setAccessibleName("Stop hosting or joining")
        self.stop_button.setMinimumWidth(80)
        self.stop_button.clicked.connect(self._on_stop)
        host_layout.addWidget(self.start_button)
        host_layout.addWidget(self.stop_button)
        outer.addWidget(host_box)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(False)
        self.tabs.setAccessibleName("Audiosync controls")
        outer.addWidget(self.tabs, 1)

        playback_tab = QWidget()
        playback_layout = QVBoxLayout(playback_tab)
        playback_layout.setContentsMargins(10, 8, 10, 8)
        playback_layout.setSpacing(8)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Speaker", "Plays", "Volume"])
        self.table.setAccessibleName("Speakers")
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setMinimumHeight(80)
        table_header = self.table.horizontalHeader()
        table_header.setStretchLastSection(False)
        table_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(2, 180)
        self.table.verticalHeader().setDefaultSectionSize(34)
        playback_layout.addWidget(self.table, 1)

        play_box = QGroupBox("This device")
        play_layout = QHBoxLayout(play_box)
        play_layout.setContentsMargins(10, 6, 10, 6)
        play_layout.setSpacing(14)
        self.play_radios = {}
        for value, label, hint in (
            ("synced", "&Synced", "Play in sync with the group"),
            ("direct", "&Direct", "Play locally without Snapcast sync"),
            ("off", "&Off", "Stay silent on this device"),
        ):
            radio = QRadioButton(label)
            radio.setToolTip(hint)
            radio.setAccessibleName(label.replace("&", ""))
            radio.setChecked(self.cfg.playback == value)
            radio.toggled.connect(self._on_playback_changed)
            self.play_radios[value] = radio
            play_layout.addWidget(radio)
        play_layout.addStretch(1)
        playback_layout.addWidget(play_box)

        buffer_box = QGroupBox("Buffer")
        lat_layout = QHBoxLayout(buffer_box)
        lat_layout.setContentsMargins(10, 6, 10, 6)
        lat_layout.setSpacing(14)
        self.preset_radios = {}
        for value, label, hint in (
            ("auto", "&Auto", "Adaptive buffer; settles at the learned link floor"),
            ("deep", "&Deep (60 s)", "Pin a 60 s buffer for difficult links"),
            ("manual", "&Manual", "Use the millisecond value on the right"),
        ):
            radio = QRadioButton(label)
            radio.setToolTip(hint)
            radio.setAccessibleName(label.replace("&", ""))
            radio.setChecked(self.cfg.preset == value)
            radio.toggled.connect(self._on_preset_changed)
            self.preset_radios[value] = radio
            lat_layout.addWidget(radio)
        self.manual_spin = QSpinBox()
        self.manual_spin.setRange(40, 5000)
        self.manual_spin.setSuffix(" ms")
        self.manual_spin.setValue(self.cfg.manual_buffer_ms)
        self.manual_spin.setToolTip("Manual buffer in milliseconds; enabled only for Manual")
        self.manual_spin.setAccessibleName("Manual buffer in milliseconds")
        self.manual_spin.setEnabled(self.cfg.preset == "manual")
        self.manual_spin.valueChanged.connect(self._on_manual_changed)
        lat_layout.addWidget(self.manual_spin)
        lat_layout.addStretch(1)
        playback_layout.addWidget(buffer_box)

        self.latency_label = QLabel("Buffer: \u2014")
        self.latency_label.setWordWrap(True)
        self.latency_label.setToolTip("Reported Snapcast buffer; not an acoustic measurement")
        playback_layout.addWidget(self.latency_label)

        action_layout = QHBoxLayout()
        action_layout.setSpacing(8)
        self.preload_button = QPushButton("Preload && Play")
        self.preload_button.setToolTip(
            "Copy the selected media to every device, verify the complete file, "
            "then start all devices together after 10 s"
        )
        self.preload_button.setAccessibleName("Preload and play")
        self.preload_button.clicked.connect(self._on_preload)
        action_layout.addWidget(self.preload_button)
        self.stop_playback_button = QPushButton("Stop playback")
        self.stop_playback_button.setToolTip(
            "Close Audiosync preload players, legacy RTP receivers, and Audiosync units "
            "on every device. Personal VLC/browser audio is left alone."
        )
        self.stop_playback_button.setAccessibleName("Stop playback")
        self.stop_playback_button.clicked.connect(self._on_stop_playback)
        action_layout.addWidget(self.stop_playback_button)
        action_layout.addStretch(1)
        playback_layout.addLayout(action_layout)
        self.tabs.addTab(playback_tab, "Playback")

        sync_tab = QWidget()
        sync_layout = QVBoxLayout(sync_tab)
        sync_layout.setContentsMargins(10, 8, 10, 8)
        sync_layout.setSpacing(10)

        sync_box = QGroupBox("Video sync")
        sync_row = QHBoxLayout(sync_box)
        sync_row.setContentsMargins(10, 6, 10, 6)
        sync_row.setSpacing(8)
        self.sync_label = QLabel("Audio offset \u2014")
        self.sync_label.setWordWrap(True)
        self.sync_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.sync_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        sync_row.addWidget(self.sync_label, 1)
        self.copy_button = QPushButton("Copy VLC")
        self.copy_button.setToolTip("Copy the exact VLC audio-desync command")
        self.copy_button.setAccessibleName("Copy VLC command")
        self.copy_button.clicked.connect(self._copy_offset)
        sync_row.addWidget(self.copy_button)
        self.mpv_button = QPushButton("Launch mpv")
        self.mpv_button.setToolTip("Launch mpv with the audio offset already applied")
        self.mpv_button.setAccessibleName("Launch mpv")
        self.mpv_button.clicked.connect(self._launch_mpv)
        sync_row.addWidget(self.mpv_button)
        sync_layout.addWidget(sync_box)

        library_box = QGroupBox("Library")
        library_layout = QHBoxLayout(library_box)
        library_layout.setContentsMargins(10, 6, 10, 6)
        library_layout.setSpacing(8)
        self.library_label = QLabel("No synced folder")
        self.library_label.setWordWrap(True)
        self.library_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.library_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        library_layout.addWidget(self.library_label, 1)
        self.sync_folder_button = QPushButton("Sync folder\u2026")
        self.sync_folder_button.setAccessibleName("Sync media folder")
        self.sync_folder_button.setToolTip(
            "Copy a folder from the SMB share (or any mounted folder) to every device once. "
            "Preload & Play then verifies and reuses that copy instead of transferring the file again."
        )
        self.sync_folder_button.clicked.connect(self._on_sync_folder)
        library_layout.addWidget(self.sync_folder_button)
        sync_layout.addWidget(library_box)

        acoustic_box = QGroupBox("Acoustic sync")
        acoustic_layout = QVBoxLayout(acoustic_box)
        acoustic_layout.setContentsMargins(10, 6, 10, 6)
        acoustic_layout.setSpacing(6)
        acoustic_controls = QHBoxLayout()
        acoustic_controls.setSpacing(8)
        self.calibrate_button = QPushButton("Calibrate now")
        self.calibrate_button.setAccessibleName("Calibrate acoustic sync")
        self.calibrate_button.setToolTip(
            "Play three short probes and measure each physical microphone. "
            "Snapcast per-client latency is adjusted from the result."
        )
        self.calibrate_button.clicked.connect(self._on_calibrate)
        acoustic_controls.addWidget(self.calibrate_button)
        self.monitor_check = QCheckBox("Monitor during playback")
        self.monitor_check.setToolTip(
            "Opt in to audible probes every interval while hosting. "
            "The microphone is active only during each short measurement."
        )
        self.monitor_check.setChecked(bool(self.cfg.acoustic_monitor))
        self.monitor_check.toggled.connect(self._on_monitor_toggled)
        acoustic_controls.addWidget(self.monitor_check)
        self.monitor_interval = QSpinBox()
        self.monitor_interval.setRange(10, 300)
        self.monitor_interval.setSuffix(" s")
        self.monitor_interval.setValue(max(10, min(300, int(self.cfg.acoustic_interval_s))))
        self.monitor_interval.setToolTip("Seconds between acoustic probe rounds")
        self.monitor_interval.setAccessibleName("Acoustic probe interval")
        self.monitor_interval.valueChanged.connect(self._on_monitor_interval)
        acoustic_controls.addWidget(self.monitor_interval)
        self.probe_volume = QSpinBox()
        self.probe_volume.setRange(1, 100)
        self.probe_volume.setSuffix(" %")
        self.probe_volume.setValue(max(1, min(100, int(self.cfg.acoustic_volume))))
        self.probe_volume.setToolTip("Probe level inside the generated WAV")
        self.probe_volume.setAccessibleName("Acoustic probe level")
        self.probe_volume.valueChanged.connect(self._on_probe_volume)
        acoustic_controls.addWidget(self.probe_volume)
        self.mic_button = QPushButton("Microphones\u2026")
        self.mic_button.setToolTip("Choose physical microphone sources per device")
        self.mic_button.setAccessibleName("Choose microphones")
        self.mic_button.clicked.connect(self._on_choose_microphones)
        acoustic_controls.addWidget(self.mic_button)
        acoustic_controls.addStretch(1)
        acoustic_layout.addLayout(acoustic_controls)
        self.acoustic_label = QLabel("No acoustic measurement yet")
        self.acoustic_label.setWordWrap(True)
        self.acoustic_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.acoustic_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        acoustic_layout.addWidget(self.acoustic_label)
        sync_layout.addWidget(acoustic_box)
        sync_layout.addStretch(1)
        self.tabs.addTab(sync_tab, "Sync && Library")
        self._refresh_library_label()

        self.status = QLabel("idle")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(28)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.status.setAccessibleName("Status")
        outer.addWidget(self.status)

        self.setCentralWidget(central)

        self.tray = QSystemTrayIcon(QIcon.fromTheme("audio-speakers"), self)
        tray_menu = QMenu()
        show_action = QAction("Show window", self)
        show_action.triggered.connect(self.showNormal)
        host_action = QAction("Host here", self)
        host_action.triggered.connect(self._start_host)
        stop_action = QAction("Stop", self)
        stop_action.triggered.connect(self._on_stop)
        stop_playback_action = QAction("Stop playback", self)
        stop_playback_action.triggered.connect(self._on_stop_playback)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.quit)
        for action in (show_action, host_action, stop_action, stop_playback_action, quit_action):
            tray_menu.addAction(action)
        self.tray.setContextMenu(tray_menu)
        self.tray.setToolTip("Audiosync")
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    # ------------------------------------------------------------------
    # small UI helpers
    # ------------------------------------------------------------------
    def _alert(self, title: str, text: str) -> None:
        """Dialogs block the caller, so headless runs must bypass them."""
        self._last_diagnosis = text
        if self.silent:
            print(f"[audiosync] {title}: {text}", flush=True)
            return
        QMessageBox.critical(self, title, text)

    def _confirm(self, title: str, text: str) -> bool:
        if self.silent:
            return False
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def _now(self) -> float:
        """Return the system wall clock used by every UI deadline."""
        return time.time()

    def _rebase_clock(self, now: float) -> None:
        """Recover deadlines after an NTP correction or a suspend/resume."""
        previous = self._last_clock
        self._last_clock = now
        if previous is None or (now >= previous and now - previous <= 30.0):
            return
        # Keep the chosen buffer, but restart the settle and gate windows at the
        # new wall time so a jump cannot stall the loop or cause an instant step.
        delta = now - previous
        self.controller.start(now, self.controller.buffer_ms)
        self.gate.begin(now, self.controller.buffer_ms)
        self._pending = {key: ready_at + delta for key, ready_at in self._pending.items()}
        self._ramp = {
            key: (released_at + delta, target)
            for key, (released_at, target) in self._ramp.items()
        }
        self._notice_until = min(self._notice_until, now)
        self._ignore_drops_until = now
        self._last_health = now
        self._last_retry = now

    def _schedule_refresh(self) -> None:
        """Wake at the next wall-clock boundary instead of free-running."""
        interval_ms = 250 if (self._pending or self._ramp) else 1000
        remainder_ms = (self._now() * 1000.0) % interval_ms
        self.timer.start(max(50, int(round(interval_ms - remainder_ms))))

    def _set_notice(self, text: str, seconds: float = 5.0) -> None:
        """Keep a control result visible across the wall-clock status refresh."""
        self._notice = text
        self._notice_until = self._now() + seconds
        self.status.setText(text)

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    def _on_host_changed(self) -> None:
        data = self.host_combo.currentData()
        self.cfg.preferred_host = data
        self.cfg.save()

    def _on_playback_changed(self) -> None:
        selected = next((value for value, radio in self.play_radios.items() if radio.isChecked()), None)
        if selected is None:
            return
        if self.state != IDLE:
            self._restore_playback_radios()
            self._set_notice("Stop hosting or joining before changing playback routing.")
            return
        try:
            selected = safety.validate_playback(selected)
        except safety.RoutingError as exc:
            self._restore_playback_radios()
            self._set_notice(str(exc))
            return
        stopped_monitor = False
        cancelled_calibration = False
        if selected != "synced":
            # The acoustic probe needs Synced playback.  Do not leave a
            # persisted monitor enabled for a route it cannot measure, and
            # cancel any round that is still listening.
            stopped_monitor = self.cfg.acoustic_monitor
            self.cfg.acoustic_monitor = False
            self.monitor_check.blockSignals(True)
            self.monitor_check.setChecked(False)
            self.monitor_check.blockSignals(False)
            if self._calibration_thread is not None:
                cancelled_calibration = True
                self._calibration_cancelled = True
                self._calibration_thread.job.cancel()
        self.cfg.playback = selected
        self.cfg.save()
        self._acoustic_next = 0.0
        self._refresh_acoustic_label()
        if stopped_monitor or cancelled_calibration:
            detail = "Acoustic monitoring was stopped" if stopped_monitor else "Acoustic calibration was cancelled"
            self._set_notice(f"{detail}; acoustic measurement requires Synced playback.")

    def _restore_playback_radios(self) -> None:
        """Put the radios back on the persisted route without re-entering the slot."""
        for value, radio in self.play_radios.items():
            radio.blockSignals(True)
            radio.setChecked(self.cfg.playback == value)
            radio.blockSignals(False)

    def _on_preset_changed(self) -> None:
        for value, radio in self.preset_radios.items():
            if radio.isChecked():
                self.cfg.preset = value
        self.cfg.save()
        self.manual_spin.setEnabled(self.cfg.preset == "manual")
        self.controller.set_preset(self.cfg.preset, self._now(), self.cfg.effective_buffer_ms())
        self.discovery.update(preset=self.cfg.preset, buf_ms=self.controller.buffer_ms)
        if self.state == HOSTING:
            self._apply_buffer(self.controller.buffer_ms)

    def _on_manual_changed(self, value: int) -> None:
        self.cfg.manual_buffer_ms = int(value)
        self.cfg.save()
        if self.cfg.preset == "manual" and self.state == HOSTING:
            self.controller.set_preset("manual", self._now(), int(value))
            self._apply_buffer(int(value))

    # ------------------------------------------------------------------
    # acoustic sync
    # ------------------------------------------------------------------
    def _on_monitor_toggled(self, checked: bool) -> None:
        if checked:
            reason = self._acoustic_route_reason()
            if reason:
                self.monitor_check.blockSignals(True)
                self.monitor_check.setChecked(False)
                self.monitor_check.blockSignals(False)
                self.cfg.acoustic_monitor = False
                self.cfg.save()
                self._set_notice(reason)
                return
        self.cfg.acoustic_monitor = bool(checked)
        self.cfg.save()
        self._acoustic_next = 0.0
        self._refresh_acoustic_label()

    def _acoustic_route_reason(self) -> str:
        """Why acoustic measurement is unsafe right now; empty means allowed."""
        try:
            safety.validate_playback(self.cfg.playback)
        except safety.RoutingError as exc:
            return str(exc)
        if self.cfg.playback != "synced":
            return "Acoustic sync needs playback set to Synced."
        if self.state != HOSTING:
            return "Acoustic sync needs this device to be hosting."
        if (
            self._playing_job is not None
            or self._preload_thread is not None
            or self._preload_stop_thread is not None
        ):
            return "Stop preload playback before acoustic measurement."
        return ""

    def _on_monitor_interval(self, value: int) -> None:
        self.cfg.acoustic_interval_s = max(10, min(300, int(value)))
        self.cfg.save()
        self._acoustic_next = 0.0

    def _on_probe_volume(self, value: int) -> None:
        self.cfg.acoustic_volume = max(1, min(100, int(value)))
        self.cfg.save()

    def _refresh_acoustic_label(self) -> None:
        mode = f"monitor every {self.cfg.acoustic_interval_s}s" if self.cfg.acoustic_monitor else "monitor off"
        prefix = "microphone active \u00b7 " if self._mic_active else ""
        if self._acoustic_note:
            text = f"{prefix}{mode} \u00b7 {self._acoustic_note}"
        else:
            text = f"{prefix}{mode} \u00b7 no acoustic measurement yet"
        self.acoustic_label.setText(text)
        if self._acoustic_result:
            rows = self._acoustic_result.get("measurements") or []
            details = []
            for item in rows:
                name = item.get("name", "?")
                offset = item.get("offset_ms")
                if offset is None:
                    details.append(f"{name}: skipped ({item.get('error') or 'no answer'})")
                else:
                    details.append(f"{name}: {offset} ms, confidence {float(item.get('confidence') or 0):.2f}")
            self.acoustic_label.setToolTip("\n".join(details) or text)
        else:
            self.acoustic_label.setToolTip(
                "Physical microphones measure the speakers' acoustic arrival. "
                "Snapcast still corrects clock drift; this corrects output-path offset."
            )

    def _acoustic_targets(self) -> list[preload.Target]:
        targets: list[preload.Target] = []
        seen: set[str] = set()
        for row in self._last_rows:
            if not row.get("connected"):
                continue
            name = str(row.get("name") or row.get("id") or "").strip()
            host = str(row.get("host") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            if name == config.device_name() or preload.is_local_host(host):
                targets.append(preload.Target(name, None))
            else:
                targets.append(preload.Target(name, host or name))
        return targets

    def _on_choose_microphones(self) -> None:
        if self._mic_thread is not None:
            return
        targets = self._acoustic_targets()
        if not targets:
            self._alert("No devices", "Start hosting and connect at least one speaker first.")
            return
        try:
            targets = safety.unique_targets(targets, preload.local_host_names())
        except safety.RoutingError as exc:
            self._alert("Unsafe acoustic targets", str(exc))
            return
        self.mic_button.setEnabled(False)
        self._set_notice("Checking physical microphone sources...")
        thread = SourceDiscoveryThread(targets, self)
        thread.done.connect(lambda discovered: self._open_microphone_dialog(targets, discovered))
        thread.finished.connect(self._on_mic_discovery_finished)
        self._mic_thread = thread
        thread.start()

    def _open_microphone_dialog(self, targets: list[preload.Target], discovered: dict[str, list[str]]) -> None:
        dialog = MicrophoneDialog(targets, discovered, self.cfg.mic_sources, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.values()
            for name, source in values.items():
                try:
                    safety.safe_capture_source(source, require_metadata=False)
                except safety.RoutingError as exc:
                    self._alert("Unsafe microphone", f"{name}: {exc}")
                    return
                if source not in discovered.get(name, []):
                    self._alert(
                        "Unsafe microphone",
                        f"{name}: {source!r} was not in the verified physical-source list",
                    )
                    return
            for name, source in values.items():
                self.cfg.mic_sources[name] = source
            self.cfg.save()
            self._acoustic_note = f"microphones saved for {len(values)} device(s)"
            self._refresh_acoustic_label()
            self._set_notice("Microphone choices saved.")

    def _on_mic_discovery_finished(self) -> None:
        self.mic_button.setEnabled(True)
        if self._mic_thread is not None:
            self._mic_thread.deleteLater()
            self._mic_thread = None

    def _on_calibrate(self) -> None:
        if self._calibration_thread is not None:
            self._calibration_cancelled = True
            self._calibration_thread.job.cancel()
            self.calibrate_button.setEnabled(False)
            self._set_notice("Cancelling acoustic calibration...")
            return
        reason = self._acoustic_route_reason()
        if reason:
            self._alert(
                "Acoustic sync unavailable",
                reason,
            )
            return
        self._start_acoustic_round(manual=True)

    def _start_acoustic_round(self, manual: bool) -> bool:
        if self._calibration_thread is not None:
            return False
        reason = self._acoustic_route_reason()
        if reason:
            if manual:
                self._alert("Acoustic sync unavailable", reason)
            return False
        targets = self._acoustic_targets()
        if not targets:
            self._acoustic_note = "no connected speakers to measure"
            self._refresh_acoustic_label()
            return False
        try:
            targets = safety.unique_targets(targets, preload.local_host_names())
        except safety.RoutingError as exc:
            self._acoustic_note = str(exc)
            self._refresh_acoustic_label()
            if manual:
                self._alert("Unsafe acoustic targets", str(exc))
            return False
        sources = {target.name: self.cfg.mic_sources.get(target.name, "") for target in targets}
        job = calibration.AcousticCalibration(
            targets,
            sources,
            volume=self.cfg.acoustic_volume,
        )
        self._calibration_manual = manual
        self._calibration_cancelled = False
        thread = CalibrationThread(job, self)
        thread.progress.connect(self._on_calibration_progress)
        thread.failed.connect(self._on_calibration_failed)
        thread.done.connect(self._on_calibration_done)
        thread.finished.connect(self._on_calibration_finished)
        self._calibration_thread = thread
        self.calibrate_button.setText("Cancel calibration")
        self._acoustic_next = self._now() + max(10, int(self.cfg.acoustic_interval_s))
        if manual:
            self._set_notice(f"Measuring {len(targets)} device(s)...")
        thread.start()
        return True

    def _on_calibration_progress(self, message: str) -> None:
        if "microphone active" in message:
            self._mic_active = True
            self._refresh_acoustic_label()
        if self._calibration_manual:
            self._set_notice(message, seconds=4)

    def _on_calibration_done(self, result: dict) -> None:
        self._mic_active = False
        if self._calibration_cancelled:
            self._acoustic_note = "calibration cancelled"
            self._refresh_acoustic_label()
            self._set_notice("Acoustic calibration cancelled.")
            return
        sources = result.get("sources") or {}
        changed = False
        for name, source in sources.items():
            if source and self.cfg.mic_sources.get(name) != source:
                self.cfg.mic_sources[name] = source
                changed = True
        if changed:
            self.cfg.save()
        self._acoustic_result = result
        self._apply_acoustic_result(result)
        if self._calibration_manual:
            self._set_notice(self._acoustic_note or "Acoustic calibration finished.", seconds=10)

    def _on_calibration_failed(self, message: str) -> None:
        self._mic_active = False
        self._acoustic_note = f"measurement failed: {message}"
        self._refresh_acoustic_label()
        if self._calibration_manual:
            self._alert("Acoustic calibration failed", message)

    def _on_calibration_finished(self) -> None:
        self._mic_active = False
        self._calibration_cancelled = False
        self.calibrate_button.setText("Calibrate now")
        self.mic_button.setEnabled(True)
        if self._calibration_thread is not None:
            self._calibration_thread.deleteLater()
            self._calibration_thread = None
        self.calibrate_button.setEnabled(
            self.state == HOSTING and not self._acoustic_route_reason()
        )
        if self.cfg.acoustic_monitor and self.state == HOSTING:
            self._acoustic_next = self._now() + max(10, int(self.cfg.acoustic_interval_s))

    def _apply_acoustic_result(self, result: dict) -> None:
        report = self._latency_applier.apply_result(
            result,
            self._last_rows,
            self._now(),
            apply_buffer=self._apply_buffer,
            persist_floor=self._persist_learned_floor,
        )
        self._acoustic_note = report.note
        self._refresh_acoustic_label()

    def _apply_saved_latencies(self, rows: list[dict]) -> None:
        if self.state != HOSTING or not self.cfg.client_latency_ms:
            return
        self._latency_applier.reapply(
            rows,
            self._now(),
            apply_buffer=self._apply_buffer,
            persist_floor=self._persist_learned_floor,
        )

    def _maybe_acoustic_monitor(self, now: float, rows: list[dict], playing: bool) -> None:
        if not self.cfg.acoustic_monitor or self._acoustic_route_reason():
            return
        if self.state != HOSTING or not playing or not rows or self._calibration_thread is not None:
            return
        if now < self._acoustic_next:
            return
        if not any(row.get("connected") for row in rows):
            return
        self._start_acoustic_round(manual=False)

    # ------------------------------------------------------------------
    # roles
    # ------------------------------------------------------------------
    def _on_add_manual(self) -> None:
        host, ok = QInputDialog.getText(self, "Add device", "Address or hostname:")
        host = (host or "").strip()
        if not ok or not host:
            return
        self.discovery.add_manual(host)
        if host not in self.cfg.extra_hosts:
            self.cfg.extra_hosts.append(host)
            self.cfg.save()
        self._refresh_hosts()

    def _on_start(self) -> None:
        target = self.host_combo.currentData()
        if target:
            self._join(target)
        else:
            self._start_host()

    def _choose_codec(self) -> str:
        peers = [p for p in self.discovery.peers() if p.is_host or p.address]
        if not peers:
            return "pcm"
        worst = None
        for peer in peers:
            probe = codec_mod.probe_link(peer.address, config.SNAPCAST_STREAM_PORT, samples=2, timeout=0.4)
            if worst is None or (probe.rtt_ms or 0) > (worst.rtt_ms or 0):
                worst = probe
        chosen, reason = codec_mod.choose_codec(worst, preferred="pcm")
        self.cfg.codec = chosen
        self.cfg.save()
        if chosen != "pcm":
            self.status.setText(f"codec downgraded to {chosen}: {reason}")
        return chosen

    def _codec_floor(self, codec: str) -> int:
        """Compressed codecs need a little more buffer than raw PCM."""
        return max(100, codec_mod.buffer_floor_for(codec))

    def _start_host(self, force: bool = False) -> None:
        if self._preload_thread is not None or self._playing_job is not None or self._preload_stop_thread is not None:
            self._alert(
                "Preload playback is active",
                "Stop preload playback before hosting; a local player writing to the "
                "virtual sink would be captured and streamed back to the group.",
            )
            return
        others = [p for p in self.discovery.hosts() if p.name != config.device_name()]
        if others and not force:
            names = ", ".join(p.name for p in others)
            if not self._confirm(
                "Another device is hosting",
                f"{names} is already hosting audio.\n\nStart a second server on this device anyway?",
            ):
                self.status.setText(f"{names} is already hosting; refused to start a second server.")
                return
        buffer_ms = self.controller.buffer_ms if self.cfg.preset == "auto" else self.cfg.effective_buffer_ms()
        if self.state == HOSTING:
            self._restore_gate_volumes()
        codec = self._choose_codec()
        try:
            self._refresh_output(hosting=True, restart=False)
        except BackendError as exc:
            self._alert("Output unavailable", str(exc))
            return
        # The server may already be running from a previous session; clear this
        # device's records before it loads its persisted history again.
        self.backend.forget_pairings("127.0.0.1", config.device_name(), local=True)
        try:
            self.backend.host(buffer_ms=buffer_ms, codec=codec, playback=self.cfg.playback)
        except BackendError as exc:
            self._alert("Could not start", str(exc))
            return
        # Second pass: only disconnected ghosts, never the client that just
        # joined.  On a cold start the pre-host file prune already covered us.
        try:
            rows_now = self.backend.clients("127.0.0.1")
        except BackendError:
            rows_now = []
        if any(row["connected"] and row["name"] == config.device_name() for row in rows_now):
            self.backend.forget_pairings("127.0.0.1", config.device_name(), include_connected=False)
        self.state = HOSTING
        self._unit_states = ""
        self.host_ip = "127.0.0.1"
        self.controller = LatencyController(
            ControllerConfig(floor_ms=self._codec_floor(codec)),
            buffer_ms=buffer_ms,
            learned_floor_ms=self.cfg.learned_floor(config.device_name()),
        )
        now = self._now()
        self.controller.set_preset(self.cfg.preset, now, buffer_ms)
        self.gate.begin(now, buffer_ms)
        self._preroll_active = False
        self._armed.clear()
        self._gate_ids.clear()
        self._gate_muted.clear()
        self._pending.clear()
        self._saved_volumes.clear()
        self._ramp.clear()
        self.tracker.reset()
        self.underruns.reset()
        self._remote_watchers.clear()
        self._latency_applier.reset(self.controller, self.host_ip)
        self._acoustic_next = now + max(10, int(self.cfg.acoustic_interval_s)) if self.cfg.acoustic_monitor else 0.0
        self._ignore_drops_until = 0.0
        self._last_diagnosis = ""
        self.discovery.update(role="host", serves=config.device_name(), buf_ms=buffer_ms, preset=self.cfg.preset)

    def _join(self, address: str) -> None:
        if self._preload_thread is not None or self._playing_job is not None or self._preload_stop_thread is not None:
            self._alert(
                "Preload playback is active",
                "Stop preload playback before joining a host; its players would otherwise "
                "mix with the Snapcast stream.",
            )
            return
        diagnosis = self.backend.diagnose_peer(address)
        if not diagnosis["ok"]:
            # Probe the actual ports rather than blaming "the network".
            self._alert(f"Cannot reach {address}", diagnosis["summary"])
            self.status.setText(diagnosis["summary"])
            return
        self._refresh_output(restart=False)
        self.backend.stop_client()
        cleared = self.backend.forget_pairings(address, config.device_name())
        if cleared:
            self._output_note += f" \u00b7 cleared {cleared} stale pairing(s)"
        try:
            self.backend.join(address)
        except BackendError as exc:
            self._alert("Could not join", f"{exc}\n\n{diagnosis['summary']}")
            return
        self.state = JOINING
        self._unit_states = ""
        self.host_ip = address
        peer = self.discovery.find(address)
        self.discovery.update(role="client", serves=peer.name if peer else address, preset=self.cfg.preset)

    def _reset_session(self) -> None:
        """Return the app to idle without touching the systemd units.

        A stop during the ramp would persist a near-zero volume on the
        server, so every gated speaker is restored first.  The units are
        left alone: callers that want them down stop them explicitly.
        """
        self._restore_gate_volumes()
        self.state = IDLE
        self.host_ip = "127.0.0.1"
        self._preroll_active = False
        self._armed.clear()
        self._gate_ids.clear()
        self._gate_muted.clear()
        self._pending.clear()
        self._saved_volumes.clear()
        self._ramp.clear()
        self.tracker.reset()
        self._last_rows = []
        self._remote_watchers.clear()
        self._acoustic_next = 0.0
        self._last_diagnosis = ""
        self.discovery.update(role="idle", serves="")

    def _on_stop(self) -> None:
        self._reset_session()
        self.backend.stop()

    # ------------------------------------------------------------------
    # live loop
    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        now = self._now()
        try:
            self._rebase_clock(now)
            self._refresh_ui(now)
        finally:
            self._schedule_refresh()

    def _refresh_ui(self, now: float) -> None:
        self._ticks += 1
        self._sync_library_state()
        if self._ticks % 5 == 1 or not self._unit_states:
            self._unit_states = self.backend.status_line("127.0.0.1")
        self._refresh_hosts()
        rows: list[dict] = []
        playing = False
        if self.state in (HOSTING, JOINING, STREAMING):
            try:
                rows = self.backend.clients(self.host_ip)
                playing = self.backend.stream_playing(self.host_ip)
            except BackendError:
                if self.state in (JOINING, STREAMING):
                    self.state = LOST
                rows = []
            self._render_clients(rows)
            self._last_rows = rows
            if self.state == JOINING and any(r["connected"] for r in rows):
                self.state = STREAMING

        if self.state == LOST and now - self._last_retry > 5.0:
            # Keep trying the host we lost; Start stays available as an
            # explicit escape hatch to host here instead.
            self._last_retry = now
            self._refresh_output(restart=False)
            self.backend.forget_pairings(self.host_ip, config.device_name())
            try:
                self.backend.join(self.host_ip)
                if server_ready(self.host_ip, timeout=0.5):
                    self.state = JOINING
                    self.tracker.reset()
            except BackendError:
                self._last_diagnosis = self.backend.diagnose_peer(self.host_ip)["summary"]

        if self.state == HOSTING:
            self._apply_saved_latencies(rows)
            self._check_health(now, rows, playing)
            changed = self.controller.tick(now, playing)
            if changed is not None:
                self._apply_buffer(changed)
            self._enforce_preroll(now, rows, playing)
            self._maybe_acoustic_monitor(now, rows, playing)

        self._advance_ramp(now)
        self._render_status(rows, playing)
        if self._playing_job is not None:
            alive = self._playing_job.playing()
            if alive:
                self._set_notice(
                    f"Playing {Path(self._playing_job.media).name} on {alive} device(s)",
                    seconds=2,
                )
            else:
                self._playing_job = None
                self._set_notice("Preload finished.")

    def _apply_buffer(self, buffer_ms: int) -> None:
        ok = self.backend.apply_buffer(self.host_ip, buffer_ms, self.cfg.codec, self.cfg.playback)
        lipsync.mpv_set_offset(None, buffer_ms)
        self.discovery.update(buf_ms=buffer_ms)
        # Our own restart looks like every client dropping; never count it.
        self._ignore_drops_until = self._now() + 4.0
        if not ok:
            self.status.setText("buffer change failed; check journalctl --user -u snapserver")

    def _check_health(self, now: float, rows: list[dict], playing: bool) -> None:
        """Feed real underruns and client drops into the controller."""
        if now - self._last_health < 5.0:
            return
        self._last_health = now
        if now >= self._ignore_drops_until:
            if self.tracker.observe(rows):
                changed = self.controller.on_disconnect(now)
                self._persist_learned_floor()
                if changed is not None:
                    self._apply_buffer(changed)
        else:
            self.tracker.observe(rows)
        connected = [r for r in rows if r["connected"]]
        if playing and connected:
            # No connected client means nothing can underrun (and a stopped
            # unit's old journal lines must never be counted).
            for _ in range(min(self.underruns.poll(), 3)):
                changed = self.controller.on_client_underrun(config.device_name(), now)
                self._persist_learned_floor()
                if changed is not None:
                    self._apply_buffer(changed)
            remote = {}
            for row in connected:
                name = str(row.get("name") or "")
                host = str(row.get("host") or "")
                if not name or not host or host == "?":
                    continue
                if name == config.device_name() or preload.is_local_host(host):
                    continue
                watcher = self._remote_watchers.get(name)
                if watcher is None or watcher.host != host:
                    watcher = RemoteUnderrunWatcher(name, host)
                    self._remote_watchers[name] = watcher
                remote[name] = watcher
            if remote and self._remote_health_thread is None:
                thread = RemoteHealthThread(remote, self)
                thread.counts.connect(self._on_remote_health)
                thread.finished.connect(self._on_remote_health_finished)
                self._remote_health_thread = thread
                thread.start()

    def _on_remote_health(self, counts: dict) -> None:
        """Apply one off-thread poll; the GUI thread owns controller changes."""
        if self.state != HOSTING:
            return
        now = self._now()
        for name, count in counts.items():
            for _ in range(min(int(count), 3)):
                changed = self.controller.on_client_underrun(str(name), now)
                self._persist_learned_floor()
                if changed is not None:
                    self._apply_buffer(changed)

    def _on_remote_health_finished(self) -> None:
        if self._remote_health_thread is not None:
            self._remote_health_thread.deleteLater()
            self._remote_health_thread = None

    def _refresh_output(self, hosting: bool = False, restart: bool = True) -> bool:
        """Keep the cached sink honest; the next join/host restarts what it must."""
        try:
            changed, sink, note = self.backend.refresh_outputs(hosting=hosting, restart=restart)
        except BackendError:
            if hosting:
                raise
            self._output_note = "output check failed"
            return False
        if sink:
            self._output_note = f"output \u2192 {note}"
        return changed

    def _persist_learned_floor(self) -> None:
        """Remember a promoted floor per host so a restart does not relearn it."""
        learned = self.controller.learned_floor
        if learned is None:
            return
        key = config.device_name()
        if self.cfg.learned_floor_ms.get(key) != learned:
            self.cfg.learned_floor_ms[key] = learned
            self.cfg.save()

    def _advance_ramp(self, now: float) -> None:
        """500 ms volume ramp after the pre-roll gate releases a speaker."""
        for gate_key, (released_at, target) in list(self._ramp.items()):
            client_id = self._gate_ids.get(gate_key)
            if not client_id:
                self._ramp.pop(gate_key, None)
                continue
            fraction = min(1.0, (now - released_at) / 0.5)
            self._set_volume(client_id, int(round(target * fraction)))
            if fraction >= 1.0:
                self._ramp.pop(gate_key, None)
                self._gate_muted.discard(gate_key)

    def _enforce_preroll(self, now: float, rows: list[dict], playing: bool) -> None:
        """Keep each speaker muted until its buffer is genuinely primed.

        Every client is gated exactly once per hosting session (``_armed``),
        otherwise the next refresh would re-mute it and the group would
        pulse instead of settling.
        """
        if not rows:
            return
        if not playing:
            # Nothing is flowing, so nothing needs protecting; never leave a
            # speaker muted just because the stream went idle mid-priming.
            self._release_pending()
            return
        for row in rows:
            client_id = row["id"]
            # A reconnect gets a new client id; all gate state is keyed by the
            # stable name and resolves the current id when it changes.
            gate_key = row.get("name") or client_id
            if row["connected"]:
                self._gate_ids[gate_key] = client_id
            if not row["connected"]:
                continue
            if gate_key in self._armed:
                # A reconnect can inherit Snapserver's persisted mute (the
                # zero-MAC clients reuse their id).  Only undo a mute the
                # gate itself applied, never one the user chose in the UI.
                if (
                    gate_key in self._gate_muted
                    and gate_key not in self._pending
                    and gate_key not in self._ramp
                    and row["volume"].get("muted")
                ):
                    self._set_volume(client_id, self._saved_volumes.get(gate_key, 100))
                    self._gate_muted.discard(gate_key)
                continue
            self._armed.add(gate_key)
            self._gate_muted.add(gate_key)
            self._saved_volumes[gate_key] = row["volume"].get("percent", 100)
            self._mute(client_id, True)
            self._pending[gate_key] = now + self.gate.required_s
            self._preroll_active = True
        for gate_key, ready_at in list(self._pending.items()):
            if now >= ready_at:
                # Release into a short ramp rather than a hard jump
                self._ramp[gate_key] = (now, self._saved_volumes.get(gate_key, 100))
                self._pending.pop(gate_key, None)
        if not self._pending:
            self._preroll_active = False

    def _release_pending(self) -> None:
        for gate_key in list(self._pending):
            client_id = self._gate_ids.get(gate_key)
            if client_id:
                self._set_volume(client_id, self._saved_volumes.get(gate_key, 100))
                self._gate_muted.discard(gate_key)
        self._pending.clear()
        self._preroll_active = False

    def _restore_gate_volumes(self) -> None:
        """Undo any gate mute/ramp before stopping or re-hosting."""
        for gate_key in set(self._pending) | set(self._ramp) | set(self._gate_muted):
            client_id = self._gate_ids.get(gate_key)
            if client_id:
                self._set_volume(client_id, self._saved_volumes.get(gate_key, 100))
        self._pending.clear()
        self._ramp.clear()
        self._gate_muted.clear()
        self._preroll_active = False

    def _mute(self, client_id: str, muted: bool) -> None:
        try:
            self.backend.set_client_volume(self.host_ip, client_id, 100, muted)
        except BackendError:
            pass

    def _set_volume(self, client_id: str, percent: int) -> None:
        try:
            self.backend.set_client_volume(self.host_ip, client_id, percent, False)
        except BackendError:
            pass

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------
    def _refresh_hosts(self) -> None:
        peers = self.discovery.peers()
        current = self.cfg.preferred_host
        self.host_combo.blockSignals(True)
        self.host_combo.clear()
        self.host_combo.addItem("This device", None)
        for peer in peers:
            label = f"{peer.name} ({peer.address})"
            if peer.role == "host":
                label += " \u2014 hosting"
            self.host_combo.addItem(label, peer.address)
        index = self.host_combo.findData(current) if current else 0
        self.host_combo.setCurrentIndex(index if index >= 0 else 0)
        self.host_combo.blockSignals(False)

        new_addresses = {p.address for p in peers}
        if new_addresses - self._known_peers and self.state == HOSTING:
            self._known_peers |= new_addresses
            # A new peer may need a cheaper codec; re-evaluate on the next start
        self._known_peers |= new_addresses

    def _render_clients(self, rows: list[dict]) -> None:
        ids = [row["id"] for row in rows]
        if ids != list(self._client_widgets):
            self.table.setRowCount(len(rows))
            self._client_widgets = {}
            for index, row in enumerate(rows):
                self.table.setItem(index, 0, QTableWidgetItem(row["name"]))
                check = QCheckBox()
                check.setAccessibleName(f"Play {row['name']}")
                check.setChecked(not row["volume"].get("muted", False))
                check.toggled.connect(lambda checked, cid=row["id"]: self._on_mute_toggled(cid, checked))
                self.table.setCellWidget(index, 1, check)
                slider = QSlider(Qt.Orientation.Horizontal)
                slider.setAccessibleName(f"Volume for {row['name']}")
                slider.setRange(0, 100)
                slider.setValue(int(row["volume"].get("percent", 100)))
                slider.valueChanged.connect(lambda value, cid=row["id"]: self._set_volume(cid, int(value)))
                self.table.setCellWidget(index, 2, slider)
                self._client_widgets[row["id"]] = {"check": check, "slider": slider}
        else:
            for index, row in enumerate(rows):
                widgets = self._client_widgets.get(row["id"])
                if not widgets:
                    continue
                widgets["check"].blockSignals(True)
                widgets["check"].setChecked(not row["volume"].get("muted", False))
                widgets["check"].blockSignals(False)
                widgets["slider"].blockSignals(True)
                widgets["slider"].setValue(int(row["volume"].get("percent", 100)))
                widgets["slider"].blockSignals(False)

    def _on_mute_toggled(self, client_id: str, playing: bool) -> None:
        try:
            self.backend.set_client_volume(self.host_ip, client_id, 100, not playing)
        except BackendError:
            pass

    def _render_status(self, rows: list[dict], playing: bool) -> None:
        buffer_ms = self.controller.buffer_ms
        peers = len(self.discovery.peers())
        peer_text = f"{peers} peer" + ("" if peers == 1 else "s")
        self.header.setText(f"<b>{config.device_name()}</b> \u00b7 {peer_text}")
        speakers = f"{len(rows)} speaker" + ("" if len(rows) == 1 else "s")
        if self.state == HOSTING:
            status = self.controller.status()
            parts = [f"{buffer_ms} ms"]
            if self.cfg.preset == "auto":
                parts.append(f"floor {self.controller.floor} ms")
            underruns = int(status["underruns"])
            parts.append(f"{underruns} underrun" + ("" if underruns == 1 else "s"))
            self.latency_label.setText("Buffer: " + " \u00b7 ".join(parts))
            self.latency_label.setToolTip(
                "Snapcast buffer, learned floor and underrun count; not an acoustic measurement.\n"
                f"Adaptive steps down: {status['steps_down']} \u00b7 Auto max: {status['auto_max_ms']} ms"
            )
            held_by = status.get("held_by") or []
            if held_by:
                parts.append("hold: " + ", ".join(str(name) for name in held_by))
                self.latency_label.setText("Buffer: " + " \u00b7 ".join(parts))
            state_text = "Streaming" if playing else "Waiting for audio"
            output_note = f" \u00b7 {self._output_note}" if self._output_note else ""
            units = f" \u00b7 {self._unit_states}" if self._unit_states else ""
            self.status.setText(
                f"{state_text} \u00b7 {speakers} \u00b7 codec {self.cfg.codec}{units}{output_note}"
            )
        elif self.state in (JOINING, STREAMING):
            self.latency_label.setText(f"Buffer: {buffer_ms} ms (host-controlled)")
            self.latency_label.setToolTip("The host chooses the buffer for this device.")
            verb = "Streaming from" if self.state == STREAMING else "Joining"
            output_note = f" \u00b7 {self._output_note}" if self._output_note else ""
            self.status.setText(f"{verb} {self.host_ip} \u00b7 {speakers}{output_note}")
        elif self.state == LOST:
            self.latency_label.setText("Buffer: \u2014")
            self.latency_label.setToolTip("Waiting for the host to return.")
            text = "Host lost - retrying every 5 s. Press Start to host here instead."
            if self._last_diagnosis:
                text = f"{text}\n{self._last_diagnosis}"
            self.status.setText(text)
        else:
            self.latency_label.setText("Buffer: \u2014")
            self.latency_label.setToolTip("Start hosting or join a host to see the buffer.")
            output_note = f"\n{self._output_note}" if self._output_note else ""
            self.status.setText("Stopped. Choose a host and press Start." + output_note)

        guidance = lipsync.guidance(buffer_ms)
        self.sync_label.setText(f"Audio offset {guidance['offset']}")
        self.sync_label.setToolTip(
            f"VLC: {guidance['vlc']}\n{guidance['kodi']}\n{guidance['browser']}\n{guidance['mpv']}"
        )
        self.start_button.setEnabled(self.state == IDLE or self.state == LOST)
        self.stop_button.setEnabled(self.state != IDLE)
        self.manual_spin.setEnabled(self.cfg.preset == "manual")
        playback_locked = self.state != IDLE
        for radio in self.play_radios.values():
            radio.setEnabled(not playback_locked)
        can_measure = self.state == HOSTING and not self._acoustic_route_reason()
        self.calibrate_button.setEnabled(can_measure or self._calibration_thread is not None)
        self.monitor_check.setEnabled(can_measure or self.cfg.acoustic_monitor)
        self.monitor_interval.setEnabled(can_measure)
        self.probe_volume.setEnabled(can_measure)
        self.mic_button.setEnabled(can_measure and self._mic_thread is None)
        self.preload_button.setEnabled(
            self.state == IDLE
            and self._preload_thread is None
            and self._preload_stop_thread is None
            and self._calibration_thread is None
            and self._playing_job is None
        )
        if self._notice and self._now() < self._notice_until:
            self.status.setText(self._notice)
        elif self._notice:
            self._notice = ""

    # ------------------------------------------------------------------
    # lip-sync helpers
    # ------------------------------------------------------------------
    def _copy_offset(self) -> None:
        QApplication.clipboard().setText(lipsync.guidance(self.controller.buffer_ms)["vlc"])

    def _launch_mpv(self) -> None:
        try:
            lipsync.launch_mpv(self.controller.buffer_ms)
        except FileNotFoundError:
            QMessageBox.warning(self, "mpv missing", "Install mpv to use this shortcut. VLC and Kodi offsets are in the tooltip.")
            return
        self.status.setText("mpv launched with the offset applied; drag a file onto it.")

    # ------------------------------------------------------------------
    # local-file preload
    # ------------------------------------------------------------------
    def _preload_targets(self) -> list[preload.Target]:
        """This device first, then discovered peers and manually saved hosts."""
        seen = {config.device_name()}
        targets = [preload.Target(config.device_name(), None)]
        for target in preload.peer_targets(self.discovery.peers()):
            if target.name in seen:
                continue
            targets.append(target)
            seen.add(target.name)
        for host in self.cfg.extra_hosts:
            if host not in seen and not preload.is_local_host(host):
                targets.append(preload.Target(host, host))
                seen.add(host)
        return targets

    def _preload_start_dir(self) -> Path:
        """Most recent synced root, otherwise the user's music folder."""
        for record in reversed(self.cfg.library_roots or []):
            root = Path(str(record.get("path") or "")).expanduser()
            if root.is_dir():
                return root
        music = Path.home() / "Music"
        return music if music.is_dir() else Path.home()

    def _on_preload(self) -> None:
        if self._preload_thread is not None or self._preload_stop_thread is not None:
            return
        if self.state != IDLE:
            self._alert(
                "Preload requires an idle group",
                "Preload bypasses Snapcast. Stop hosting or joining first so its "
                "players cannot be captured back into the stream.",
            )
            return
        if self.cfg.acoustic_monitor:
            self._alert(
                "Preload requires monitoring off",
                "Turn off acoustic monitoring before preload; its probe and "
                "microphone capture must not overlap the local players.",
            )
            return
        if self._calibration_thread is not None or self._playing_job is not None:
            self._alert(
                "Preload unavailable",
                "Finish or cancel acoustic measurement and stop existing preload "
                "playback before starting another preload.",
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Preload media on every device",
            str(self._preload_start_dir()),
            "Media (*.mp4 *.mkv *.webm *.mov *.avi *.m4v *.ts *.mpg *.mpeg "
            "*.mp3 *.flac *.opus *.ogg *.wav *.m4a);;All files (*)",
        )
        if not path:
            return

        targets = self._preload_targets()
        if len(targets) < 2:
            self._alert(
                "No peer devices",
                "Preload needs at least one other device running Audiosync on the same network.",
            )
            return
        names = ", ".join(target.name for target in targets)
        lib = library_mod.find_library(path, self.cfg.library_roots)
        detail = ""
        if lib is not None:
            detail = f"\n\n{lib.label} is synced: devices already holding this file reuse it."
        if not self._confirm(
            "Preload and play",
            f"Copy {Path(path).name} to {len(targets)} device(s):\n{names}\n\n"
            "Each device verifies the complete file, then playback starts after a 10 s delay."
            + detail,
        ):
            return

        self._preload_cancelled = False
        self.preload_button.setEnabled(False)
        self._set_notice(f"Preloading {Path(path).name}...")
        try:
            job = preload.Job(path, targets, delay_s=10.0, library=lib)
        except preload.PreloadError as exc:
            self.preload_button.setEnabled(True)
            self._alert("Unsafe preload targets", str(exc))
            return
        thread = PreloadThread(job, self)
        thread.progress.connect(self._set_notice)
        thread.failed.connect(self._on_preload_failed)
        thread.done.connect(self._on_preload_done)
        thread.finished.connect(self._on_preload_finished)
        self._preload_thread = thread
        thread.start()

    def _on_preload_failed(self, message: str) -> None:
        self._playing_job = None
        self._alert("Preload failed", message)
        self.status.setText(f"Preload failed: {message}")

    def _on_preload_done(self, count: int) -> None:
        failures = list(self._preload_thread.job.failures) if self._preload_thread else []
        job = self._preload_thread.job if self._preload_thread else None
        if self._preload_cancelled:
            self._playing_job = None
            self._set_notice("Preload playback stopped.")
            return
        if count and job is not None:
            # A partial run still has live players.  Remember them so a second
            # preload or host start cannot stack another playback path on top.
            self._playing_job = job
        if failures:
            self._set_notice(
                f"Preload started on {count} device(s); "
                f"skipped {len(failures)} unsafe/unreachable device(s)."
            )
            self._alert(
                "Some devices were skipped",
                "Playback started on the devices that answered. "
                "These were not used:\n\n" + "\n".join(failures),
            )
            return
        self._set_notice(f"Preload started on {count} device(s).")

    def _on_preload_finished(self) -> None:
        self.preload_button.setEnabled(True)
        if self._preload_thread is not None:
            self._preload_thread.deleteLater()
            self._preload_thread = None

    # ------------------------------------------------------------------
    # media library sync
    # ------------------------------------------------------------------
    def _sync_library_state(self) -> None:
        """Pick up a folder synced by another instance without a restart."""
        path = config.config_file()
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return
        if mtime == self._library_config_mtime:
            return
        self._library_config_mtime = mtime
        try:
            roots = config.Config.load().library_roots
        except Exception:  # noqa: BLE001 - a bad config must not stop the loop
            return
        self.cfg.library_roots = roots
        self._refresh_library_label()

    def _refresh_library_label(self) -> None:
        roots = self.cfg.library_roots or []
        if not roots:
            self.library_label.setText("No synced folder")
            return
        identity = str(roots[-1].get("identity") or "")
        text = (
            library_mod.label_for(identity)
            if identity
            else str(roots[-1].get("path") or "synced folder")
        )
        if self._library_result:
            text += f" \u00b7 {self._library_result}"
        self.library_label.setText(text)

    def _remember_library(self, lib: library_mod.Library) -> None:
        """Persist the canonical path/identity, replacing an earlier record."""
        roots = [
            record
            for record in self.cfg.library_roots or []
            if record.get("identity") != lib.identity and record.get("path") != lib.path
        ]
        roots.append({"path": lib.path, "identity": lib.identity})
        self.cfg.library_roots = roots
        self.cfg.save()

    def _on_sync_folder(self) -> None:
        if self._library_thread is not None:
            self._library_cancelled = True
            self._library_thread.job.cancel()
            self.sync_folder_button.setEnabled(False)
            self._set_notice("Cancelling folder sync...")
            return

        path = QFileDialog.getExistingDirectory(
            self, "Sync a media folder to every device", str(self._preload_start_dir())
        )
        if not path:
            return
        try:
            lib = library_mod.resolve_source(path)
            stats = library_mod.scan_directory(lib.path)[1]
        except library_mod.LibraryError as exc:
            self._alert("Folder sync failed", str(exc))
            return

        targets = self._preload_targets()
        if len(targets) < 2:
            self._alert(
                "No peer devices",
                "Folder sync needs at least one other device running Audiosync "
                "on the same network.",
            )
            return
        names = ", ".join(target.name for target in targets)
        if not self._confirm(
            "Sync folder",
            f"Copy {stats.files} file(s), {stats.bytes / 1048576:.1f} MiB, from "
            f"{lib.label} to {len(targets)} device(s):\n{names}\n\n"
            "Later preloads from this folder reuse the synced copy instead of "
            "transferring the file again.",
        ):
            return

        self._remember_library(lib)
        self._library_result = ""
        self._refresh_library_label()
        self._library_cancelled = False
        self.sync_folder_button.setText("Cancel sync")
        self._set_notice(f"Syncing {lib.label}...")
        thread = LibrarySyncThread(
            library_mod.FolderSyncJob(lib.path, targets, lib.identity), self
        )
        thread.progress.connect(self._set_notice)
        thread.failed.connect(self._on_library_failed)
        thread.skipped.connect(self._on_library_skipped)
        thread.done.connect(self._on_library_done)
        thread.finished.connect(self._on_library_finished)
        self._library_thread = thread
        thread.start()

    def _on_library_skipped(self, message: str) -> None:
        """Report an absent root in the status bar instead of an error dialog."""
        self._library_result = "sync skipped"
        self._refresh_library_label()
        self._set_notice(f"Sync skipped: {message}")
        self.status.setText(f"Folder sync skipped: {message}")

    def _on_library_failed(self, message: str) -> None:
        self._library_result = "sync failed"
        self._refresh_library_label()
        self._alert("Folder sync failed", message)
        self.status.setText(f"Folder sync failed: {message}")

    def _on_library_done(self, copied: int) -> None:
        failures = list(self._library_thread.job.failures) if self._library_thread else []
        if self._library_cancelled:
            self._library_result = "sync cancelled"
            self._refresh_library_label()
            self._set_notice("Folder sync cancelled; files already copied stay cached.")
            return
        result = f"synced to {copied} device(s)"
        if failures:
            result += f", {len(failures)} skipped"
        self._library_result = result
        self._refresh_library_label()
        if failures:
            self._set_notice(
                f"Folder synced to {copied} device(s); "
                f"skipped {len(failures)} unreachable device(s)."
            )
            self._alert(
                "Some devices were skipped",
                "The folder is cached on the devices that answered. "
                "Preload will copy only the selected file to these:\n\n"
                + "\n".join(failures),
            )
            return
        self._set_notice(f"Folder synced to {copied} device(s); preloads can reuse it.")

    def _on_library_finished(self) -> None:
        self.sync_folder_button.setEnabled(True)
        self.sync_folder_button.setText("Sync folder\u2026")
        if self._library_thread is not None:
            self._library_thread.deleteLater()
            self._library_thread = None

    def _on_stop_playback(self) -> None:
        if self._preload_stop_thread is not None:
            return
        self._preload_cancelled = True
        if self._preload_thread is not None:
            self._preload_thread.job.cancel()
        self.stop_playback_button.setEnabled(False)
        self._set_notice("Stopping playback on all devices...")
        thread = PreloadStopThread(self._preload_targets(), self)
        thread.done.connect(self._on_stop_playback_done)
        thread.failed.connect(self._on_stop_playback_failed)
        thread.finished.connect(self._on_stop_playback_finished)
        self._preload_stop_thread = thread
        thread.start()

    def _on_stop_playback_done(self, count: int) -> None:
        # ``preload.stop`` already put back the Snapcast units it had to stop,
        # so only the session is reset: idle means the latency gate and
        # pre-roll stop touching every peer's volume.
        self._playing_job = None
        self._reset_session()
        if count:
            self._set_notice(f"Stopped playback on {count} device(s); Snapcast restored.")
        else:
            self._set_notice("No Audiosync playback was running.")

    def _on_stop_playback_failed(self, message: str) -> None:
        self._alert("Stop playback failed", message)
        self.status.setText(f"Stop playback failed: {message}")

    def _on_stop_playback_finished(self) -> None:
        self.stop_playback_button.setEnabled(True)
        if self._preload_stop_thread is not None:
            self._preload_stop_thread.deleteLater()
            self._preload_stop_thread = None

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.discovery.stop()
        if self._library_thread is not None:
            self._library_cancelled = True
            self._library_thread.job.cancel()
        if self._calibration_thread is not None:
            self._calibration_cancelled = True
            self._calibration_thread.job.cancel()
        super().closeEvent(event)


def run_gui(smoke: bool = False) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Audiosync")
    app.setQuitOnLastWindowClosed(not QSystemTrayIcon.isSystemTrayAvailable())
    window = MainWindow(config.Config.load(), silent=smoke)
    window.show()
    if smoke:
        QTimer.singleShot(1200, app.quit)
    return app.exec()
