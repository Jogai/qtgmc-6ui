"""PySide6 GUI (WP-E).  A front end to the CLI: no processing logic, no vapoursynth import.

Every backend operation runs ``python -m qtgmc_6ui <command> --json|--events`` in a QProcess
(backend.py); the JSON-lines event stream is folded into a RunState (run_model.py).
Modules: app (entry), main_window, queue_model + queue_panel, controller (analysis and job
sequencing), analysis_panel, settings_panel, view_section + profile_editor + catalog,
preview_widget (``preview-frame`` PNGs), run_panel + log_pane, doctor_dialog, ui_state.
"""
