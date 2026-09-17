#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Графический интерфейс к эмулятору принтера Argox 3140 (argox_emulator.py).

Кнопкой «Старт» поднимает RAW/TCP-порт 9100 и принимает задания печати
от драйвера Argox, сохраняя RAW (.bin) и рендеря каждое задание в PDF.
Лог виден в окне. Папки RAW/PDF открываются кнопками.

Сборка в .exe:
    pyinstaller --noconfirm --onefile --windowed ^
        --name ArgoxEmulator ^
        --collect-all barcode --collect-all PIL ^
        --hidden-import win32print --hidden-import win32ui ^
        --hidden-import win32gui --hidden-import win32con ^
        --hidden-import PIL.ImageWin ^
        --collect-all qrcode --collect-all pystrich ^
        argox_gui.py
"""

import json
import os
import queue
import sys
import threading

import tkinter as tk
from tkinter import ttk, messagebox

import argox_emulator as E


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Эмулятор принтера Argox 3140  →  PDF / Godex")
        self.geometry("820x560")
        self.minsize(680, 460)

        self.server = None
        self.server_thread = None
        self.log_queue = queue.Queue()

        self.cfg = E.load_config()
        self._build_ui()

        # приём строк лога из потоков сервера
        E.add_log_listener(self._on_log)
        self.after(150, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._append(f"Готово. Папка данных: {E.BASE_DIR}")
        if not E.HAVE_PIL:
            self._append("ВНИМАНИЕ: Pillow не найден — PDF создаваться не будет.")
        elif not E.HAVE_BARCODE:
            self._append("python-barcode не найден — штрихкоды будут плейсхолдерами.")

    # ------------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="IP:").grid(row=0, column=0, sticky="e")
        self.var_ip = tk.StringVar(value=str(self.cfg.get("listen_ip", "0.0.0.0")))
        ttk.Entry(top, textvariable=self.var_ip, width=16).grid(row=0, column=1, sticky="w", padx=(2, 12))

        ttk.Label(top, text="Порт:").grid(row=0, column=2, sticky="e")
        self.var_port = tk.StringVar(value=str(self.cfg.get("port", 9100)))
        ttk.Entry(top, textvariable=self.var_port, width=7).grid(row=0, column=3, sticky="w", padx=(2, 12))

        ttk.Label(top, text="DPI:").grid(row=0, column=4, sticky="e")
        self.var_dpi = tk.StringVar(value=str(self.cfg.get("dpi", 300)))
        ttk.Entry(top, textvariable=self.var_dpi, width=6).grid(row=0, column=5, sticky="w", padx=(2, 12))

        self.var_raw = tk.BooleanVar(value=bool(self.cfg.get("save_raw", True)))
        ttk.Checkbutton(top, text="Сохранять RAW", variable=self.var_raw).grid(row=1, column=0, columnspan=2, sticky="w")
        self.var_pdf = tk.BooleanVar(value=bool(self.cfg.get("save_pdf", True)))
        ttk.Checkbutton(top, text="Сохранять PDF", variable=self.var_pdf).grid(row=1, column=2, columnspan=2, sticky="w")
        self.var_debug = tk.BooleanVar(value=bool(self.cfg.get("debug", False)))
        ttk.Checkbutton(top, text="HEX в лог", variable=self.var_debug).grid(row=1, column=4, columnspan=2, sticky="w")

        # --- Автопересылка на принтер Godex ---
        fwd = ttk.Frame(self)
        fwd.pack(fill="x", **pad)
        self.var_fwd = tk.BooleanVar(value=bool(self.cfg.get("forward_to_printer", False)))
        ttk.Checkbutton(fwd, text="Печатать на Godex:", variable=self.var_fwd).pack(side="left")
        self.var_printer = tk.StringVar(value=str(self.cfg.get("printer_name", "")))
        printers = E.list_windows_printers()
        self.cmb_printer = ttk.Combobox(fwd, textvariable=self.var_printer,
                                        values=printers, width=38, state="readonly")
        self.cmb_printer.pack(side="left", padx=(4, 6))
        ttk.Button(fwd, text="⟳", width=3, command=self._refresh_printers).pack(side="left")
        self.var_autosize = tk.BooleanVar(value=bool(self.cfg.get("auto_label_size", True)))
        ttk.Checkbutton(fwd, text="Авторазмер этикетки",
                        variable=self.var_autosize).pack(side="left", padx=(12, 0))
        if not E.HAVE_WIN32:
            self.cmb_printer.configure(state="disabled")
            ttk.Label(fwd, text="(нет pywin32)", foreground="#b00").pack(side="left", padx=6)

        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        self.btn_start = ttk.Button(btns, text="▶  Старт", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(btns, text="■  Стоп", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(6, 18))
        ttk.Button(btns, text="Папка PDF", command=lambda: self._open(E.PDF_DIR)).pack(side="left", padx=3)
        ttk.Button(btns, text="Папка RAW", command=lambda: self._open(E.JOBS_DIR)).pack(side="left", padx=3)
        ttk.Button(btns, text="Очистить лог", command=self._clear).pack(side="left", padx=3)

        self.var_status = tk.StringVar(value="● Остановлен")
        self.lbl_status = ttk.Label(self, textvariable=self.var_status, foreground="#b00")
        self.lbl_status.pack(anchor="w", padx=8)

        # лог
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        self.txt = tk.Text(frame, wrap="none", height=18, bg="#111", fg="#d0d0d0",
                           insertbackground="#d0d0d0", font=("Consolas", 9))
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=yscroll.set, state="disabled")
        self.txt.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")

    # ------------------------------------------------------------------
    def _refresh_printers(self):
        printers = E.list_windows_printers()
        self.cmb_printer.configure(values=printers)
        self._append(f"Найдено принтеров: {len(printers)}")

    def _collect_cfg(self):
        cfg = E.load_config()
        cfg["listen_ip"] = self.var_ip.get().strip() or "0.0.0.0"
        try:
            cfg["port"] = int(self.var_port.get())
        except ValueError:
            raise ValueError("Порт должен быть числом")
        try:
            cfg["dpi"] = int(self.var_dpi.get())
        except ValueError:
            raise ValueError("DPI должен быть числом")
        cfg["save_raw"] = self.var_raw.get()
        cfg["save_pdf"] = self.var_pdf.get()
        cfg["debug"] = self.var_debug.get()
        cfg["forward_to_printer"] = self.var_fwd.get()
        cfg["printer_name"] = self.var_printer.get().strip()
        cfg["auto_label_size"] = self.var_autosize.get()
        if cfg["forward_to_printer"] and not cfg["printer_name"]:
            raise ValueError("Отмечено «Печатать на Godex», но принтер не выбран")
        # запомним настройки на следующий запуск
        try:
            with open(E.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)
        except Exception:
            pass
        return cfg

    def start(self):
        try:
            cfg = self._collect_cfg()
        except ValueError as e:
            messagebox.showerror("Ошибка", str(e))
            return
        try:
            self.server = E.create_server(cfg)
        except OSError as e:
            messagebox.showerror("Не удалось запустить",
                                 f"Порт {cfg['port']} занят или IP {cfg['listen_ip']} "
                                 f"недоступен.\n\n{e}")
            self.server = None
            return

        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

        E.log(f"Argox Emulator started on {cfg['listen_ip']}:{cfg['port']} @ {cfg['dpi']} dpi")
        E.log(f"RAW -> {E.JOBS_DIR}")
        E.log(f"PDF -> {E.PDF_DIR}")
        E.log("Ожидание заданий от драйвера Argox...")

        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.var_status.set(f"● Работает на {cfg['listen_ip']}:{cfg['port']}")
        self.lbl_status.config(foreground="#0a0")

    def stop(self):
        srv = self.server
        if srv is None:
            return
        self.btn_stop.config(state="disabled")

        def _shutdown():
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
            self.log_queue.put(("Сервер остановлен", "INFO"))

        threading.Thread(target=_shutdown, daemon=True).start()
        self.server = None
        self.btn_start.config(state="normal")
        self.var_status.set("● Остановлен")
        self.lbl_status.config(foreground="#b00")

    # ------------------------------------------------------------------
    def _on_log(self, line, level):
        # вызывается из потоков сервера — только кладём в очередь
        self.log_queue.put((line, level))

    def _drain_log(self):
        try:
            while True:
                line, _level = self.log_queue.get_nowait()
                self._append(line)
        except queue.Empty:
            pass
        self.after(150, self._drain_log)

    def _append(self, text):
        self.txt.config(state="normal")
        self.txt.insert("end", text + "\n")
        self.txt.see("end")
        self.txt.config(state="disabled")

    def _clear(self):
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.config(state="disabled")

    def _open(self, path):
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)  # Windows
        except Exception as e:
            messagebox.showerror("Ошибка", str(e))

    def _on_close(self):
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        E.remove_log_listener(self._on_log)
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
