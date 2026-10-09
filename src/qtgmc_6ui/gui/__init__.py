"""PySide6 GUI (WP-E).  A front end to the CLI: no processing logic, no vapoursynth import.

Modules: app (QApplication, main()), main_window, queue_model (job queue, QAbstractListModel),
analysis_panel, settings_panel, view_section + profile_editor, preview_widget (calls
``qtgmc-6ui preview-frame``), job_runner (QProcess + events.Event parsing, cancel via stdin
"cancel" then terminate()), log_pane (full log + copy), doctor_dialog.
"""
