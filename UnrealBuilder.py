#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unreal Builder - compile & package an Unreal Engine project.

Flow:
  1. Pick a .uproject (scan a folder, or browse manually).
  2. Read its EngineAssociation to locate the engine (registry for
     "5.4"-style IDs, direct path for source builds).
  3. Auto-detect Build.bat / RunUAT.bat.
  4. Compile: Build.bat (fast, opens the editor afterwards).
     Package: RunUAT.bat BuildCookRun (full cook/pak, pick output dir).

The last selected project is persisted and restored on next launch.
"""

import json
import os
import subprocess
import sys
import threading
import tkinter as tk
import winsound
from tkinter import filedialog, messagebox, ttk

import winreg

APP_TITLE = "Unreal Builder · Za0Shu1"
CONFIG_NAME = "unreal_builder_config.json"


def config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "UnrealBuilder")
    os.makedirs(path, exist_ok=True)
    return path


def load_config():
    try:
        with open(os.path.join(config_dir(), CONFIG_NAME), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(data):
    try:
        with open(os.path.join(config_dir(), CONFIG_NAME), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def find_uproject_root(path):
    return os.path.dirname(os.path.abspath(path))


def normalize_path(path):
    """Normalize a Windows path to backslashes (F:\\...\\Project.uproject)."""
    return os.path.normpath(path)


def read_uproject(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def engine_from_association(assoc, uproject_path):
    if not assoc:
        return None, "uproject has no EngineAssociation"
    assoc = assoc.strip()
    if os.path.isabs(assoc):
        if os.path.exists(assoc):
            return os.path.normpath(assoc), None
        return None, "EngineAssociation path does not exist: %s" % assoc
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in ("SOFTWARE\\EpicGames\\Unreal Engine",
                    "SOFTWARE\\WOW6432Node\\EpicGames\\Unreal Engine"):
            key_path = "%s\\%s" % (sub, assoc)
            try:
                key = winreg.OpenKey(hive, key_path)
            except OSError:
                continue
            try:
                value, _ = winreg.QueryValueEx(key, "InstalledDirectory")
                if value and os.path.exists(value):
                    return os.path.normpath(value), None
            finally:
                winreg.CloseKey(key)
    return None, "engine '%s' not found in registry (is it a source build?)" % assoc


def find_build_bat(engine_root):
    candidate = os.path.join(engine_root, "Engine", "Build", "BatchFiles", "Build.bat")
    if os.path.exists(candidate):
        return candidate, None
    return None, "Build.bat not found: %s" % candidate


def find_runuat_bat(engine_root):
    candidate = os.path.join(engine_root, "Engine", "Build", "BatchFiles", "RunUAT.bat")
    if os.path.exists(candidate):
        return candidate, None
    return None, "RunUAT.bat not found: %s" % candidate


def find_targets(uproject_root):
    source_dir = os.path.join(uproject_root, "Source")
    targets = []
    if os.path.isdir(source_dir):
        for root, _, files in os.walk(source_dir):
            for f in files:
                if f.endswith(".Target.cs"):
                    targets.append(f[: -len(".Target.cs")])
    targets = sorted(targets)
    # The Editor target is the default build; hide pure Game/Client targets.
    editors = [t for t in targets if t.endswith("Editor")]
    return editors if editors else targets


def scan_uprojects(root):
    found = []
    if root and os.path.isdir(root):
        for dirpath, dirnames, filenames in os.walk(root):
            for f in filenames:
                if f.endswith(".uproject"):
                    found.append(normalize_path(os.path.join(dirpath, f)))
    return sorted(found)


PLATFORMS = ["Win64", "Win32", "Linux", "Mac", "Android", "IOS", "TVOS", "HoloLens"]
CONFIGS = ["DebugGame", "Development", "Shipping", "Test"]


class BuildThread(threading.Thread):
    def __init__(self, cmd, log_cb, done_cb):
        super().__init__(daemon=True)
        self.cmd = cmd
        self.log_cb = log_cb
        self.done_cb = done_cb
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        try:
            proc = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
                bufsize=1,
                universal_newlines=True,
                errors="replace",
            )
        except Exception as exc:
            self.log_cb("Failed to start process: %s\n" % exc)
            self.done_cb(False)
            return
        for line in proc.stdout:
            self.log_cb(line)
            if self._cancel.is_set():
                proc.kill()
                break
        proc.wait()
        self.log_cb("\n[exit code %d]\n" % proc.returncode)
        self.done_cb(self._cancel.is_set() or proc.returncode == 0)


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("980x680")
        self.build_thread = None
        self.uprojects = []
        self._action = None
        self._output_dir = None
        self._open_until = None

        pad = {"padx": 6, "pady": 4}
        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)

        # Project row
        row = ttk.Frame(frm)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="Unreal Project:").pack(side="left")
        self.uproject_var = tk.StringVar()
        self.uproject_combo = ttk.Combobox(
            row, textvariable=self.uproject_var, state="normal", width=60
        )
        self.uproject_combo.pack(side="left", padx=4)
        self.uproject_combo.bind("<<ComboboxSelected>>", lambda e: self.on_project_selected())
        ttk.Button(row, text="Scan Folder...", command=self.on_scan).pack(side="left", padx=2)
        ttk.Button(row, text="Browse...", command=self.on_browse).pack(side="left", padx=2)

        # Detected info
        info = ttk.Frame(frm)
        info.pack(fill="x", **pad)
        ttk.Label(info, text="Engine:").pack(side="left")
        self.engine_var = tk.StringVar(value="(select a project)")
        ttk.Entry(info, textvariable=self.engine_var, state="readonly", width=70).pack(
            side="left", padx=4
        )

        # Platform / config
        row2 = ttk.Frame(frm)
        row2.pack(fill="x", **pad)
        ttk.Label(row2, text="Platform:").pack(side="left")
        self.platform_var = tk.StringVar(value="Win64")
        ttk.Combobox(
            row2, textvariable=self.platform_var, values=PLATFORMS, state="readonly", width=10
        ).pack(side="left", padx=4)
        ttk.Label(row2, text="Config:").pack(side="left", padx=(12, 2))
        self.config_var = tk.StringVar(value="Development")
        ttk.Combobox(
            row2, textvariable=self.config_var, values=CONFIGS, state="readonly", width=12
        ).pack(side="left", padx=4)

        # Actions
        row3 = ttk.Frame(frm)
        row3.pack(fill="x", **pad)
        self.compile_btn = ttk.Button(row3, text="Compile", command=self.on_compile)
        self.compile_btn.pack(side="left")
        self.package_btn = ttk.Button(row3, text="Package", command=self.on_package)
        self.package_btn.pack(side="left", padx=4)
        self.cancel_btn = ttk.Button(row3, text="Cancel", command=self.on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(row3, textvariable=self.status_var).pack(side="left", padx=12)
        # Shown for 1 minute after a successful package (auto-hides).
        self.open_dir_btn = ttk.Button(row3, text="Open Output", command=self.open_output)
        self.open_dir_btn.pack(side="right")
        self.open_dir_btn.pack_forget()

        # Log area
        logfrm = ttk.Frame(frm)
        logfrm.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logfrm, wrap="none", font=("Consolas", 9),
                           bg="#1e1e1e", fg="#d4d4d4")
        scroll = ttk.Scrollbar(logfrm, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.log.bind("<Key>", lambda e: "break")
        self.log.bind("<Button-3>", self.show_log_menu)
        self.log_menu = tk.Menu(self.root, tearoff=0)
        self.log_menu.add_command(label="Copy", command=self.copy_selection)
        self.log_menu.add_command(label="Select All", command=self.select_all_log)

        self.restore_last_project()

    # ---- helpers -------------------------------------------------------
    def log_line(self, text):
        self.log.insert("end", text)
        self.log.see("end")

    def show_log_menu(self, event):
        try:
            self.log_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.log_menu.grab_release()

    def copy_selection(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.log.get("sel.first", "sel.last"))
        except tk.TclError:
            pass

    def select_all_log(self):
        self.log.tag_add("sel", "1.0", "end-1c")

    def set_status(self, text):
        self.status_var.set(text)

    def refresh_project_list(self):
        self.uproject_combo["values"] = self.uprojects

    def remember_project(self, path):
        path = normalize_path(path)
        cfg = load_config()
        cfg["last_project"] = path
        # Persist the whole known-project list so a fresh launch shows them all.
        known = list(cfg.get("projects", []))
        if path not in known:
            known.append(path)
        for p in list(known):
            if not os.path.isfile(p):
                known.remove(p)
        cfg["projects"] = [normalize_path(p) for p in known]
        save_config(cfg)

    def remember_projects(self, projects):
        """Replace the persisted project list entirely (scan acts as a filter)."""
        cfg = load_config()
        cfg["projects"] = [normalize_path(p) for p in projects if os.path.isfile(p)]
        save_config(cfg)

    def restore_last_project(self):
        cfg = load_config()
        self.uprojects = [normalize_path(p) for p in cfg.get("projects", []) if os.path.isfile(p)]
        if self.uprojects:
            self.refresh_project_list()
        last = cfg.get("last_project")
        if last and os.path.isfile(last):
            self.uproject_var.set(normalize_path(last))
            self.on_project_selected()

    # ---- events --------------------------------------------------------
    def on_scan(self):
        root_dir = filedialog.askdirectory(title="Select a folder to scan for .uproject files")
        if not root_dir:
            return
        found = scan_uprojects(root_dir)
        if not found:
            messagebox.showinfo(APP_TITLE, "No .uproject files found under:\n%s" % root_dir)
            return
        # Scan replaces the whole list — it acts as a filter, not an accumulator.
        self.uprojects = found
        self.remember_projects(found)
        self.refresh_project_list()
        self.uproject_var.set(found[0])
        self.on_project_selected()
        self.set_status("Found %d project(s)" % len(found))

    def on_browse(self):
        path = filedialog.askopenfilename(
            title="Select a .uproject",
            filetypes=[("UE project", "*.uproject"), ("All files", "*.*")],
        )
        if not path:
            return
        path = normalize_path(path)
        if path not in self.uprojects:
            self.uprojects.append(path)
            self.refresh_project_list()
        self.uproject_var.set(path)
        self.on_project_selected()

    def on_project_selected(self):
        path = self.uproject_var.get().strip()
        if not path or not os.path.isfile(path):
            return
        self.remember_project(path)
        try:
            data = read_uproject(path)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, "Cannot read uproject: %s" % exc)
            return
        assoc = data.get("EngineAssociation", "")
        engine_root, err = engine_from_association(assoc, path)
        if err:
            messagebox.showerror(APP_TITLE, err)
            self.engine_var.set("(engine not found)")
            return
        self.engine_var.set(engine_root)
        self.set_status("Project selected: %s" % os.path.basename(path))

    # ---- build actions -------------------------------------------------
    def on_compile(self):
        uproject = self.uproject_var.get().strip()
        engine_root = self.engine_var.get().strip()
        platform = self.platform_var.get()
        config = self.config_var.get()
        if not uproject or not engine_root or "(engine" in engine_root:
            messagebox.showwarning(APP_TITLE, "Pick a valid project first.")
            return
        build_bat, err = find_build_bat(engine_root)
        if err:
            messagebox.showerror(APP_TITLE, err)
            return
        targets = find_targets(find_uproject_root(uproject))
        if not targets:
            messagebox.showwarning(APP_TITLE, "No target found for this project.")
            return
        target = targets[0]

        cmd = [
            build_bat,
            target,
            platform,
            config,
            "-Project=%s" % uproject,
            "-WaitMutex",
        ]
        self.log_line("$ %s\n" % " ".join(cmd))
        self.start_build(cmd, "Compiling %s %s %s ..." % (target, platform, config), action="compile")

    def on_package(self):
        uproject = self.uproject_var.get().strip()
        engine_root = self.engine_var.get().strip()
        platform = self.platform_var.get()
        config = self.config_var.get()
        if not uproject or not engine_root or "(engine" in engine_root:
            messagebox.showwarning(APP_TITLE, "Pick a valid project first.")
            return
        runuat, err = find_runuat_bat(engine_root)
        if err:
            messagebox.showerror(APP_TITLE, err)
            return

        # Reuse the last package output dir; default to the project root.
        cfg = load_config()
        default_output = cfg.get("package_output") or find_uproject_root(uproject)
        if not os.path.isdir(default_output):
            default_output = find_uproject_root(uproject)
        output = filedialog.askdirectory(
            title="Select package output directory",
            initialdir=default_output,
        )
        if not output:
            return
        cfg["package_output"] = output
        save_config(cfg)
        os.makedirs(output, exist_ok=True)

        cmd = [
            runuat,
            "BuildCookRun",
            "-Project=%s" % uproject,
            "-NoP4",
            "-Platform=%s" % platform,
            "-ClientConfig=%s" % config,
            "-Cook",
            "-Build",
            "-Stage",
            "-Pak",
            "-Archive",
            "-ArchiveDirectory=%s" % output,
        ]
        self.log_line("$ %s\n" % " ".join(cmd))
        self.start_build(cmd, "Packaging %s %s ..." % (platform, config),
                         action="package", output_dir=output)

    def start_build(self, cmd, status, action=None, output_dir=None):
        self.compile_btn.configure(state="disabled")
        self.package_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.set_status(status)
        self._action = action
        self._output_dir = output_dir
        safe_log = lambda text: self.root.after(0, self.log_line, text)
        safe_done = lambda ok: self.root.after(0, self.on_build_done, ok)
        self.build_thread = BuildThread(cmd, safe_log, safe_done)
        self.build_thread.start()

    def on_build_done(self, ok):
        self.compile_btn.configure(state="normal")
        self.package_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.set_status("Finished (exit code %d)" % (0 if ok else 1))
        self.build_thread = None

        if ok:
            # A short chime like the editor's build-done sound.
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            if getattr(self, "_action", None) == "package" and getattr(self, "_output_dir", None):
                self._open_until = self.root.after(60000, self.hide_open_btn)
                self.open_dir_btn.pack(side="right")
        else:
            winsound.MessageBeep(winsound.MB_ICONHAND)

    def open_output(self):
        if getattr(self, "_output_dir", None):
            try:
                os.startfile(self._output_dir)
            except OSError as exc:
                messagebox.showerror(APP_TITLE, "Cannot open directory: %s" % exc)

    def hide_open_btn(self):
        self.open_dir_btn.pack_forget()

    def on_cancel(self):
        if self.build_thread:
            self.build_thread.cancel()
            self.set_status("Cancelling...")


def resource_path(relative):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def main():
    root = tk.Tk()
    ico = resource_path("Default.ico")
    if os.path.exists(ico):
        try:
            root.iconbitmap(ico)
        except tk.TclError:
            pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()