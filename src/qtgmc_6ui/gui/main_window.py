"""Main window: banner, queue | tabs (analysis, settings, viewing copies, preview), run panel + log."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent, QKeySequence
from PySide6.QtWidgets import QMainWindow, QMessageBox, QSplitter, QTabWidget, QVBoxLayout, QWidget

from .. import __version__, paths
from ..models import FieldOrderChoice, JobSpec, ViewCopySpec
from . import ui_state
from .analysis_panel import AnalysisPanel
from .backend import CliCall, CliResult, backend_display
from .catalog import Catalog
from .controller import QueueController
from .doctor_dialog import Banner, DoctorDialog, banner_for
from .preview_widget import PreviewWidget, prune_preview_cache
from .queue_model import JobItem, JobStatus, QueueModel, expand_paths
from .queue_panel import QueuePanel
from .run_model import RunState
from .run_panel import RunPanel
from .settings_panel import SettingsPanel
from .view_section import ViewSection

TAB_ANALYSIS, TAB_SETTINGS, TAB_VIEWS, TAB_PREVIEW = range(4)


class MainWindow(QMainWindow):
    def __init__(self, run_startup_checks: bool = True) -> None:
        super().__init__()
        self.setWindowTitle(paths.DISPLAY_NAME)
        self.setObjectName("mainWindow")
        self.setAcceptDrops(True)
        self.model = QueueModel(self)
        self.catalog = Catalog(self)
        self.controller = QueueController(self.model, self.build_spec, self)
        self.doctor_result: CliResult | None = None
        self._doctor_call: CliCall | None = None
        self._closing = False
        self._last_doctor_line = ""

        self.banner = Banner()
        self.banner.actionClicked.connect(self.show_doctor)
        self.queue_panel = QueuePanel(self.model)
        self.analysis_panel = AnalysisPanel()
        self.settings_panel = SettingsPanel()
        self.view_section = ViewSection(self.catalog)
        self.preview = PreviewWidget()
        self.run_panel = RunPanel()

        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        self.tabs.addTab(self.analysis_panel, "Analysis")
        self.tabs.addTab(self.settings_panel, "Master settings")
        self.tabs.addTab(self.view_section, "Viewing copies")
        self.tabs.addTab(self.preview, "Preview")
        self.tabs.setDocumentMode(True)

        self.h_split = QSplitter(Qt.Orientation.Horizontal)
        self.h_split.addWidget(self.queue_panel)
        self.h_split.addWidget(self.tabs)
        self.h_split.setStretchFactor(0, 2)
        self.h_split.setStretchFactor(1, 5)
        self.h_split.setChildrenCollapsible(False)
        self.v_split = QSplitter(Qt.Orientation.Vertical)
        self.v_split.addWidget(self.h_split)
        self.v_split.addWidget(self.run_panel)
        self.v_split.setStretchFactor(0, 3)
        self.v_split.setStretchFactor(1, 1)
        self.v_split.setChildrenCollapsible(False)

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.addWidget(self.banner)
        lay.addWidget(self.v_split, 1)
        self.setCentralWidget(central)
        self._build_menus()
        self._wire()
        self.statusBar().showMessage(f"{paths.DISPLAY_NAME} {__version__} — backend: {backend_display()}")
        self._restore_layout()
        self.preview.set_master(self.settings_panel.master_settings())
        self._update_start()
        prune_preview_cache()
        if run_startup_checks:
            self.run_startup_checks()

    # ------------------------------------------------------------------ setup
    def _build_menus(self) -> None:
        mfile = self.menuBar().addMenu("&File")
        self.act_add = QAction("Add files…", self, shortcut=QKeySequence.StandardKey.Open)
        self.act_add.triggered.connect(self.queue_panel.choose_files)
        act_folder = QAction("Add folder…", self)
        act_folder.triggered.connect(self.queue_panel.choose_folder)
        act_quit = QAction("Quit", self, shortcut=QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)
        for a in (self.act_add, act_folder):
            mfile.addAction(a)
        mfile.addSeparator()
        mfile.addAction(act_quit)
        mq = self.menuBar().addMenu("&Queue")
        self.act_start = QAction("Start", self, shortcut=QKeySequence("Ctrl+Return"))
        self.act_start.triggered.connect(self.start_queue)
        self.act_cancel = QAction("Cancel", self, shortcut=QKeySequence("Ctrl+."))
        self.act_cancel.triggered.connect(self.controller.cancel)
        self.act_cancel.setEnabled(False)
        mq.addAction(self.act_start)
        mq.addAction(self.act_cancel)
        mh = self.menuBar().addMenu("&Help")
        act_doctor = QAction("Run doctor…", self)
        act_doctor.triggered.connect(self.show_doctor)
        act_about = QAction("About", self)
        act_about.triggered.connect(self._about)
        mh.addAction(act_doctor)
        mh.addAction(act_about)

    def _wire(self) -> None:
        qp, c = self.queue_panel, self.controller
        qp.addRequested.connect(self.add_paths)
        qp.removeRequested.connect(c.remove)
        qp.reanalyzeRequested.connect(c.reanalyze)
        qp.currentChanged.connect(self._on_current)
        self.analysis_panel.reanalyzeRequested.connect(c.reanalyze)
        self.analysis_panel.decisionChanged.connect(self._on_decision)
        self.analysis_panel.previewRequested.connect(self._show_fields_preview)
        self.settings_panel.changed.connect(lambda: self.preview.set_master(self.settings_panel.master_settings()))
        self.settings_panel.changed.connect(self._update_start)
        self.view_section.selectionChanged.connect(self._update_start)
        self.model.itemChanged.connect(self._on_item_changed)
        c.log.connect(self.run_panel.log.append)
        c.analysisDone.connect(self._on_analysis_done)
        c.runUpdated.connect(self._on_run_update)
        c.jobFinished.connect(self._on_job_finished)
        c.batchStarted.connect(lambda n: self._set_running(True))
        c.batchFinished.connect(self._on_batch_finished)
        c.idle.connect(self._on_idle)
        self.run_panel.startRequested.connect(self.start_queue)
        self.run_panel.cancelRequested.connect(c.cancel)
        self.run_panel.doctorRequested.connect(self.show_doctor)
        self.catalog.failed.connect(lambda msg: self.run_panel.log.append(f"Warning: {msg}"))

    def run_startup_checks(self) -> None:
        self._doctor_call = CliCall(["doctor", "--json", "--no-load"], self)
        self._doctor_call.finished.connect(self._on_doctor)
        self._doctor_call.start()
        self.catalog.load_profiles()

    # ------------------------------------------------------------------ doctor
    def _on_doctor(self, result: CliResult) -> None:
        quick = self.doctor_result is None
        self.doctor_result = result
        msg = banner_for(result)
        if quick and result.data is not None and (msg is None or msg[0] != "error"):
            # second pass loads every plugin (catches an Arch update that broke a plugin), no smoke run
            self._doctor_call = CliCall(["doctor", "--json", "--no-smoke"], self)
            self._doctor_call.finished.connect(self._on_doctor)
            self._doctor_call.start()
        if msg:
            self.banner.show_message(msg[0], msg[1], "Details…")
            line = "Startup check: " + msg[1].replace("<br>", "\n").replace("<b>", "").replace("</b>", "")
            if line != self._last_doctor_line:
                self.run_panel.log.append(line)
                self._last_doctor_line = line
        else:
            self.banner.hide()
            self.statusBar().showMessage("Component check passed.", 8000)

    def show_doctor(self) -> DoctorDialog:
        dlg = DoctorDialog(self)
        if self.doctor_result is not None:
            dlg.set_result(self.doctor_result)
        else:
            dlg.run_full()
        dlg.show()
        self._doctor_dialog = dlg
        return dlg

    def _about(self) -> None:
        QMessageBox.about(
            self, f"About {paths.DISPLAY_NAME}",
            f"<b>{paths.DISPLAY_NAME}</b> {__version__}<br>QTGMC deinterlacing for archival video "
            "(VapourSynth), with a Qt6 front end.<br><br>The GUI runs the <tt>qtgmc-6ui</tt> command line "
            f"for every operation:<br><tt>{backend_display()}</tt>",
        )

    # ------------------------------------------------------------------ queue
    def add_paths(self, paths_: list[str]) -> list[JobItem]:
        files = expand_paths(paths_)
        new = self.controller.add(files)
        if new and not self.queue_panel.current_item():
            self.queue_panel.select(new[0])
        skipped = len(files) - len(new)
        if skipped:
            self.statusBar().showMessage(f"{skipped} file(s) already in the queue.", 6000)
        return new

    def build_spec(self, item: JobItem) -> JobSpec:
        master = self.settings_panel.master_settings()
        if item.field_order_override:
            master.field_order = FieldOrderChoice(item.field_order_override)
        elif item.force and item.effective_field_order():
            master.field_order = FieldOrderChoice(item.effective_field_order())
        return JobSpec(
            input=item.path,
            output_dir=self.settings_panel.output_dir(),
            name_template=self.settings_panel.name_template_text(),
            master=master,
            views=[ViewCopySpec(pid) for pid in self.view_section.selected_ids()],
            force=item.force,
            keep_master=self.settings_panel.keeps_master(),
            overwrite=self.settings_panel.overwrite.isChecked(),
            analysis=item.analysis,
        )

    def start_queue(self) -> None:
        if self.controller.running:
            return
        problem = self.settings_panel.validate() or self._master_choice_problem() or self._duplicate_outputs()
        if problem:
            self.tabs.setCurrentIndex(TAB_SETTINGS)
            QMessageBox.warning(self, "Cannot start", problem)
            return
        waiting = [i for i in self.model.items if i.status == JobStatus.NEEDS_DECISION]
        pending = [i for i in self.model.items if i.status in (JobStatus.WAITING, JobStatus.ANALYZING)]
        n = self.controller.start()
        notes = []
        if waiting:
            notes.append(f"{len(waiting)} file(s) need your decision (Analysis tab) and were skipped.")
        if pending:
            notes.append(f"{len(pending)} file(s) are still being analysed; start again when they are ready.")
        if not n:
            text = "Nothing to run: add files and wait for their analysis." if not notes else " ".join(notes)
            self.run_panel.stage_label.setText(text)
            return
        if notes:
            self.run_panel.log.append(" ".join(notes))

    def _master_choice_problem(self) -> str | None:
        if self.settings_panel.keeps_master() or self.view_section.selected_ids():
            return None
        return ("Without the archival master at least one viewing copy is needed. Select a profile in "
                "the Viewing copies tab, or tick \"Keep the archival master\".")

    def _update_start(self) -> None:
        """Start is only possible when the master/viewing-copy choice produces something."""
        views = len(self.view_section.selected_ids())
        self.settings_panel.set_views_selected(views)
        problem = self._master_choice_problem()
        running = self.controller.running
        self.run_panel.start_button.setEnabled(not running and problem is None)
        self.act_start.setEnabled(not running and problem is None)
        self.run_panel.start_button.setToolTip(problem or "")
        if problem and not running:
            self.run_panel.stage_label.setText("Cannot start: no archival master and no viewing copy selected.")
        elif not running and self.run_panel.stage_label.text().startswith("Cannot start: no archival"):
            self.run_panel.stage_label.setText("Idle")

    def _duplicate_outputs(self) -> str | None:
        """Two inputs with the same stem would write the same master / viewing copies (m5)."""
        out_dir, tpl = self.settings_panel.output_dir(), self.settings_panel.name_template_text()
        seen: dict[Path, str] = {}
        for it in self.model.runnable():
            src = Path(it.path)
            master = (Path(out_dir) if out_dir else src.parent) / (tpl.format(stem=src.stem) + ".mkv")
            if master in seen:
                return (f"{seen[master]} and {src.name} would both write {master.stem}.* (master or viewing "
                        "copies). Remove one of them from this run, or process them with different output folders.")
            seen[master] = src.name
        return None

    def _set_running(self, running: bool) -> None:
        self.run_panel.set_running(running)
        self.act_start.setEnabled(not running)
        if not running:
            self._update_start()
        self.act_cancel.setEnabled(running)
        self.settings_panel.setEnabled(not running)
        self.view_section.setEnabled(not running)
        self.analysis_panel.decision_box.setEnabled(not running)

    # ------------------------------------------------------------------ events
    def _on_current(self, item: JobItem | None) -> None:
        self.analysis_panel.set_item(item)
        self.settings_panel.set_analysis(item.analysis if item else None)
        self.preview.set_item(item)

    def _on_item_changed(self, item: JobItem) -> None:
        if item is self.queue_panel.current_item() and item.status in (JobStatus.ANALYZING, JobStatus.WAITING):
            self.analysis_panel.set_item(item)

    def _on_analysis_done(self, item: JobItem) -> None:
        if item is self.queue_panel.current_item():
            self._on_current(item)

    def _on_decision(self, item: JobItem) -> None:
        self.model.notify(item)
        if item is self.queue_panel.current_item():
            self.preview.set_item(item)

    def _show_fields_preview(self, item: JobItem) -> None:
        self.preview.set_view("fields")
        self.tabs.setCurrentIndex(TAB_PREVIEW)

    def _on_run_update(self, item: JobItem, state: RunState) -> None:
        self.run_panel.update_state(item, state, self.controller.batch_index, self.controller.batch_total)

    def _on_job_finished(self, item: JobItem, state: RunState) -> None:
        self.run_panel.show_finished(item, state)
        if state.exit_code == 0:
            for o in item.outputs:
                self.run_panel.log.append(f"output: {o.get('path')}")

    def _on_batch_finished(self, cancelled: bool) -> None:
        done = sum(1 for i in self.model.items if i.status == JobStatus.DONE)
        failed = sum(1 for i in self.model.items if i.status == JobStatus.FAILED)
        summary = f"Queue finished: {done} done" + (f", {failed} failed" if failed else "")
        if done and not self.settings_panel.keeps_master():
            summary += " — viewing copies only, no archival master kept"
        self.run_panel.show_batch_done(cancelled, summary)
        self._set_running(False)

    # ------------------------------------------------------------------ window
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        files = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if files:
            self.add_paths(files)
            event.acceptProposedAction()

    def _on_idle(self) -> None:
        if self._closing:
            self._closing = False
            self.close()

    def _restore_layout(self) -> None:
        s = ui_state.settings()
        geo = s.value("window/geometry")
        if isinstance(geo, QByteArray) and not geo.isEmpty():
            self.restoreGeometry(geo)
        else:
            self.resize(1280, 720)
        for key, split in (("window/h_split", self.h_split), ("window/v_split", self.v_split)):
            st = s.value(key)
            if isinstance(st, QByteArray) and not st.isEmpty():
                split.restoreState(st)
            else:
                fm = self.fontMetrics()
                if split is self.h_split:
                    split.setSizes([fm.horizontalAdvance("x") * 52, fm.horizontalAdvance("x") * 110])
                else:
                    split.setSizes([fm.height() * 30, fm.height() * 12])

    def shutdown(self) -> None:
        """Hard stop of every backend process (after stop_all, or in tests)."""
        self.controller.kill_all()
        self.preview.shutdown()
        self.catalog.shutdown()
        if self._doctor_call:
            self._doctor_call.kill()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._closing:
            event.ignore()
            return
        if self.controller.busy:
            if self.controller.running:
                answer = QMessageBox.question(
                    self, "Quit", "A job is running. Cancel it and quit?\n\n"
                    "qtgmc-6UI waits until the partial output has been removed before closing.",
                )
                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
            self._closing = True
            event.ignore()
            self.setEnabled(False)
            self.banner.show_message("info", "<b>Closing…</b> waiting for the backend to stop and clean up.", None)
            self.banner.setEnabled(True)
            self.controller.stop_all()
            return
        self.shutdown()
        s = ui_state.settings()
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("window/h_split", self.h_split.saveState())
        s.setValue("window/v_split", self.v_split.saveState())
        s.sync()
        super().closeEvent(event)
