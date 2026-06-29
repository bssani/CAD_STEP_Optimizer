"""Simple Tkinter GUI front-end for the pipeline.

Double-clicking the .exe (no args) opens this window: pick a STEP file, tick
options, choose an output folder, press 변환 실행. The actual work runs in a
background thread so the window stays responsive; stdout is streamed into the
log panel.

tkinter is used because it is in the Python stdlib (no extra dependency) and
bundles cleanly with PyInstaller (the spec must NOT exclude 'tkinter').
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable, Optional

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext


class _QueueWriter:
    """File-like object that funnels writes into a thread-safe queue."""
    def __init__(self, q: "queue.Queue"):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(("log", s))

    def flush(self):
        pass


def launch(run_func: Callable, initial_step: Optional[Path] = None) -> None:
    from cadpipe import optimize as optimize_mod  # OptimizeOptions

    root = tk.Tk()
    root.title("CAD STEP → GLB 변환기")
    root.geometry("720x620")
    try:
        root.tk.call("tk", "scaling", 1.2)
    except Exception:
        pass

    pad = {"padx": 8, "pady": 4}
    q: "queue.Queue" = queue.Queue()

    # ---- file row ----
    frm = ttk.Frame(root)
    frm.pack(fill="x", **pad)
    ttk.Label(frm, text="STEP 파일:", width=12).grid(row=0, column=0, sticky="w")
    step_var = tk.StringVar(value=str(initial_step) if initial_step else "")
    ttk.Entry(frm, textvariable=step_var).grid(row=0, column=1, sticky="ew")
    ttk.Button(frm, text="찾아보기…",
               command=lambda: _pick_file(step_var)).grid(row=0, column=2, padx=4)

    ttk.Label(frm, text="출력 폴더:", width=12).grid(row=1, column=0, sticky="w", pady=(6, 0))
    out_var = tk.StringVar(value="")
    ttk.Entry(frm, textvariable=out_var).grid(row=1, column=1, sticky="ew", pady=(6, 0))
    ttk.Button(frm, text="찾아보기…",
               command=lambda: _pick_dir(out_var)).grid(row=1, column=2, padx=4, pady=(6, 0))
    ttk.Label(frm, text="(비워두면 STEP 파일 옆에 cadpipe_reports 폴더)",
              foreground="#888").grid(row=2, column=1, sticky="w")
    frm.columnconfigure(1, weight=1)

    # ---- options ----
    opt = ttk.LabelFrame(root, text="옵션")
    opt.pack(fill="x", **pad)

    ttk.Label(opt, text="정밀도 목표 P95 (mm):").grid(row=0, column=0, sticky="w", padx=8, pady=4)
    target_var = tk.StringVar(value="0.1")
    ttk.Entry(opt, textvariable=target_var, width=8).grid(row=0, column=1, sticky="w")

    ttk.Label(opt, text="Up축:").grid(row=0, column=2, sticky="e", padx=(20, 4))
    up_var = tk.StringVar(value="y")
    ttk.Radiobutton(opt, text="Y-up (glTF/Babylon)", variable=up_var, value="y").grid(row=0, column=3, sticky="w")
    ttk.Radiobutton(opt, text="Z-up (CAD)", variable=up_var, value="z").grid(row=0, column=4, sticky="w")

    web_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(opt, text="웹용 삼각형 줄이기 (목표 편차 안에서 자동, 권장)",
                    variable=web_var).grid(row=1, column=0, columnspan=4, sticky="w", padx=8)
    inst_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(opt, text="반복 부품 인스턴싱 (더 가벼움 · 개별 부품 이름은 사라짐)",
                    variable=inst_var).grid(row=2, column=0, columnspan=4, sticky="w", padx=8)

    ttk.Label(opt, text="측정 정밀도(기준 배율):").grid(row=3, column=0, sticky="w", padx=8, pady=4)
    rf_var = tk.StringVar(value="10")
    ttk.Entry(opt, textvariable=rf_var, width=8).grid(row=3, column=1, sticky="w")
    ttk.Label(opt, text="큰 파일이 느리면 5 또는 3으로", foreground="#888").grid(row=3, column=2, columnspan=3, sticky="w")

    # ---- run button + status ----
    runfrm = ttk.Frame(root)
    runfrm.pack(fill="x", **pad)
    run_btn = ttk.Button(runfrm, text="변환 실행 ▶", width=18)
    run_btn.pack(side="left")
    status_var = tk.StringVar(value="대기 중")
    ttk.Label(runfrm, textvariable=status_var).pack(side="left", padx=12)
    bar = ttk.Progressbar(runfrm, mode="indeterminate", length=180)
    bar.pack(side="right")
    open_btn = ttk.Button(runfrm, text="결과 폴더 열기", state="disabled")
    open_btn.pack(side="right", padx=8)

    # ---- log ----
    log = scrolledtext.ScrolledText(root, height=16, font=("Consolas", 9),
                                    bg="#1e1e1e", fg="#d4d4d4")
    log.pack(fill="both", expand=True, **pad)

    state = {"archive": None}

    def _log(msg):
        log.insert("end", msg)
        log.see("end")

    def on_run():
        step = step_var.get().strip().strip('"')
        if not step or not Path(step).exists():
            status_var.set("STEP 파일을 선택하세요 ❌")
            return
        step_p = Path(step)
        out = out_var.get().strip().strip('"')
        out_root = Path(out) if out else (step_p.resolve().parent / "cadpipe_reports")
        try:
            target = float(target_var.get())
            rf = float(rf_var.get())
        except ValueError:
            status_var.set("숫자 옵션이 잘못됨 ❌")
            return

        opts = optimize_mod.OptimizeOptions(
            merge_faces=True, dedup=True, weld=True, meshopt=True,
            simplify=web_var.get(), instance=inst_var.get(),
        )
        run_btn.config(state="disabled")
        open_btn.config(state="disabled")
        status_var.set("실행 중… (큰 파일은 몇 분 걸립니다)")
        bar.start(12)
        log.delete("1.0", "end")

        def worker():
            old = (sys.stdout, sys.stderr)
            sys.stdout = sys.stderr = _QueueWriter(q)
            try:
                archive = run_func(step_p, out_root, target_mm=target, up_axis=up_var.get(),
                                   ref_factor=rf, opt_options=opts)
                q.put(("done", str(archive)))
            except Exception as e:
                q.put(("log", "\n" + traceback.format_exc()))
                q.put(("fail", str(e)))
            finally:
                sys.stdout, sys.stderr = old

        threading.Thread(target=worker, daemon=True).start()

    run_btn.config(command=on_run)

    def open_folder():
        if state["archive"]:
            try:
                os.startfile(state["archive"])  # noqa: Windows only
            except Exception:
                pass
    open_btn.config(command=open_folder)

    def poll():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    _log(val)
                elif kind == "done":
                    bar.stop()
                    state["archive"] = val
                    status_var.set("완료 ✅  결과: " + Path(val).name)
                    run_btn.config(state="normal")
                    open_btn.config(state="normal")
                elif kind == "fail":
                    bar.stop()
                    status_var.set("실패 ❌  " + val)
                    run_btn.config(state="normal")
        except queue.Empty:
            pass
        root.after(120, poll)

    root.after(120, poll)
    root.mainloop()


def _pick_file(var: tk.StringVar) -> None:
    p = filedialog.askopenfilename(
        title="STEP 파일 선택",
        filetypes=[("STEP files", "*.step *.stp *.STEP *.STP"), ("All files", "*.*")])
    if p:
        var.set(p)


def _pick_dir(var: tk.StringVar) -> None:
    p = filedialog.askdirectory(title="출력 폴더 선택")
    if p:
        var.set(p)
