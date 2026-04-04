from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from . import services
from ._environment import ENVIRONMENT as BAKED_ENVIRONMENT
from .exceptions import ArcsecondGatewayError, FocaleError

WorkerFunc = Callable[[Callable[[str], None]], object]


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    log = Signal(str)
    finished = Signal()


class FunctionWorker(QRunnable):
    def __init__(self, fn: WorkerFunc) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.fn = fn
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = self.fn(self.signals.log.emit)
        except Exception as exc:
            if isinstance(exc, (FocaleError, ArcsecondGatewayError)):
                self.signals.error.emit(str(exc))
            else:
                self.signals.error.emit(traceback.format_exc())
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()


class FocaleWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.thread_pool = QThreadPool(self)
        self._busy_count = 0
        self._active_workers: dict[
            int, tuple[FunctionWorker, str, Callable[[Any], None] | None]
        ] = {}
        self._settings = services.user_settings()

        env_suffix = f" — {services.environment_label(BAKED_ENVIRONMENT)}" if BAKED_ENVIRONMENT != "production" else ""
        self.setWindowTitle(f"Focale {__version__}{env_suffix}")
        self.resize(980, 760)
        self.setStatusBar(QStatusBar(self))

        root = QWidget(self)
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        heading = QLabel("Focale")
        heading.setStyleSheet("font-size: 24px; font-weight: 600;")
        layout.addWidget(heading)

        subheading = QLabel(
            "Desktop controls for Focale session setup, Hub diagnostics, and local plate solving."
        )
        subheading.setWordWrap(True)
        layout.addWidget(subheading)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_account_tab(), "Account")
        self.tabs.addTab(self._build_alpaca_tab(), "Alpaca Server")
        self.tabs.addTab(self._build_platesolver_tab(), "Plate Solver")
        layout.addWidget(self.tabs, 1)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setPlaceholderText("Action logs and JSON results will appear here.")
        self.log_output.setMinimumHeight(180)
        layout.addWidget(self.log_output)

        self._refresh_status_summary()

    # ------------------------------------------------------------------ #
    # Tab builders                                                         #
    # ------------------------------------------------------------------ #

    def _build_account_tab(self) -> QWidget:
        tab = QWidget()
        layout = QGridLayout(tab)
        layout.setSpacing(12)
        layout.setColumnStretch(0, 3)
        layout.setColumnStretch(1, 2)

        # --- Focale Account ---
        account_box = QGroupBox("Focale Account")
        account_form = QFormLayout(account_box)
        account_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        account_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.username_input = QLineEdit(self._settings.get("username") or "")
        self.username_input.setMinimumWidth(220)
        self.secret_input = QLineEdit()
        self.secret_input.setMinimumWidth(220)
        self.secret_input.setEchoMode(QLineEdit.Password)
        self.secret_input.setPlaceholderText("Only needed when signing in again")
        self.environment_label = QLabel(
            str(self._settings.get("environment_label") or "Focale Cloud")
        )
        account_form.addRow("Username", self.username_input)
        account_form.addRow("Password", self.secret_input)
        account_form.addRow("Environment", self.environment_label)

        account_buttons = QHBoxLayout()
        login_button = QPushButton("Sign In")
        login_button.clicked.connect(self._login)
        refresh_button = QPushButton("Refresh State")
        refresh_button.clicked.connect(self._refresh_status)
        account_buttons.addWidget(login_button)
        account_buttons.addWidget(refresh_button)
        account_buttons.addStretch(1)
        account_form.addRow("", account_buttons)

        account_note = QLabel(
            "Focale keeps your session locally, so you usually only need to sign in once."
        )
        account_note.setWordWrap(True)
        account_form.addRow("", account_note)
        layout.addWidget(account_box, 0, 0)

        # --- Hub ---
        hub_box = QGroupBox("Hub")
        hub_form = QFormLayout(hub_box)
        hub_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        hub_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        hub_note = QLabel(
            "The Hub connection follows your Focale environment automatically."
        )
        hub_note.setWordWrap(True)
        hub_form.addRow("", hub_note)

        hub_buttons = QHBoxLayout()
        doctor_button = QPushButton("Run Doctor")
        doctor_button.clicked.connect(self._doctor)
        connect_button = QPushButton("Connect Once")
        connect_button.clicked.connect(self._connect_once)
        hub_buttons.addWidget(doctor_button)
        hub_buttons.addWidget(connect_button)
        hub_buttons.addStretch(1)
        hub_form.addRow("", hub_buttons)
        layout.addWidget(hub_box, 1, 0)

        # --- State table (right column, spans both rows) ---
        status_box = QGroupBox("State")
        status_layout = QVBoxLayout(status_box)
        self.state_table = QTableWidget(0, 2)
        self.state_table.setHorizontalHeaderLabels(["Property", "Value"])
        self.state_table.verticalHeader().setVisible(False)
        self.state_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.state_table.setSelectionMode(QTableWidget.NoSelection)
        self.state_table.horizontalHeader().setStretchLastSection(True)
        self.state_table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft)
        self.state_table.setMinimumWidth(300)
        status_layout.addWidget(self.state_table)
        layout.addWidget(status_box, 0, 1, 2, 1)

        layout.setRowStretch(2, 1)

        return tab

    def _build_alpaca_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        alpaca_box = QGroupBox("Local Alpaca Servers")
        alpaca_form = QFormLayout(alpaca_box)
        alpaca_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.local_alpaca_summary = QLabel("No local scan yet.")
        self.local_alpaca_summary.setWordWrap(True)
        alpaca_form.addRow("", self.local_alpaca_summary)

        alpaca_buttons = QHBoxLayout()
        discover_alpaca_button = QPushButton("Check Local Alpaca Servers")
        discover_alpaca_button.clicked.connect(self._discover_local_alpaca)
        register_alpaca_button = QPushButton("Register Server To Focale")
        register_alpaca_button.clicked.connect(self._register_local_alpaca)
        alpaca_buttons.addWidget(discover_alpaca_button)
        alpaca_buttons.addWidget(register_alpaca_button)
        alpaca_buttons.addStretch(1)
        alpaca_form.addRow("", self._wrap_layout(alpaca_buttons))
        layout.addWidget(alpaca_box)
        layout.addStretch(1)

        return tab

    def _build_platesolver_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        top_row = QHBoxLayout()
        top_row.setSpacing(12)

        # --- Equipment + Target (left) ---
        equip_box = QGroupBox("Equipment & Target")
        equip_form = QFormLayout(equip_box)
        equip_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        equip_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        cam_row = QHBoxLayout()
        self.camera_address_input = QLineEdit()
        self.camera_address_input.setPlaceholderText("http://localhost:11111")
        self.camera_number_input = QLineEdit("0")
        self.camera_number_input.setFixedWidth(36)
        cam_row.addWidget(self.camera_address_input, 1)
        cam_row.addWidget(QLabel("#"))
        cam_row.addWidget(self.camera_number_input)
        equip_form.addRow("Camera", self._wrap_layout(cam_row))

        tel_row = QHBoxLayout()
        self.telescope_address_input = QLineEdit()
        self.telescope_address_input.setPlaceholderText("http://localhost:11111")
        self.telescope_number_input = QLineEdit("0")
        self.telescope_number_input.setFixedWidth(36)
        tel_row.addWidget(self.telescope_address_input, 1)
        tel_row.addWidget(QLabel("#"))
        tel_row.addWidget(self.telescope_number_input)
        equip_form.addRow("Telescope", self._wrap_layout(tel_row))

        equip_form.addRow(QLabel(""))  # spacer

        self.target_ra_input = QLineEdit()
        self.target_ra_input.setPlaceholderText("decimal hours")
        equip_form.addRow("Target RA (h)", self.target_ra_input)

        self.target_dec_input = QLineEdit()
        self.target_dec_input.setPlaceholderText("degrees")
        equip_form.addRow("Target Dec (°)", self.target_dec_input)

        top_row.addWidget(equip_box, 1)

        # --- Centering parameters (right) ---
        centering_box = QGroupBox("Centering Parameters")
        centering_form = QFormLayout(centering_box)
        centering_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        centering_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.centering_duration_input = QLineEdit("5")
        centering_form.addRow("Exposure (s)", self.centering_duration_input)

        self.centering_max_iter_input = QLineEdit("10")
        centering_form.addRow("Max iterations", self.centering_max_iter_input)

        self.centering_min_peaks_input = QLineEdit("20")
        centering_form.addRow("Min peaks", self.centering_min_peaks_input)

        self.centering_success_input = QLineEdit("10")
        centering_form.addRow("Success threshold (\")", self.centering_success_input)

        self.centering_failure_input = QLineEdit("300")
        centering_form.addRow("Failure threshold (\")", self.centering_failure_input)

        self.centering_max_dur_adj_input = QLineEdit("2")
        centering_form.addRow("Max exposure doublings", self.centering_max_dur_adj_input)

        top_row.addWidget(centering_box, 1)
        layout.addLayout(top_row)

        # --- Local solver index files (full width) ---
        solver_box = QGroupBox("Local Solver Index Files")
        solver_form = QFormLayout(solver_box)
        solver_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        solver_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.solver_cache_dir_input = QLineEdit()
        cache_row = QHBoxLayout()
        cache_row.addWidget(self.solver_cache_dir_input, 1)
        browse_cache_button = QPushButton("Browse")
        browse_cache_button.clicked.connect(self._browse_cache_dir)
        cache_row.addWidget(browse_cache_button)
        solver_form.addRow("Cache directory", self._wrap_layout(cache_row))

        self.solver_scales_input = QLineEdit("6")
        solver_form.addRow("Scales (0–19)", self.solver_scales_input)
        layout.addWidget(solver_box)

        # --- Buttons ---
        button_row = QHBoxLayout()
        check_solver_button = QPushButton("Check Solver")
        check_solver_button.clicked.connect(self._platesolver_status)
        center_button = QPushButton("Center")
        center_button.clicked.connect(self._center_on_coordinates)
        button_row.addWidget(check_solver_button)
        button_row.addWidget(center_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self.platesolver_output = QPlainTextEdit()
        self.platesolver_output.setReadOnly(True)
        self.platesolver_output.setPlaceholderText("Centering progress and results will appear here.")
        layout.addWidget(self.platesolver_output, 1)

        return tab

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _wrap_layout(self, layout) -> QWidget:
        widget = QWidget()
        widget.setLayout(layout)
        return widget

    def _start_action(
        self,
        label: str,
        fn: WorkerFunc,
        *,
        on_result: Callable[[Any], None] | None = None,
    ) -> None:
        worker = FunctionWorker(fn)
        self._active_workers[id(worker.signals)] = (worker, label, on_result)
        worker.signals.log.connect(self._append_log)
        worker.signals.error.connect(self._handle_worker_error)
        worker.signals.result.connect(self._handle_worker_result)
        worker.signals.finished.connect(self._finish_action)
        self._busy_count += 1
        self.statusBar().showMessage(f"{label} in progress...")
        self.thread_pool.start(worker)

    def _worker_context(
        self,
    ) -> tuple[FunctionWorker, str, Callable[[Any], None] | None] | None:
        sender = self.sender()
        if sender is None:
            return None
        return self._active_workers.get(id(sender))

    @Slot(object)
    def _handle_worker_result(self, payload: Any) -> None:
        context = self._worker_context()
        if context is None:
            return
        _worker, label, on_result = context
        self._append_log(f"{label} completed.")
        self._append_log(self._format_payload(payload))
        if on_result is not None:
            on_result(payload)

    @Slot(str)
    def _handle_worker_error(self, message: str) -> None:
        context = self._worker_context()
        if context is None:
            return
        _worker, label, _on_result = context
        self._handle_error(label, message)

    def _handle_error(self, label: str, message: str) -> None:
        self._append_log(f"{label} failed.")
        self._append_log(message)
        self._refresh_status_summary()
        QMessageBox.critical(self, "Focale", f"{label} failed.\n\n{message}")

    @Slot()
    def _finish_action(self) -> None:
        sender = self.sender()
        if sender is not None:
            self._active_workers.pop(id(sender), None)
        self._busy_count = max(0, self._busy_count - 1)
        if self._busy_count == 0:
            self.statusBar().showMessage("Ready")

    @Slot(str)
    def _append_log(self, message: str) -> None:
        self.log_output.appendPlainText(message)

    def _format_payload(self, payload: Any) -> str:
        return json.dumps(payload, indent=2, sort_keys=True, default=str)

    def _api_server(self) -> str | None:
        value = str(self._settings.get("api_server") or "").strip()
        return value or None

    def _hub_url(self) -> str | None:
        value = str(self._settings.get("hub_url") or "").strip()
        return value or None

    # ------------------------------------------------------------------ #
    # Account tab actions                                                  #
    # ------------------------------------------------------------------ #

    def _login(self) -> None:
        api_server = self._api_server()
        username = self.username_input.text().strip()
        secret = self.secret_input.text()
        if not username or not secret:
            QMessageBox.warning(self, "Focale", "Username and password are required.")
            return

        self._start_action(
            "Sign in",
            lambda _log: services.login(
                api_server=api_server,
                username=username,
                secret=secret,
            ),
            on_result=self._after_login,
        )

    def _after_login(self, _payload: Any) -> None:
        self.secret_input.clear()
        self._settings = services.user_settings()
        self.environment_label.setText(
            str(self._settings.get("environment_label") or "Focale Cloud")
        )
        self._refresh_status_summary()

    def _refresh_status(self) -> None:
        api_server = self._api_server()
        self._start_action(
            "Refresh state",
            lambda _log: services.status(
                api_server=api_server,
            ),
            on_result=self._set_status_summary,
        )

    def _refresh_status_summary(self) -> None:
        try:
            payload = services.status(
                api_server=self._api_server(),
            )
        except Exception as exc:
            self.state_table.setRowCount(1)
            label_item = QTableWidgetItem("Status")
            value_item = QTableWidgetItem(str(exc))
            label_item.setFlags(Qt.ItemIsEnabled)
            value_item.setFlags(Qt.ItemIsEnabled)
            self.state_table.setItem(0, 0, label_item)
            self.state_table.setItem(0, 1, value_item)
        else:
            self._set_status_summary(payload)

    def _set_status_summary(self, payload: Any) -> None:
        rows = [
            ("Signed in", "Yes" if payload.get("logged_in") else "No"),
            ("Username", str(payload.get("username") or "Not signed in")),
            ("Environment", str(payload.get("environment_label") or "Focale Cloud")),
            ("Stored installations", str(len(payload.get("installations") or {}))),
            ("Known Alpaca servers", str(payload.get("known_alpaca_servers") or 0)),
            ("Focale version", str(payload.get("focale_version") or __version__)),
        ]
        auth_error = payload.get("auth_error")
        if auth_error:
            rows.insert(1, ("Session", str(auth_error)))
        self.state_table.setRowCount(len(rows))
        for row_index, (label, value) in enumerate(rows):
            label_item = QTableWidgetItem(label)
            value_item = QTableWidgetItem(value)
            label_item.setFlags(Qt.ItemIsEnabled)
            value_item.setFlags(Qt.ItemIsEnabled)
            self.state_table.setItem(row_index, 0, label_item)
            self.state_table.setItem(row_index, 1, value_item)

    def _doctor(self) -> None:
        api_server = self._api_server()
        hub_url = self._hub_url()
        self._start_action(
            "Doctor",
            lambda log: services.doctor(
                api_server=api_server,
                hub_url=hub_url,
                organisation=None,
                workspace_id=None,
                force_refresh=False,
                re_enroll=False,
                echo=log,
            ),
        )

    def _connect_once(self) -> None:
        api_server = self._api_server()
        hub_url = self._hub_url()
        self._start_action(
            "Connect once",
            lambda log: services.connect_once(
                api_server=api_server,
                hub_url=hub_url,
                organisation=None,
                workspace_id=None,
                re_enroll=False,
                discover_alpaca=False,
                echo=log,
            ),
        )

    # ------------------------------------------------------------------ #
    # Alpaca tab actions                                                   #
    # ------------------------------------------------------------------ #

    def _discover_local_alpaca(self) -> None:
        self._start_action(
            "Check local Alpaca servers",
            lambda _log: services.discover_local_alpaca(),
            on_result=self._set_local_alpaca_summary,
        )

    def _register_local_alpaca(self) -> None:
        api_server = self._api_server()
        self._start_action(
            "Register local Alpaca servers",
            lambda log: services.register_local_alpaca(
                api_server=api_server,
                echo=log,
            ),
            on_result=self._after_register_local_alpaca,
        )

    def _after_register_local_alpaca(self, payload: Any) -> None:
        self._set_local_alpaca_summary(payload)
        self._refresh_status_summary()

    def _set_local_alpaca_summary(self, payload: Any) -> None:
        count = int(payload.get("count", payload.get("discovered", 0)) or 0)
        if count == 0:
            self.local_alpaca_summary.setText("No local Alpaca servers found.")
            return

        servers = payload.get("servers") or []
        names = ", ".join(
            str(server.get("name") or server.get("address") or "Unknown")
            for server in servers[:3]
            if isinstance(server, dict)
        )
        extra = ""
        if len(servers) > 3:
            extra = f" and {len(servers) - 3} more"

        registration = ""
        if "registered" in payload or "already_registered" in payload:
            registration = (
                f" Registered: {payload.get('registered', 0)},"
                f" already registered: {payload.get('already_registered', 0)}."
            )

        devices = ""
        if "devices_registered" in payload or "devices_already_registered" in payload:
            devices = (
                f" Devices: {payload.get('devices_registered', 0)} new,"
                f" {payload.get('devices_already_registered', 0)} already known."
            )

        resources = ""
        if (
            "sites_created" in payload
            or "telescopes_created" in payload
            or "equipments_created" in payload
        ):
            resources = (
                f" Resources: {payload.get('sites_created', 0)} site(s),"
                f" {payload.get('telescopes_created', 0)} telescope(s),"
                f" {payload.get('equipments_created', 0)} equipment item(s) created."
            )

        self.local_alpaca_summary.setText(
            f"Found {count} local server(s): {names}{extra}.{registration}{devices}{resources}"
        )

    # ------------------------------------------------------------------ #
    # Plate Solver tab actions                                             #
    # ------------------------------------------------------------------ #

    def _platesolver_status(self) -> None:
        cache_dir = self._clean(self.solver_cache_dir_input)
        scales = self.solver_scales_input.text().strip() or "6"
        self._start_action(
            "Check solver",
            lambda _log: services.platesolver_status(
                cache_dir=cache_dir,
                scales=scales,
            ),
            on_result=lambda payload: self.platesolver_output.setPlainText(
                self._format_payload(payload)
            ),
        )

    def _center_on_coordinates(self) -> None:
        try:
            camera_address = self.camera_address_input.text().strip()
            camera_number = self._required_int(self.camera_number_input, "Camera device number")
            telescope_address = self.telescope_address_input.text().strip()
            telescope_number = self._required_int(self.telescope_number_input, "Telescope device number")
            target_ra = self._required_float(self.target_ra_input, "Target RA")
            target_dec = self._required_float(self.target_dec_input, "Target Dec")
            duration = self._required_float(self.centering_duration_input, "Exposure")
            max_iter = self._required_int(self.centering_max_iter_input, "Max iterations")
            min_peaks = self._required_int(self.centering_min_peaks_input, "Min peaks")
            success_thr = self._required_float(self.centering_success_input, "Success threshold")
            failure_thr = self._required_float(self.centering_failure_input, "Failure threshold")
            max_dur_adj = self._required_int(self.centering_max_dur_adj_input, "Max exposure doublings")
        except FocaleError as exc:
            QMessageBox.warning(self, "Focale", str(exc))
            return

        if not camera_address:
            QMessageBox.warning(self, "Focale", "Camera Alpaca address is required.")
            return
        if not telescope_address:
            QMessageBox.warning(self, "Focale", "Telescope Alpaca address is required.")
            return

        cache_dir = self._clean(self.solver_cache_dir_input)
        scales = self.solver_scales_input.text().strip() or "6"
        self.platesolver_output.clear()

        self._start_action(
            "Centering",
            lambda log: services.center_on_coordinates(
                camera_address=camera_address,
                camera_number=camera_number,
                telescope_address=telescope_address,
                telescope_number=telescope_number,
                target_ra_hours=target_ra,
                target_dec_deg=target_dec,
                cache_dir=cache_dir,
                scales=scales,
                duration=duration,
                max_iterations=max_iter,
                min_peaks=min_peaks,
                success_threshold=success_thr,
                failure_threshold=failure_thr,
                max_duration_adjustments=max_dur_adj,
                echo=log,
            ),
            on_result=lambda payload: self.platesolver_output.appendPlainText(
                self._format_payload(payload)
            ),
        )

    def _browse_cache_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select cache directory")
        if path:
            self.solver_cache_dir_input.setText(path)

    # ------------------------------------------------------------------ #
    # Input helpers                                                        #
    # ------------------------------------------------------------------ #

    def _clean(self, line_edit: QLineEdit) -> str | None:
        value = line_edit.text().strip()
        return value or None

    def _optional_float(self, line_edit: QLineEdit) -> float | None:
        value = line_edit.text().strip()
        if not value:
            return None
        try:
            return float(value)
        except ValueError as exc:
            raise FocaleError(f"Invalid number: '{value}'.") from exc

    def _required_float(self, line_edit: QLineEdit, label: str) -> float:
        value = line_edit.text().strip()
        try:
            return float(value)
        except ValueError as exc:
            raise FocaleError(f"{label}: '{value}' is not a valid number.") from exc

    def _required_int(self, line_edit: QLineEdit, label: str) -> int:
        value = line_edit.text().strip()
        try:
            return int(value)
        except ValueError as exc:
            raise FocaleError(f"{label}: '{value}' is not a valid integer.") from exc


def main() -> int:
    app = QApplication(sys.argv)
    services.ensure_environment()
    window = FocaleWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
