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
import time
import tkinter as tk
import tkinter.font as tkfont
import winsound
from tkinter import filedialog, messagebox, ttk

import winreg

APP_TITLE = "Unreal Builder · by Za0Shu1"
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


def list_engines():
    """Return [(association, path), ...] for every engine registered on this
    machine (Launcher builds under the registry, deduplicated)."""
    engines = {}
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in ("SOFTWARE\\EpicGames\\Unreal Engine",
                    "SOFTWARE\\WOW6432Node\\EpicGames\\Unreal Engine"):
            try:
                key = winreg.OpenKey(hive, sub)
            except OSError:
                continue
            try:
                index = 0
                while True:
                    try:
                        assoc = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        eng_key = winreg.OpenKey(key, assoc)
                    except OSError:
                        continue
                    try:
                        value, _ = winreg.QueryValueEx(eng_key, "InstalledDirectory")
                        if value and os.path.isdir(value):
                            engines.setdefault(assoc, os.path.normpath(value))
                    finally:
                        winreg.CloseKey(eng_key)
            finally:
                winreg.CloseKey(key)
    return sorted(engines.items())


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


def find_ubt(engine_root):
    candidate = os.path.join(
        engine_root, "Engine", "Binaries", "DotNET", "UnrealBuildTool", "UnrealBuildTool.exe"
    )
    if os.path.exists(candidate):
        return candidate, None
    return None, "UnrealBuildTool.exe not found: %s" % candidate


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


def is_cpp_project(uproject_path):
    """True if the project has C++ source (i.e. Target.cs files exist)."""
    return bool(find_targets(find_uproject_root(uproject_path)))


def scan_uprojects(root):
    found = []
    if root and os.path.isdir(root):
        for dirpath, dirnames, filenames in os.walk(root):
            for f in filenames:
                if f.endswith(".uproject"):
                    found.append(normalize_path(os.path.join(dirpath, f)))
    return sorted(found)


def center_window(win, width, height):
    """Set a fixed size for a Toplevel and center it on the screen."""
    win.update_idletasks()
    win.geometry("%dx%d" % (width, height))
    win.update_idletasks()
    w = max(win.winfo_width(), width)
    h = max(win.winfo_height(), height)
    x = max((win.winfo_screenwidth() - w) // 2, 0)
    y = max((win.winfo_screenheight() - h) // 2, 0)
    win.geometry("+%d+%d" % (x, y))


def set_window_icon(win):
    ico = resource_path("Default.ico")
    if os.path.exists(ico):
        try:
            win.iconbitmap(ico)
        except tk.TclError:
            pass


PLATFORMS = ["Win64", "Win32", "Linux", "Mac", "Android", "IOS", "TVOS", "HoloLens"]
CONFIGS = ["DebugGame", "Development", "Shipping", "Test"]


class EllipsisLabel(tk.Label):
    """A label that truncates its text with an ellipsis when too wide,
    and shows the full text in a tooltip on hover."""

    def __init__(self, master, **kw):
        super().__init__(master, anchor="w", **kw)
        self._full = ""
        self._tip = None
        self.bind("<Configure>", lambda e: self._refresh())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def set_text(self, text):
        self._full = text or ""
        self._refresh()

    def _refresh(self):
        width = self.winfo_width()
        font = tkfont.Font(font=self.cget("font"))
        if width > 1 and font.measure(self._full) > width:
            text = self._full
            while text and font.measure(text + "...") > width:
                text = text[:-1]
            self.configure(text=text + "...")
        else:
            self.configure(text=self._full)

    def _on_enter(self, _event):
        if self.cget("text") == self._full:
            return
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + 2
        self._tip = tk.Toplevel(self)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry("+%d+%d" % (x, y))
        tk.Label(self._tip, text=self._full, bg="#ffffe0", relief="solid",
                 borderwidth=1).pack()
        self._tip.after(3000, self._hide_tip)

    def _on_leave(self, _event):
        self._hide_tip()

    def _hide_tip(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


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
            self.done_cb(False, False)
            return
        for line in proc.stdout:
            self.log_cb(line)
            if self._cancel.is_set():
                proc.kill()
                break
        proc.wait()
        cancelled = self._cancel.is_set()
        self.done_cb(not cancelled and proc.returncode == 0, cancelled)


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

        # ---- 1. Project selection (object selection) ----
        proj_box = ttk.LabelFrame(frm, text="Project", padding=6)
        proj_box.pack(fill="x", **pad)

        row = ttk.Frame(proj_box)
        row.pack(fill="x")
        ttk.Label(row, text="Unreal Project:").pack(side="left")
        self.uproject_var = tk.StringVar()
        self.uproject_combo = ttk.Combobox(
            row, textvariable=self.uproject_var, state="normal", width=50
        )
        self.uproject_combo.pack(side="left", padx=4)
        self.uproject_combo.bind("<<ComboboxSelected>>", lambda e: self.on_project_selected())
        self.uproject_combo.bind("<Button-3>", self.show_project_menu)
        ttk.Button(row, text="Scan Folder", command=self.on_scan).pack(side="left", padx=2)
        ttk.Button(row, text="Browse", command=self.on_browse).pack(side="left", padx=2)

        info = ttk.Frame(proj_box)
        info.pack(fill="x", pady=(4, 0))
        ttk.Label(info, text="Engine:").pack(side="left")
        self.engine_var = tk.StringVar(value="(select a project)")
        ttk.Entry(info, textvariable=self.engine_var, state="readonly", width=60).pack(
            side="left", padx=4
        )

        # Right-click menu on the project combobox.
        self.project_menu = tk.Menu(self.root, tearoff=0)
        self.project_menu.add_command(
            label="Generate Visual Studio Project files", command=self.on_generate_vs
        )
        self.project_menu.add_command(
            label="Switch Unreal Engine Version...", command=self.on_switch_engine
        )

        # ---- 2. Functions ----
        act_box = ttk.LabelFrame(frm, text="Actions", padding=6)
        act_box.pack(fill="x", **pad)

        # Project utility functions
        rowf = ttk.Frame(act_box)
        rowf.pack(fill="x")
        self.open_project_btn = ttk.Button(rowf, text="Open Project", command=self.on_open_project)
        self.open_project_btn.pack(side="left")
        self.open_folder_btn = ttk.Button(rowf, text="Open Folder", command=self.on_open_dir)
        self.open_folder_btn.pack(side="left", padx=4)
        self.switch_engine_btn = ttk.Button(
            rowf, text="Switch Unreal Engine Version", command=self.on_switch_engine
        )
        self.switch_engine_btn.pack(side="left", padx=4)
        self.genvs_btn = ttk.Button(
            rowf, text="Generate Visual Studio Project Files", command=self.on_generate_vs
        )
        self.genvs_btn.pack(side="left", padx=4)

        # Compile: Platform / Config are its required sub-options
        rowc = ttk.Frame(act_box)
        rowc.pack(fill="x", pady=(6, 0))
        ttk.Label(rowc, text="Compile:").pack(side="left")
        ttk.Label(rowc, text="Platform:").pack(side="left", padx=(8, 2))
        self.platform_var = tk.StringVar(value="Win64")
        ttk.Combobox(
            rowc, textvariable=self.platform_var, values=PLATFORMS, state="readonly", width=10
        ).pack(side="left", padx=2)
        ttk.Label(rowc, text="Config:").pack(side="left", padx=(10, 2))
        self.config_var = tk.StringVar(value="Development")
        ttk.Combobox(
            rowc, textvariable=self.config_var, values=CONFIGS, state="readonly", width=12
        ).pack(side="left", padx=2)
        self.compile_btn = ttk.Button(rowc, text="Compile", command=self.on_compile)
        self.compile_btn.pack(side="left", padx=6)

        # Package: output path is chosen via dialog (its required sub-option)
        rowp = ttk.Frame(act_box)
        rowp.pack(fill="x", pady=(6, 0))
        ttk.Label(rowp, text="Package:").pack(side="left")
        self.package_btn = ttk.Button(rowp, text="Package", command=self.on_package)
        self.package_btn.pack(side="left", padx=6)
        # Shown for 1 minute after a successful package (auto-hides).
        self.open_dir_btn = ttk.Button(rowp, text="Open Output", command=self.open_output)
        self.open_dir_btn.pack(side="left", padx=4)
        self.open_dir_btn.pack_forget()

        # Status / cancel row
        rowst = ttk.Frame(frm)
        rowst.pack(fill="x", **pad)
        self.cancel_btn = ttk.Button(rowst, text="Cancel", command=self.on_cancel, state="disabled")
        self.cancel_btn.pack(side="left")
        self.status_var = tk.StringVar(value="Ready")
        self.status_label = EllipsisLabel(rowst, width=40)
        self.status_label.pack(side="left", padx=12, fill="x", expand=True)
        self.status_label.set_text(self.status_var.get())

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

    def log_banner(self, text, closing=False):
        line = "=" * 80
        if closing:
            self.log_line("\n---- %s ----\n%s\n" % (text, line))
        else:
            self.log_line("\n%s\n---- %s ----\n\n" % (line, text))

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
        self.status_label.set_text(text)

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
        cfg = load_config()
        initial = cfg.get("scan_dir")
        if not initial or not os.path.isdir(initial):
            initial = None
        root_dir = filedialog.askdirectory(
            title="Select a folder to scan for .uproject files",
            initialdir=initial,
        )
        if not root_dir:
            return
        root_dir = normalize_path(root_dir)
        cfg["scan_dir"] = root_dir
        save_config(cfg)
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
        cfg = load_config()
        initial = cfg.get("scan_dir")
        if not initial or not os.path.isdir(initial):
            initial = None
        path = filedialog.askopenfilename(
            title="Select a .uproject",
            initialdir=initial,
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

    def show_project_menu(self, event):
        try:
            self.project_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.project_menu.grab_release()

    def on_open_project(self):
        uproject = self.uproject_var.get().strip()
        if not uproject or not os.path.isfile(uproject):
            messagebox.showwarning(APP_TITLE, "Pick a valid project first.")
            return
        try:
            os.startfile(uproject)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, "Cannot open project: %s" % exc)

    def on_open_dir(self):
        uproject = self.uproject_var.get().strip()
        if not uproject or not os.path.isfile(uproject):
            messagebox.showwarning(APP_TITLE, "Pick a valid project first.")
            return
        try:
            os.startfile(find_uproject_root(uproject))
        except OSError as exc:
            messagebox.showerror(APP_TITLE, "Cannot open directory: %s" % exc)

    def on_generate_vs(self):
        uproject = self.uproject_var.get().strip()
        engine_root = self.engine_var.get().strip()
        if not uproject or not engine_root or "(engine" in engine_root:
            messagebox.showwarning(APP_TITLE, "Pick a valid project first.")
            return
        ubt, err = find_ubt(engine_root)
        if err:
            messagebox.showerror(APP_TITLE, err)
            return
        cmd = [ubt, "-projectfiles", "-project=%s" % uproject, "-game"]
        self.start_build(cmd, "Generating Visual Studio project files...", action="genvs")

    def on_switch_engine(self):
        uproject = self.uproject_var.get().strip()
        if not uproject or not os.path.isfile(uproject):
            messagebox.showwarning(APP_TITLE, "Pick a valid project first.")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Switch Unreal Engine Version")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        set_window_icon(dlg)

        body = ttk.Frame(dlg, padding=10)
        body.pack(fill="both", expand=True)

        rows = []  # (association, path, label)
        for assoc, path in list_engines():
            rows.append((assoc, path, "%s  ->  %s" % (assoc, path)))

        ttk.Label(
            body,
            text="Select the Unreal Engine version to associate with this project:",
        ).pack(anchor="w", pady=(0, 6))

        listfrm = ttk.Frame(body)
        listfrm.pack(fill="both", expand=True)
        listbox = tk.Listbox(listfrm, width=90, height=12, selectmode="browse")
        scroll = ttk.Scrollbar(listfrm, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=scroll.set)
        listbox.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for _, _, label in rows:
            listbox.insert("end", label)
        if rows:
            listbox.selection_set(0)

        def browse_source():
            path = filedialog.askdirectory(title="Select the root of a source-built engine")
            if not path:
                return
            path = normalize_path(path)
            if any(os.path.normpath(p) == path for _, p, _ in rows):
                return
            rows.append((path, path, "%s  ->  %s" % (path, path)))
            listbox.insert("end", rows[-1][2])
            listbox.selection_set("end")

        def apply():
            sel = listbox.curselection()
            if not sel:
                messagebox.showwarning(APP_TITLE, "Select an engine first.", parent=dlg)
                return
            assoc, path, _ = rows[sel[0]]
            try:
                with open(uproject, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
            except Exception as exc:
                messagebox.showerror(APP_TITLE, "Cannot read uproject: %s" % exc, parent=dlg)
                return
            data["EngineAssociation"] = assoc
            try:
                with open(uproject, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4)
            except Exception as exc:
                messagebox.showerror(APP_TITLE, "Cannot write uproject: %s" % exc, parent=dlg)
                return
            dlg.destroy()
            self.set_status("Engine association set to %s" % assoc)
            # Re-resolve and refresh the engine directory display.
            self.on_project_selected()
            # C++ projects need a fresh project-files regeneration after the
            # engine switch; pure Blueprint projects are done here.
            if is_cpp_project(uproject):
                self.on_generate_vs()

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Browse for Source Build...", command=browse_source).pack(side="left")
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right", padx=(4, 0))
        ttk.Button(btns, text="OK", command=apply).pack(side="right")

        center_window(dlg, 640, 380)
        dlg.grab_set()
        dlg.focus_set()

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
        self.start_build(cmd, "Compiling %s %s %s" % (target, platform, config), action="compile")

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

        # Per-project package output dir: keyed by the uproject path so each
        # project remembers its own output; fall back to the project root.
        cfg = load_config()
        outputs = cfg.get("package_outputs") or {}
        uproject = normalize_path(uproject)
        default_output = outputs.get(uproject) or find_uproject_root(uproject)
        if not os.path.isdir(default_output):
            default_output = find_uproject_root(uproject)
        output = filedialog.askdirectory(
            title="Select package output directory",
            initialdir=default_output,
        )
        if not output:
            return
        outputs[uproject] = normalize_path(output)
        cfg["package_outputs"] = outputs
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
        self.start_build(cmd, "Packaging %s %s" % (platform, config),
                         action="package", output_dir=output)

    def start_build(self, cmd, status, action=None, output_dir=None):
        self.compile_btn.configure(state="disabled")
        self.package_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.set_status(status)
        self._action = action
        self._output_dir = output_dir
        self.log_banner("%s @ %s" % (status, time.strftime("%Y-%m-%d %H:%M:%S")))
        safe_log = lambda text: self.root.after(0, self.log_line, text)
        safe_done = lambda ok, cancelled: self.root.after(0, self.on_build_done, ok, cancelled)
        self.build_thread = BuildThread(cmd, safe_log, safe_done)
        self.build_thread.start()

    def on_build_done(self, ok, cancelled):
        self.compile_btn.configure(state="normal")
        self.package_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.build_thread = None

        if cancelled:
            result = "Cancelled (exit code 1)"
            winsound.MessageBeep(winsound.MB_ICONHAND)
            self.hide_open_btn()
        elif ok:
            result = "Finished (exit code 0)"
            # A short chime like the editor's build-done sound.
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            if getattr(self, "_action", None) == "package" and getattr(self, "_output_dir", None):
                self._open_until = self.root.after(60000, self.hide_open_btn)
                self.open_dir_btn.pack(side="left", padx=4)
        else:
            result = "Failed (exit code 1)"
            winsound.MessageBeep(winsound.MB_ICONHAND)
            self.hide_open_btn()
        self.set_status(result)
        self.log_banner("%s @ %s" % (result, time.strftime("%Y-%m-%d %H:%M:%S")), closing=True)

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
            self.set_status("Cancelling")


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