from __future__ import annotations

import json
import sys
import traceback
from functools import partial
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from . import services
from .exceptions import FocaleError

WorkerFunc = Callable[[Callable[[str], None]], object]


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    log = Signal(str)
    finished = Signal()


class FunctionWorker(QRunnable):
    def __init__(self, fn: WorkerFunc) -> None:
        super().__init__()
        self.fn = fn
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = self.fn(self.signals.log.emit)
        except Exception:
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

        self.setWindowTitle(f"Focale {__version__}")
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
            "Desktop controls for Arcsecond session setup, Hub diagnostics, and local plate solving."
        )
        subheading.setWordWrap(True)
        layout.addWidget(subheading)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_arcsecond_tab(), "Arcsecond")
        self.tabs.addTab(self._build_platesolver_tab(), "Plate Solver")
        layout.addWidget(self.tabs, 1)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setPlaceholderText("Action logs and JSON results will appear here.")
        self.log_output.setMinimumHeight(220)
        layout.addWidget(self.log_output)

        self._refresh_status_summary()

    def _build_arcsecond_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(12)

        session_box = QGroupBox("Session")
        session_form = QFormLayout(session_box)
        self.api_name_input = QLineEdit("cloud")
        self.api_server_input = QLineEdit()
        self.username_input = QLineEdit()
        self.auth_mode_input = QComboBox()
        self.auth_mode_input.addItems(["password", "access-key"])
        self.secret_input = QLineEdit()
        self.secret_input.setEchoMode(QLineEdit.Password)
        session_form.addRow("API profile", self.api_name_input)
        session_form.addRow("API server", self.api_server_input)
        session_form.addRow("Username", self.username_input)
        session_form.addRow("Auth mode", self.auth_mode_input)
        session_form.addRow("Password / Access Key", self.secret_input)

        session_buttons = QHBoxLayout()
        login_button = QPushButton("Login")
        login_button.clicked.connect(self._login)
        refresh_button = QPushButton("Refresh Status")
        refresh_button.clicked.connect(self._refresh_status)
        session_buttons.addWidget(login_button)
        session_buttons.addWidget(refresh_button)
        session_buttons.addStretch(1)
        session_form.addRow("", session_buttons)
        layout.addWidget(session_box)

        context_box = QGroupBox("Context")
        context_form = QFormLayout(context_box)
        self.context_target_input = QLineEdit()
        self.context_target_input.setPlaceholderText("personal or organisation subdomain")
        context_form.addRow("Default context", self.context_target_input)

        context_buttons = QHBoxLayout()
        list_contexts_button = QPushButton("List Contexts")
        list_contexts_button.clicked.connect(self._list_contexts)
        set_context_button = QPushButton("Use Context")
        set_context_button.clicked.connect(self._set_context)
        context_buttons.addWidget(list_contexts_button)
        context_buttons.addWidget(set_context_button)
        context_buttons.addStretch(1)
        context_form.addRow("", context_buttons)
        layout.addWidget(context_box)

        hub_box = QGroupBox("Hub")
        hub_form = QFormLayout(hub_box)
        self.hub_url_input = QLineEdit()
        self.hub_url_input.setPlaceholderText("wss://hub.arcsecond.io/ws/agent")
        self.organisation_input = QLineEdit()
        self.organisation_input.setPlaceholderText("Optional organisation override")
        self.workspace_id_input = QLineEdit()
        self.workspace_id_input.setPlaceholderText("Optional workspace override")
        self.force_refresh_checkbox = QCheckBox("Force JWT refresh during doctor")
        self.reenroll_checkbox = QCheckBox("Force re-enrollment")
        self.discover_alpaca_checkbox = QCheckBox("Discover and register Alpaca servers")
        self.discover_alpaca_checkbox.setChecked(True)
        hub_form.addRow("Hub URL", self.hub_url_input)
        hub_form.addRow("Organisation", self.organisation_input)
        hub_form.addRow("Workspace ID", self.workspace_id_input)
        hub_form.addRow("", self.force_refresh_checkbox)
        hub_form.addRow("", self.reenroll_checkbox)
        hub_form.addRow("", self.discover_alpaca_checkbox)

        hub_buttons = QHBoxLayout()
        doctor_button = QPushButton("Run Doctor")
        doctor_button.clicked.connect(self._doctor)
        connect_button = QPushButton("Connect Once")
        connect_button.clicked.connect(self._connect_once)
        hub_buttons.addWidget(doctor_button)
        hub_buttons.addWidget(connect_button)
        hub_buttons.addStretch(1)
        hub_form.addRow("", hub_buttons)
        layout.addWidget(hub_box)

        self.status_summary = QPlainTextEdit()
        self.status_summary.setReadOnly(True)
        self.status_summary.setPlaceholderText("Current Focale state summary.")
        self.status_summary.setMinimumHeight(180)
        layout.addWidget(self.status_summary)

        return tab

    def _build_platesolver_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(12)

        solver_box = QGroupBox("Solver")
        solver_form = QFormLayout(solver_box)
        self.solver_service_url_input = QLineEdit()
        self.solver_cache_dir_input = QLineEdit()
        self.solver_scales_input = QLineEdit("6")

        cache_row = QHBoxLayout()
        cache_row.addWidget(self.solver_cache_dir_input, 1)
        browse_cache_button = QPushButton("Browse")
        browse_cache_button.clicked.connect(self._browse_cache_dir)
        cache_row.addWidget(browse_cache_button)

        solver_form.addRow("Remote service URL", self.solver_service_url_input)
        solver_form.addRow("Cache directory", self._wrap_layout(cache_row))
        solver_form.addRow("Scales", self.solver_scales_input)
        layout.addWidget(solver_box)

        solve_box = QGroupBox("Solve")
        solve_grid = QGridLayout(solve_box)
        self.peaks_file_input = QLineEdit()
        browse_peaks_button = QPushButton("Browse")
        browse_peaks_button.clicked.connect(self._browse_peaks_file)
        self.ra_input = QLineEdit()
        self.dec_input = QLineEdit()
        self.radius_input = QLineEdit()
        self.lower_scale_input = QLineEdit()
        self.upper_scale_input = QLineEdit()

        solve_grid.addWidget(QLabel("Peaks file"), 0, 0)
        solve_grid.addWidget(self.peaks_file_input, 0, 1)
        solve_grid.addWidget(browse_peaks_button, 0, 2)
        solve_grid.addWidget(QLabel("RA (deg)"), 1, 0)
        solve_grid.addWidget(self.ra_input, 1, 1)
        solve_grid.addWidget(QLabel("Dec (deg)"), 1, 2)
        solve_grid.addWidget(self.dec_input, 1, 3)
        solve_grid.addWidget(QLabel("Radius (deg)"), 2, 0)
        solve_grid.addWidget(self.radius_input, 2, 1)
        solve_grid.addWidget(QLabel("Lower arcsec/px"), 2, 2)
        solve_grid.addWidget(self.lower_scale_input, 2, 3)
        solve_grid.addWidget(QLabel("Upper arcsec/px"), 3, 0)
        solve_grid.addWidget(self.upper_scale_input, 3, 1)

        button_row = QHBoxLayout()
        status_button = QPushButton("Solver Status")
        status_button.clicked.connect(self._platesolver_status)
        solve_button = QPushButton("Solve")
        solve_button.clicked.connect(self._platesolver_solve)
        button_row.addWidget(status_button)
        button_row.addWidget(solve_button)
        button_row.addStretch(1)

        layout.addWidget(solve_box)
        layout.addLayout(button_row)

        self.platesolver_output = QPlainTextEdit()
        self.platesolver_output.setReadOnly(True)
        self.platesolver_output.setPlaceholderText("Plate solve results will appear here.")
        self.platesolver_output.setMinimumHeight(220)
        layout.addWidget(self.platesolver_output)

        return tab

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
        worker.signals.log.connect(lambda message: self._append_log(message))
        worker.signals.error.connect(
            lambda message: self._handle_error(label, message)
        )
        worker.signals.result.connect(
            partial(self._handle_result, label, on_result)
        )
        worker.signals.finished.connect(self._finish_action)
        self._busy_count += 1
        self.statusBar().showMessage(f"{label} in progress...")
        self.thread_pool.start(worker)

    def _handle_result(
        self,
        label: str,
        on_result: Callable[[Any], None] | None,
        payload: Any,
    ) -> None:
        self._append_log(f"{label} completed.")
        self._append_log(self._format_payload(payload))
        if on_result is not None:
            on_result(payload)

    def _handle_error(self, label: str, message: str) -> None:
        self._append_log(f"{label} failed.")
        self._append_log(message)
        QMessageBox.critical(self, "Focale", f"{label} failed.\n\n{message}")

    def _finish_action(self) -> None:
        self._busy_count = max(0, self._busy_count - 1)
        if self._busy_count == 0:
            self.statusBar().showMessage("Ready")

    def _append_log(self, message: str) -> None:
        self.log_output.appendPlainText(message)

    def _format_payload(self, payload: Any) -> str:
        return json.dumps(payload, indent=2, sort_keys=True, default=str)

    def _api_name(self) -> str:
        return self.api_name_input.text().strip() or "cloud"

    def _api_server(self) -> str | None:
        value = self.api_server_input.text().strip()
        return value or None

    def _login(self) -> None:
        api_name = self._api_name()
        api_server = self._api_server()
        username = self.username_input.text().strip()
        secret = self.secret_input.text()
        auth_mode = self.auth_mode_input.currentText()
        if not username or not secret:
            QMessageBox.warning(self, "Focale", "Username and password/access key are required.")
            return

        self._start_action(
            "Login",
            lambda _log: services.login(
                api_name=api_name,
                api_server=api_server,
                username=username,
                secret=secret,
                auth_mode=auth_mode,
            ),
            on_result=lambda _payload: self._refresh_status_summary(),
        )

    def _refresh_status(self) -> None:
        api_name = self._api_name()
        api_server = self._api_server()
        self._start_action(
            "Status refresh",
            lambda _log: services.status(
                api_name=api_name,
                api_server=api_server,
            ),
            on_result=self._set_status_summary,
        )

    def _refresh_status_summary(self) -> None:
        try:
            payload = services.status(
                api_name=self._api_name(),
                api_server=self._api_server(),
            )
        except Exception as exc:
            self.status_summary.setPlainText(str(exc))
        else:
            self._set_status_summary(payload)

    def _set_status_summary(self, payload: Any) -> None:
        self.status_summary.setPlainText(self._format_payload(payload))

    def _list_contexts(self) -> None:
        api_name = self._api_name()
        api_server = self._api_server()
        self._start_action(
            "List contexts",
            lambda _log: services.list_contexts(
                api_name=api_name,
                api_server=api_server,
            ),
        )

    def _set_context(self) -> None:
        api_name = self._api_name()
        api_server = self._api_server()
        target = self.context_target_input.text().strip()
        if not target:
            QMessageBox.warning(self, "Focale", "Enter a context target first.")
            return

        self._start_action(
            "Set context",
            lambda _log: services.set_default_context(
                api_name=api_name,
                api_server=api_server,
                target=target,
            ),
            on_result=lambda _payload: self._refresh_status_summary(),
        )

    def _doctor(self) -> None:
        api_name = self._api_name()
        api_server = self._api_server()
        hub_url = self._clean(self.hub_url_input)
        organisation = self._clean(self.organisation_input)
        workspace_id = self._clean(self.workspace_id_input)
        force_refresh = self.force_refresh_checkbox.isChecked()
        re_enroll = self.reenroll_checkbox.isChecked()
        self._start_action(
            "Doctor",
            lambda log: services.doctor(
                api_name=api_name,
                api_server=api_server,
                hub_url=hub_url,
                organisation=organisation,
                workspace_id=workspace_id,
                force_refresh=force_refresh,
                re_enroll=re_enroll,
                echo=log,
            ),
        )

    def _connect_once(self) -> None:
        api_name = self._api_name()
        api_server = self._api_server()
        hub_url = self._clean(self.hub_url_input)
        organisation = self._clean(self.organisation_input)
        workspace_id = self._clean(self.workspace_id_input)
        re_enroll = self.reenroll_checkbox.isChecked()
        discover_alpaca = self.discover_alpaca_checkbox.isChecked()
        self._start_action(
            "Connect once",
            lambda log: services.connect_once(
                api_name=api_name,
                api_server=api_server,
                hub_url=hub_url,
                organisation=organisation,
                workspace_id=workspace_id,
                re_enroll=re_enroll,
                discover_alpaca=discover_alpaca,
                echo=log,
            ),
        )

    def _platesolver_status(self) -> None:
        service_url = self._clean(self.solver_service_url_input)
        cache_dir = self._clean(self.solver_cache_dir_input)
        scales = self.solver_scales_input.text().strip() or "6"
        self._start_action(
            "Plate solver status",
            lambda _log: services.platesolver_status(
                service_url=service_url,
                cache_dir=cache_dir,
                scales=scales,
            ),
            on_result=lambda payload: self.platesolver_output.setPlainText(
                self._format_payload(payload)
            ),
        )

    def _platesolver_solve(self) -> None:
        peaks_path = self.peaks_file_input.text().strip()
        if not peaks_path:
            QMessageBox.warning(self, "Focale", "Select a peaks JSON file first.")
            return

        service_url = self._clean(self.solver_service_url_input)
        cache_dir = self._clean(self.solver_cache_dir_input)
        scales = self.solver_scales_input.text().strip() or "6"
        ra_deg = self._optional_float(self.ra_input)
        dec_deg = self._optional_float(self.dec_input)
        radius_deg = self._optional_float(self.radius_input)
        lower_arcsec_per_pixel = self._optional_float(self.lower_scale_input)
        upper_arcsec_per_pixel = self._optional_float(self.upper_scale_input)

        self._start_action(
            "Plate solve",
            lambda _log: services.platesolver_solve(
                peaks_file=Path(peaks_path),
                service_url=service_url,
                cache_dir=cache_dir,
                scales=scales,
                ra_deg=ra_deg,
                dec_deg=dec_deg,
                radius_deg=radius_deg,
                lower_arcsec_per_pixel=lower_arcsec_per_pixel,
                upper_arcsec_per_pixel=upper_arcsec_per_pixel,
            ),
            on_result=lambda payload: self.platesolver_output.setPlainText(
                self._format_payload(payload)
            ),
        )

    def _browse_cache_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select cache directory")
        if path:
            self.solver_cache_dir_input.setText(path)

    def _browse_peaks_file(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self,
            "Select peaks JSON file",
            filter="JSON files (*.json);;All files (*)",
        )
        if path:
            self.peaks_file_input.setText(path)

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
            raise FocaleError(f"Invalid numeric value `{value}`.") from exc


def main() -> int:
    app = QApplication(sys.argv)
    window = FocaleWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
