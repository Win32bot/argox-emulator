#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Argox 3140 Emulator — диагностический TCP-сервер (этап 1: захват RAW).

Назначение:
    Эмулирует сетевой порт принтера Argox 3140 (RAW / TCP 9100),
    принимает задания печати от штатного драйвера Argox, сохраняет
    оригинальный RAW-поток на диск, выводит HEX/ASCII для анализа
    и ведёт журнал событий.

    Это первый рабочий этап согласно ТЗ (п.6 "Диагностический режим"
    и п.31 "Важное техническое условие") — сбор реальных дампов от
    установленного драйвера Argox 3140, прежде чем писать полноценный
    парсер PPLB и конвертер в GoDEX.

Запуск:
    python argox_capture.py
    python argox_capture.py --ip 0.0.0.0 --port 9100

Настройка на удалённом ПК (где стоит драйвер Argox):
    Добавить принтер -> Standard TCP/IP Port
    IP: <адрес компьютера, где запущен этот скрипт>
    Port: 9100 (Raw, port number 9100)

Результат работы:
    ArgoxCapture/
        config.json
        Jobs/
            2026-09-09/
                000001.bin
                000002.bin
        Logs/
            2026-09-09.log
"""

import argparse
import datetime
import json
import os
import socket
import socketserver
import sys
import threading
import time

# --------------------------------------------------------------------------
# Базовые пути и конфигурация
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
JOBS_DIR = os.path.join(BASE_DIR, "Jobs")
LOGS_DIR = os.path.join(BASE_DIR, "Logs")

DEFAULT_CONFIG = {
    "listen_ip": "192.168.0.185",
    "port": 9100,
    "save_raw": True,
    "debug": False,
    # Сколько секунд ждать без новых данных, прежде чем считать
    # задание завершённым (драйверы Argox обычно шлют задание одним
    # потоком и закрывают соединение, но на некоторых стеках сокет
    # может оставаться открытым — поэтому используем idle-timeout).
    "idle_timeout_sec": 2.0,
    # Максимальный размер одного задания (защита от аномальных потоков).
    "max_job_size_bytes": 50 * 1024 * 1024,
}

_job_lock = threading.Lock()
_job_counter = 0


def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


# --------------------------------------------------------------------------
# Логирование
# --------------------------------------------------------------------------

def log(msg, level="INFO"):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"{ts} [{level}] {msg}"
    print(line, flush=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    log_file = os.path.join(LOGS_DIR, datetime.date.today().isoformat() + ".log")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# --------------------------------------------------------------------------
# Определение номера задания и путей сохранения
# --------------------------------------------------------------------------

def next_job_number():
    global _job_counter
    with _job_lock:
        _job_counter += 1
        return _job_counter


def init_job_counter():
    """При старте находит максимальный уже сохранённый номер задания,
    чтобы нумерация продолжалась, а не начиналась заново."""
    global _job_counter
    max_n = 0
    if os.path.isdir(JOBS_DIR):
        for root, _dirs, files in os.walk(JOBS_DIR):
            for fn in files:
                if fn.endswith(".bin"):
                    try:
                        n = int(os.path.splitext(fn)[0])
                        max_n = max(max_n, n)
                    except ValueError:
                        pass
    _job_counter = max_n


def job_dir_for_today():
    d = os.path.join(JOBS_DIR, datetime.date.today().isoformat())
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# Грубое определение протокола (эвристика для первого этапа)
# --------------------------------------------------------------------------

def detect_protocol(data: bytes) -> str:
    """Очень грубая эвристика для быстрой ориентировки в логе.
    Точное определение появится после анализа реальных дампов (см. п.31)."""
    if not data:
        return "EMPTY"
    head = data[:64]
    # PPLB задания Argox обычно начинаются с STX (0x02) и команды типа "A0001..."
    if head[:1] == b"\x02":
        return "PPLB (предположительно, STX-префикс)"
    if head.startswith(b"\x1b"):
        return "PPLA (предположительно, ESC-префикс)"
    if b"^XA" in head:
        return "ZPL-подобный (не Argox)"
    printable = sum(1 for b in head if 32 <= b <= 126)
    if printable / max(len(head), 1) > 0.8:
        return "TEXT/ASCII-подобный (уточнить вручную)"
    return "UNKNOWN"


def hex_ascii_dump(data: bytes, max_bytes: int = 512) -> str:
    chunk = data[:max_bytes]
    lines = []
    for i in range(0, len(chunk), 16):
        row = chunk[i:i + 16]
        hex_part = " ".join(f"{b:02X}" for b in row)
        ascii_part = "".join(chr(b) if 32 <= b <= 126 else "." for b in row)
        lines.append(f"{i:06X}  {hex_part:<47}  {ascii_part}")
    suffix = ""
    if len(data) > max_bytes:
        suffix = f"\n... (показаны первые {max_bytes} из {len(data)} байт)"
    return "\n".join(lines) + suffix


# --------------------------------------------------------------------------
# TCP-сервер
# --------------------------------------------------------------------------

class ArgoxCaptureHandler(socketserver.BaseRequestHandler):
    def handle(self):
        cfg = self.server.cfg
        client_ip = self.client_address[0]
        log(f"TCP connection from {client_ip}")

        self.request.settimeout(cfg["idle_timeout_sec"])
        buf = bytearray()
        max_size = cfg["max_job_size_bytes"]

        log("Receiving data...")
        while True:
            try:
                chunk = self.request.recv(65536)
            except socket.timeout:
                # Нет новых данных в течение idle_timeout — считаем,
                # что задание завершено (драйвер обычно держит сокет
                # открытым короткое время после отправки данных).
                break
            except (ConnectionResetError, ConnectionAbortedError):
                break

            if not chunk:
                # Клиент закрыл соединение — задание завершено.
                break

            buf.extend(chunk)
            if len(buf) > max_size:
                log(f"Job exceeds max_job_size_bytes ({max_size}), aborting", "ERROR")
                break

        log(f"Disconnected: {client_ip}")

        if not buf:
            log("No data received, ignoring empty connection", "WARNING")
            return

        job_no = next_job_number()
        job_id = f"{job_no:06d}"
        size = len(buf)
        log(f"Received {size} bytes")
        log(f"Job #{job_id} created")

        proto = detect_protocol(bytes(buf))
        log(f"Protocol: {proto}")

        if cfg["save_raw"]:
            path = os.path.join(job_dir_for_today(), f"{job_id}.bin")
            with open(path, "wb") as f:
                f.write(buf)
            log(f"Saved RAW -> {path}")

        if cfg["debug"]:
            log("---- HEX/ASCII dump ----", "DEBUG")
            for line in hex_ascii_dump(bytes(buf)).splitlines():
                log(line, "DEBUG")
            log("------------------------", "DEBUG")

        log(f"Job #{job_id} stored, ready for parsing (parser not yet implemented)")


class ThreadingTCPServerReuse(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_server(cfg):
    os.makedirs(JOBS_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    init_job_counter()

    server = ThreadingTCPServerReuse((cfg["listen_ip"], cfg["port"]), ArgoxCaptureHandler)
    server.cfg = cfg

    log(f"Argox Capture Server started on {cfg['listen_ip']}:{cfg['port']}")
    log(f"Jobs directory: {JOBS_DIR}")
    log(f"Save RAW: {cfg['save_raw']}, Debug: {cfg['debug']}")
    log("On the remote PC configure Argox driver port as:")
    log("  Standard TCP/IP Port, Protocol: RAW, Port: %s, IP: <this machine>" % cfg["port"])
    log("Waiting for connections... (Ctrl+C to stop)")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Stopping server (Ctrl+C)...")
    finally:
        server.shutdown()
        server.server_close()
        log("Server stopped")


def main():
    parser = argparse.ArgumentParser(description="Argox 3140 Emulator - RAW capture stage")
    parser.add_argument("--ip", help="IP для прослушивания (по умолчанию из config.json)")
    parser.add_argument("--port", type=int, help="TCP порт (по умолчанию из config.json)")
    parser.add_argument("--debug", action="store_true", help="Включить HEX/ASCII дамп в лог")
    args = parser.parse_args()

    cfg = load_config()
    if args.ip:
        cfg["listen_ip"] = args.ip
    if args.port:
        cfg["port"] = args.port
    if args.debug:
        cfg["debug"] = True

    run_server(cfg)


if __name__ == "__main__":
    main()
