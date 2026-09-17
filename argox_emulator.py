#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Argox 3140 Emulator — сетевой принтер-эмулятор с рендером в PDF.

Что делает:
    1. Поднимает RAW/TCP-порт 9100 (как реальный сетевой принтер Argox).
    2. Принимает задание печати от штатного драйвера Argox.
    3. Сохраняет оригинальный RAW-поток (.bin) — как в argox_capture.py.
    4. Разбирает поток PPLB (диалект EPL2, родной язык Argox 3140):
       текст (A), штрихкоды (B), линии/рамки (LO/LE/X), растровую
       графику (GW) — и рисует каждую этикетку на «холсте» в точках
       принтера (DPI), после чего сохраняет всё задание в один PDF
       (одна этикетка = одна страница).
    5. Если поток не распознан как PPLB/EPL2 — всё равно кладёт в PDF
       страницу с HEX/ASCII-дампом, чтобы «сохранить всё».

Запуск:
    python argox_emulator.py
    python argox_emulator.py --ip 0.0.0.0 --port 9100 --dpi 300

Настройка драйвера Argox на удалённом ПК:
    Добавить принтер -> Standard TCP/IP Port
    IP: <адрес компьютера, где запущен этот скрипт>
    Port: 9100 (Raw)

Результат:
    <папка скрипта>/
        config.json
        Jobs/2026-09-14/000001.bin      (RAW, как раньше)
        PDF/2026-09-14/000001.pdf       (рендер этикеток)
        Logs/2026-09-14.log

Зависимости:
    pip install Pillow python-barcode
    (если их нет — сервер всё равно работает и сохраняет RAW,
     а PDF будет содержать только HEX-дамп.)
"""

import argparse
import datetime
import io
import json
import os
import re
import socket
import socketserver
import sys
import threading

# --------------------------------------------------------------------------
# Опциональные зависимости для рендера
# --------------------------------------------------------------------------
try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except Exception:  # pragma: no cover
    HAVE_PIL = False

try:
    import barcode as _barcode
    from barcode.writer import ImageWriter as _BarcodeImageWriter
    HAVE_BARCODE = True
except Exception:  # pragma: no cover
    HAVE_BARCODE = False

try:
    import win32print
    import win32ui
    import win32con
    import win32gui
    from PIL import ImageWin
    HAVE_WIN32 = True
except Exception:  # pragma: no cover
    HAVE_WIN32 = False

try:
    import qrcode as _qrcode
    HAVE_QR = True
except Exception:  # pragma: no cover
    HAVE_QR = False

try:
    from pystrich.datamatrix import DataMatrixEncoder as _DataMatrixEncoder
    HAVE_DMTX = True
except Exception:  # pragma: no cover
    HAVE_DMTX = False


# --------------------------------------------------------------------------
# Базовые пути и конфигурация
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
JOBS_DIR = os.path.join(BASE_DIR, "Jobs")
PDF_DIR = os.path.join(BASE_DIR, "PDF")
LOGS_DIR = os.path.join(BASE_DIR, "Logs")

DEFAULT_CONFIG = {
    "listen_ip": "192.168.0.185",
    "port": 9100,
    "save_raw": True,
    "save_pdf": True,
    "debug": False,
    # Разрешение принтера. Argox 3140 = 300 dpi (у 203-dpi моделей
    # поставьте 203). Влияет на пересчёт «точки -> миллиметры».
    "dpi": 300,
    # Резервный размер этикетки (мм), если в задании нет команд q/Q.
    "default_label_width_mm": 58.0,
    "default_label_height_mm": 40.0,
    # В команде GW: какое значение бита означает ЧЁРНУЮ точку.
    # По стандарту EPL2 напечатанной (чёрной) точке соответствует 0.
    # Если графика выйдет инвертированной — поставьте 1.
    "graphics_black_bit": 0,
    # Поворот готовой страницы, градусы (0/90/180/270). Драйвер Argox
    # обычно формирует буфер «вверх ногами» (текст в GW уже повёрнут на
    # 180°, штрихкоды идут с rotation=2), поэтому для читаемого PDF нужен
    # разворот на 180. Если ваша этикетка выходит перевёрнутой — 0.
    "rotate_output_deg": 180,
    # Ограничение числа копий, реально выводимых в PDF (P-команда).
    "max_copies_in_pdf": 10,
    # --- Автопересылка на принтер Godex G330 (через драйвер Windows) ---
    # Godex понимает EZPL, а не PPLB/EPL2, поэтому на него отправляется НЕ
    # исходный поток, а готовый растр этикетки, отрисованный эмулятором,
    # через установленный в Windows драйвер Godex (конвертация не нужна).
    "forward_to_printer": False,
    # Режим пересылки на принтер:
    #   "raster" — рендер этикетки картинкой через драйвер Windows (универсально);
    #   "epl"    — «нативный» проброс: слать сами команды Argox (PPLB=EPL2)
    #              напрямую на принтер (RAW). Требует, чтобы Godex был в режиме
    #              эмуляции EPL/GEPL. Штрихкоды/2D рисует прошивка принтера.
    "forward_mode": "raster",
    # Масштаб координат для epl-проброса: источник(Argox)_dpi -> приёмник(Godex)_dpi.
    # Argox 3140 = 300, Godex G300 = 203. Пока для первого теста epl_scale=False —
    # шлём поток как есть (этикетка выйдет ~на 32% мельче, но станет ясно, что
    # нативная печать работает). Масштабирование включим следующим шагом.
    "epl_source_dpi": 300,
    "epl_target_dpi": 203,
    "epl_scale": False,
    # Имя принтера в Windows («Устройства и принтеры»), напр. "Godex G330".
    "printer_name": "",
    # Доп. поворот только для печати (если на Godex выходит перевёрнуто): 0/90/180/270.
    "printer_rotate_deg": 0,
    # Автоматически задавать размер этикетки в драйвере принтера под каждое
    # задание (по командам q/Q). Тогда НЕ нужно вручную менять размер в
    # свойствах Godex при печати разных этикеток. False = брать размер из
    # текущих настроек драйвера.
    "auto_label_size": True,
    # Небольшой допуск к длине этикетки, мм (компенсация зазора/подачи).
    "label_length_margin_mm": 0.0,
    # Idle-timeout и защита от аномального размера — как в capture.
    "idle_timeout_sec": 2.0,
    "max_job_size_bytes": 50 * 1024 * 1024,
}

_job_lock = threading.Lock()
_job_counter = 0

# Слушатели лога (для GUI). Каждый вызывается как fn(line, level).
_log_listeners = []


def add_log_listener(fn):
    if fn not in _log_listeners:
        _log_listeners.append(fn)


def remove_log_listener(fn):
    if fn in _log_listeners:
        _log_listeners.remove(fn)


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
    try:
        print(line, flush=True)
    except Exception:
        pass  # в оконном .exe stdout может отсутствовать
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        log_file = os.path.join(LOGS_DIR, datetime.date.today().isoformat() + ".log")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    for fn in list(_log_listeners):
        try:
            fn(line, level)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Нумерация заданий
# --------------------------------------------------------------------------

def next_job_number():
    global _job_counter
    with _job_lock:
        _job_counter += 1
        return _job_counter


def init_job_counter():
    global _job_counter
    max_n = 0
    for base in (JOBS_DIR, PDF_DIR):
        if os.path.isdir(base):
            for _root, _dirs, files in os.walk(base):
                for fn in files:
                    stem, ext = os.path.splitext(fn)
                    if ext.lower() in (".bin", ".pdf"):
                        try:
                            max_n = max(max_n, int(stem))
                        except ValueError:
                            pass
    _job_counter = max_n


def dir_for_today(base):
    d = os.path.join(base, datetime.date.today().isoformat())
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# Диагностика: HEX/ASCII
# --------------------------------------------------------------------------

def hex_ascii_dump(data: bytes, max_bytes: int = 4096) -> str:
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


# ==========================================================================
# Рендер PPLB / EPL2 -> изображения этикеток
# ==========================================================================

# Базовые размеры точечных шрифтов EPL2 (ширина x высота ячейки в точках).
EPL_FONTS = {
    "1": (8, 12),
    "2": (10, 16),
    "3": (12, 20),
    "4": (14, 24),
    "5": (32, 48),
    # Скалируемые шрифты 'a'..'z' трактуем как крупный шрифт 4.
}

# Соответствие селектора штрихкода EPL2 -> имя в python-barcode.
# (реализованы самые ходовые для товарных этикеток)
EPL_BARCODES = {
    "1": "code128", "1A": "code128", "1B": "code128", "1C": "code128",
    "3": "code39", "3C": "code39",
    "E30": "ean13", "E32": "ean13", "E35": "ean13",
    "E80": "ean8", "E82": "ean8", "E85": "ean8",
    "UA0": "upca",
}


def _load_mono_font(px_height):
    """Пытается взять моноширинный TrueType-шрифт Windows под нужную высоту."""
    px = max(6, int(px_height))
    candidates = [
        r"C:\Windows\Fonts\consola.ttf",
        r"C:\Windows\Fonts\cour.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "DejaVuSansMono.ttf",
        "arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, px)
        except Exception:
            continue
    return ImageFont.load_default()


def _split_epl_params(header: str):
    """Разбивает строку параметров команды на список, учитывая кавычки.
    Возвращает (список_позиционных_параметров, строка_данных_в_кавычках|None)."""
    text = None
    m = re.search(r'"(.*)"\s*$', header, re.DOTALL)
    if m:
        text = m.group(1)
        header = header[:m.start()]
    params = [p.strip() for p in header.split(",") if p.strip() != ""]
    return params, text


def _to_int(val, default=0):
    m = re.search(r"-?\d+", str(val))
    return int(m.group(0)) if m else default


class LabelCanvas:
    """Холст одной этикетки в точках принтера."""

    def __init__(self, width_dots, height_dots):
        self.w = max(1, int(width_dots))
        self.h = max(1, int(height_dots))
        self.img = Image.new("L", (self.w, self.h), 255)  # белый фон
        self.draw = ImageDraw.Draw(self.img)


class EPLRenderer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dpi = int(cfg.get("dpi", 300))
        self.default_w = int(round(cfg["default_label_width_mm"] / 25.4 * self.dpi))
        self.default_h = int(round(cfg["default_label_height_mm"] / 25.4 * self.dpi))
        self.black_bit = int(cfg.get("graphics_black_bit", 0))
        self.max_copies = int(cfg.get("max_copies_in_pdf", 10))
        self.rotate_out = int(cfg.get("rotate_output_deg", 0)) % 360
        self.warnings = []

    def _finalize(self, img):
        """Глобальный разворот готовой страницы под ориентацию Argox."""
        if self.rotate_out:
            img = img.rotate(-self.rotate_out, expand=True)
        return img

    # -- разбор всего задания -------------------------------------------
    def render_job(self, data: bytes):
        """Возвращает список PIL.Image (страницы). Пустой список -> не EPL."""
        self.graphics = {}     # имя -> PIL.Image (из GM, формат PCX)
        self.forms = {}        # имя -> список токенов-команд (для FR)
        st = {
            "cur": None,
            "w": self.default_w,
            "h": self.default_h,
            "recording": None,     # имя формы, которую сейчас записываем
            "recognised": 0,
        }
        pages = []
        for tok in self._tokenize(data):
            self._exec(tok, st, pages)

        # хвостовой буфер без явного P — всё равно выведем
        cur = st["cur"]
        if cur is not None and any(px != 255 for px in cur.img.getdata()):
            pages.append(self._finalize(cur.img))

        if st["recognised"] == 0:
            return []  # это не EPL2 — пусть решает вызывающий код
        return pages

    def _tokenize(self, data):
        """Разбивает поток на команды-токены (сырые байты), корректно
        поглощая бинарные данные GW (растр) и GM (сохранённый PCX)."""
        i = 0
        n = len(data)
        while i < n:
            while i < n and data[i] in (0x0A, 0x0D, 0x00, 0x02, 0x03):
                i += 1
            if i >= n:
                break
            j = i
            while j < n and data[j] not in (0x0A, 0x0D):
                j += 1
            up2 = data[i:i + 2].upper()
            if up2 == b"GW":
                end, _gw = self._read_gw(data, i)
                yield data[i:end]
                i = end
            elif up2 == b"GM":
                end = self._read_gm_end(data, i, j)
                yield data[i:end]
                i = end
            else:
                # Данные в кавычках (команды A/B/b) могут содержать CR/LF —
                # не рвём токен, пока кавычки не закрыты.
                end = j
                if data[i:j].count(b'"') % 2 == 1:
                    k = j
                    while k < n and data[i:k].count(b'"') % 2 == 1:
                        k += 1
                    end = k
                yield data[i:end]
                i = end

    def _read_gm_end(self, data, start, header_end):
        m = re.match(rb'GM"[^"]*"(\d+)', data[start:header_end + 2])
        if not m:
            return header_end
        length = int(m.group(1))
        p = start + m.end()
        if data[p:p + 1] == b"\r":
            p += 1
        if data[p:p + 1] == b"\n":
            p += 1
        return p + length

    def _ensure(self, st):
        if st["cur"] is None:
            st["cur"] = LabelCanvas(st["w"], st["h"])
        return st["cur"]

    @staticmethod
    def _quoted(line):
        m = re.search(r'"([^"]*)"', line)
        return m.group(1) if m else ""

    def _exec(self, tok, st, pages):
        up2 = tok[:2].upper()

        # режим записи формы: всё между FS и FE складываем как есть
        if st["recording"] is not None and up2 not in (b"FE", b"FS"):
            self.forms[st["recording"]].append(tok)
            return

        # бинарные команды
        if up2 == b"GW":
            _end, gw = self._read_gw(tok, 0)
            if gw is not None:
                self._draw_graphic(self._ensure(st), *gw)
                st["recognised"] += 1
            return
        if up2 == b"GM":
            self._store_graphic(tok)
            st["recognised"] += 1
            return

        # текстовые команды
        try:
            line = tok.decode("cp866", errors="replace")
        except Exception:
            line = tok.decode("latin-1", errors="replace")
        line = line.strip("\x00").strip()
        if not line:
            return

        p2 = line[:2].upper()
        cmd = line[0]
        rest = line[1:]

        if p2 == "FS":                       # начать запись формы
            name = self._quoted(line) or "?"
            st["recording"] = name
            self.forms[name] = []
            st["recognised"] += 1
        elif p2 == "FE":                     # конец записи формы
            st["recording"] = None
            st["recognised"] += 1
        elif p2 == "FR":                     # воспроизвести форму
            for t in self.forms.get(self._quoted(line), []):
                self._exec(t, st, pages)
            st["recognised"] += 1
        elif p2 == "FK":                     # удалить форму — игнор
            pass
        elif p2 == "GG":                     # напечатать сохранённую графику
            self._draw_stored_graphic(st, rest[1:])
            st["recognised"] += 1
        elif p2 == "GK":                     # удалить графику — игнор
            pass
        elif p2 in ("LO", "LE", "LW"):
            self._draw_line(self._ensure(st), p2, line[2:])
            st["recognised"] += 1
        elif cmd in ("N", "n"):              # очистка буфера — новая этикетка
            st["cur"] = LabelCanvas(st["w"], st["h"])
            st["recognised"] += 1
        elif cmd == "q":
            st["w"] = _to_int(rest, st["w"])
            if st["cur"] is not None:
                st["cur"] = LabelCanvas(st["w"], st["cur"].h)
            st["recognised"] += 1
        elif cmd == "Q":
            st["h"] = _to_int(rest.split(",")[0], st["h"])
            if st["cur"] is not None:
                st["cur"] = LabelCanvas(st["cur"].w, st["h"])
            st["recognised"] += 1
        elif cmd == "A":
            self._draw_text(self._ensure(st), rest)
            st["recognised"] += 1
        elif cmd == "B":
            self._draw_barcode(self._ensure(st), rest)
            st["recognised"] += 1
        elif cmd == "b":
            self._draw_barcode2d(self._ensure(st), rest)
            st["recognised"] += 1
        elif cmd == "X":
            self._draw_box(self._ensure(st), rest)
            st["recognised"] += 1
        elif cmd in ("P", "p"):
            m = re.match(r"\s*(\d+)", rest)
            copies = min(max(1, int(m.group(1))) if m else 1, self.max_copies)
            if st["cur"] is not None:
                final = self._finalize(st["cur"].img)
                for _ in range(copies):
                    pages.append(final)
                st["cur"] = None
            st["recognised"] += 1
        else:
            pass  # S, D, Z, R, I, O, J, ESC-команды и пр. — пропускаем

    def _store_graphic(self, tok):
        m = re.match(rb'GM"([^"]*)"(\d+)', tok)
        if not m:
            return
        name = m.group(1).decode("latin-1")
        length = int(m.group(2))
        p = m.end()
        if tok[p:p + 1] == b"\r":
            p += 1
        if tok[p:p + 1] == b"\n":
            p += 1
        blob = tok[p:p + length]
        try:
            self.graphics[name] = Image.open(io.BytesIO(blob)).convert("L")
        except Exception as e:
            self.warnings.append(f"GM {name}: {e!r}")

    def _draw_stored_graphic(self, st, rest):
        # формат: GGx,y,"name"
        m = re.match(r'\s*(\d+)\s*,\s*(\d+)\s*,\s*"?([^"]*)"?', rest)
        if not m:
            return
        x, y, name = int(m.group(1)), int(m.group(2)), m.group(3)
        img = self.graphics.get(name)
        if img is None:
            self.warnings.append(f"GG: графика '{name}' не найдена")
            return
        g = img.convert("L")
        mask = Image.eval(g, lambda v: 255 - v)      # чёрные точки -> маска
        black = Image.new("L", g.size, 0)
        self._ensure(st).img.paste(black, (x, y), mask)

    # -- отдельные команды ----------------------------------------------
    def _paste_upright(self, cur, up_img, x, y, rot, opaque=False):
        """Ставит поле (отрисованное «прямо») в точку (x,y) с поворотом rot.
        Точка (x,y) — верхний-левый угол поля при rot=0 (модель EPL2:
        поворот по часовой вокруг опорной точки)."""
        up = up_img.convert("L")
        r = rot % 4
        if r == 0:
            img, px, py = up, x, y
        elif r == 1:            # 90° CW (на реальных данных пока не проверено)
            img = up.rotate(-90, expand=True); px, py = x - img.width, y
        elif r == 2:            # 180°
            img = up.rotate(180, expand=True); px, py = x - img.width, y - img.height
        else:                   # 270° CW (пока не проверено)
            img = up.rotate(90, expand=True); px, py = x, y - img.height
        if opaque:
            cur.img.paste(img, (px, py))
        else:
            mask = Image.eval(img, lambda p: 255 - p)
            black = Image.new("L", img.size, 0)
            cur.img.paste(black, (px, py), mask)

    def _draw_text(self, cur, rest):
        params, text = _split_epl_params(rest)
        if text is None:
            return
        x = _to_int(params[0], 0) if len(params) > 0 else 0
        y = _to_int(params[1], 0) if len(params) > 1 else 0
        rot = _to_int(params[2], 0) if len(params) > 2 else 0
        font_sel = params[3] if len(params) > 3 else "1"
        hmul = _to_int(params[4], 1) if len(params) > 4 else 1
        vmul = _to_int(params[5], 1) if len(params) > 5 else 1
        reverse = (params[6].upper() == "R") if len(params) > 6 else False

        cw, ch = EPL_FONTS.get(str(font_sel), EPL_FONTS["4"])
        px_h = max(6, ch * max(1, vmul))
        font = _load_mono_font(px_h)

        # измеряем
        tmp = Image.new("L", (2, 2), 255)
        d = ImageDraw.Draw(tmp)
        try:
            bbox = d.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            tw, th = len(text) * cw * max(1, hmul), px_h

        # горизонтальный множитель приближаем растяжением по X
        pad = 4
        layer = Image.new("L", (max(1, tw + pad * 2), max(1, th + pad * 2)),
                          0 if reverse else 255)
        ld = ImageDraw.Draw(layer)
        ld.text((pad, pad), text, font=font,
                fill=255 if reverse else 0)
        if hmul and hmul != vmul and hmul >= 1:
            new_w = int(layer.width * (hmul / max(1, vmul)))
            if new_w > 0:
                layer = layer.resize((new_w, layer.height))

        self._paste_upright(cur, layer, x, y, rot, opaque=reverse)

    def _draw_barcode(self, cur, rest):
        params, text = _split_epl_params(rest)
        if text is None:
            return
        x = _to_int(params[0], 0) if len(params) > 0 else 0
        y = _to_int(params[1], 0) if len(params) > 1 else 0
        rot = _to_int(params[2], 0) if len(params) > 2 else 0
        sel = params[3] if len(params) > 3 else "1"
        narrow = _to_int(params[4], 2) if len(params) > 4 else 2
        height = _to_int(params[6], 60) if len(params) > 6 else 60
        human = (params[7].upper() == "B") if len(params) > 7 else False

        img = self._make_barcode_image(sel, text, narrow, height, human)
        if img is None:
            # не смогли — рисуем плейсхолдер-рамку с текстом
            img = Image.new("L", (max(40, len(text) * narrow * 8), max(20, height)), 255)
            dd = ImageDraw.Draw(img)
            dd.rectangle([0, 0, img.width - 1, img.height - 1], outline=0)
            dd.text((3, 3), f"[{sel}] {text}", fill=0, font=_load_mono_font(14))
            self.warnings.append(f"barcode {sel} не отрисован как настоящий")

        self._paste_upright(cur, img, x, y, rot)

    def _make_barcode_image(self, sel, text, narrow, height, human):
        if not HAVE_BARCODE:
            return None
        name = EPL_BARCODES.get(str(sel).upper())
        if name is None:
            # по умолчанию пробуем Code128 для неизвестных линейных
            name = "code128"
        try:
            cls = _barcode.get_barcode_class(name)
        except Exception:
            return None
        module_width_mm = max(0.05, narrow * 25.4 / self.dpi)
        module_height_mm = max(1.0, height * 25.4 / self.dpi)
        writer = _BarcodeImageWriter()
        options = {
            "module_width": module_width_mm,
            "module_height": module_height_mm,
            "quiet_zone": 1.0,
            "write_text": bool(human),
            "font_size": 8,
            "text_distance": 2.0,
            "dpi": self.dpi,
        }
        try:
            # ean/upc требуют корректной длины/контрольной цифры
            obj = cls(text, writer=writer)
            buf = io.BytesIO()
            obj.write(buf, options)
            buf.seek(0)
            img = Image.open(buf).convert("L")
            # python-barcode добавляет поля/место под текст -> обрезаем до
            # реального содержимого, иначе высота не равна EPL-параметру h
            # и штрихкод (при rotation=2) наезжает на соседние элементы.
            inv = Image.eval(img, lambda p: 255 - p)
            bbox = inv.getbbox()
            if bbox:
                img = img.crop(bbox)
            return img
        except Exception as e:
            self.warnings.append(f"barcode {name}('{text}'): {e}")
            return None

    def _draw_barcode2d(self, cur, rest):
        # Команда b: 2D-код.  b x,y,ТИП,опции...,"данные"
        #   ТИП: D = Data Matrix, Q = QR, P = PDF417
        #   опции вида c<кол>,r<ряд>,h<размер модуля в точках>
        params, text = _split_epl_params(rest)
        if text is None:
            return
        x = _to_int(params[0], 0) if len(params) > 0 else 0
        y = _to_int(params[1], 0) if len(params) > 1 else 0
        typ = (params[2].strip().upper()[:1] if len(params) > 2 and params[2].strip()
               else "D")
        # размер модуля (точек): опция h<n>, иначе 4
        cell = 4
        for p in params[3:]:
            p = p.strip().lower()
            if p.startswith("h") and p[1:].isdigit():
                cell = max(2, int(p[1:]))
        img = None
        try:
            if typ == "Q" and HAVE_QR:
                qr = _qrcode.QRCode(border=2, box_size=max(2, cell),
                                    error_correction=_qrcode.constants.ERROR_CORRECT_M)
                qr.add_data(text)
                qr.make(fit=True)
                img = qr.make_image(fill_color="black",
                                    back_color="white").convert("L")
            elif typ in ("D", "") and HAVE_DMTX:
                png = _DataMatrixEncoder(text).get_imagedata(cellsize=max(2, cell))
                img = Image.open(io.BytesIO(png)).convert("L")
            elif typ == "Q" and HAVE_DMTX:  # QR нет — хотя бы Data Matrix
                png = _DataMatrixEncoder(text).get_imagedata(cellsize=max(2, cell))
                img = Image.open(io.BytesIO(png)).convert("L")
                self.warnings.append("QR-движок недоступен, отрисован Data Matrix")
        except Exception as e:
            self.warnings.append(f"2D {typ} ('{text[:20]}...'): {e!r}")
            img = None

        if img is None:
            # движок недоступен — заметный плейсхолдер, чтобы было видно место
            side = max(40, cell * 24)
            img = Image.new("L", (side, side), 255)
            dd = ImageDraw.Draw(img)
            dd.rectangle([0, 0, side - 1, side - 1], outline=0)
            dd.text((4, 4), "2D?", fill=0, font=_load_mono_font(14))
            self.warnings.append(f"2D-код типа {typ} не отрисован (нет движка)")

        gray = img.convert("L")
        mask = Image.eval(gray, lambda p: 255 - p)
        black = Image.new("L", gray.size, 0)
        cur.img.paste(black, (x, y), mask)

    def _draw_line(self, cur, kind, rest):
        parts = rest.split(",")
        if len(parts) < 4:
            return
        x = _to_int(parts[0]); y = _to_int(parts[1])
        w = _to_int(parts[2]); h = _to_int(parts[3])
        color = 255 if kind == "LE" else 0  # LE = стирание (белым)
        cur.draw.rectangle([x, y, x + w - 1, y + h - 1], fill=color)

    def _draw_box(self, cur, rest):
        parts = rest.split(",")
        if len(parts) < 5:
            return
        x1 = _to_int(parts[0]); y1 = _to_int(parts[1])
        t = max(1, _to_int(parts[2], 1))
        x2 = _to_int(parts[3]); y2 = _to_int(parts[4])
        for k in range(t):
            cur.draw.rectangle([x1 + k, y1 + k, x2 - k, y2 - k], outline=0)

    # -- графика GW ------------------------------------------------------
    def _read_gw(self, data, start):
        """Разбирает GWx,y,bytesPerRow,rows<binary>. Возвращает (next_index, tuple|None)."""
        # заголовок = до 4-го числа; данные идут сразу после
        m = re.match(rb"GW\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,?",
                     data[start:start + 64])
        if not m:
            # не смогли — вернём как «строку», сдвинемся до перевода строки
            j = start
            n = len(data)
            while j < n and data[j] not in (0x0A, 0x0D):
                j += 1
            return j, None
        x = int(m.group(1)); y = int(m.group(2))
        bpr = int(m.group(3)); rows = int(m.group(4))
        data_start = start + m.end()
        need = bpr * rows
        blob = data[data_start:data_start + need]
        return data_start + need, (x, y, bpr, rows, blob)

    def _draw_graphic(self, cur, x, y, bpr, rows, blob):
        if len(blob) < bpr * rows:
            blob = blob + b"\x00" * (bpr * rows - len(blob))
        try:
            g = Image.frombytes("1", (bpr * 8, rows), bytes(blob))
        except Exception as e:
            self.warnings.append(f"GW render: {e}")
            return
        g = g.convert("L")
        # В "1"-режиме Pillow: 0->чёрный, 255->белый после convert.
        # EPL2: чёрной точке соответствует бит == graphics_black_bit.
        if self.black_bit == 1:
            g = Image.eval(g, lambda p: 255 - p)
        mask = Image.eval(g, lambda p: 255 - p)  # чёрные точки -> маска 255
        black = Image.new("L", g.size, 0)
        cur.img.paste(black, (x, y), mask)


# --------------------------------------------------------------------------
# Сохранение результата в PDF
# --------------------------------------------------------------------------

def render_hexdump_page(cfg, data: bytes):
    """Страница-заглушка с HEX/ASCII, когда поток не распознан как EPL."""
    font = _load_mono_font(16)
    text = ("НЕ РАСПОЗНАНО как PPLB/EPL2 — сохранён RAW и HEX-дамп.\n\n"
            + hex_ascii_dump(data))
    lines = text.split("\n")
    line_h = 20
    width = 1200
    height = max(400, line_h * (len(lines) + 2))
    img = Image.new("L", (width, height), 255)
    d = ImageDraw.Draw(img)
    yy = 10
    for ln in lines:
        d.text((10, yy), ln, fill=0, font=font)
        yy += line_h
    return img


def save_pdf(cfg, pages, out_path):
    if not pages:
        return False
    dpi = int(cfg.get("dpi", 300))
    rgb_pages = [p.convert("RGB") for p in pages]
    first, rest = rgb_pages[0], rgb_pages[1:]
    first.save(out_path, "PDF", resolution=float(dpi),
               save_all=True, append_images=rest)
    return True


# --------------------------------------------------------------------------
# Печать на принтер Godex G330 через драйвер Windows (GDI)
# --------------------------------------------------------------------------

# GetDeviceCaps индексы
_GDC_LOGPIXELSX = 88

def list_windows_printers():
    """Список имён установленных принтеров Windows (или [] без pywin32)."""
    if not HAVE_WIN32:
        return []
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    try:
        return [p[2] for p in win32print.EnumPrinters(flags)]
    except Exception:
        return []


def _open_print_dc(name, width_mm, length_mm):
    """Открывает DC принтера с заданным размером этикетки (пользовательская
    бумага через DEVMODE). Если размер задать не удалось — обычный DC с
    текущими настройками драйвера."""
    if width_mm and length_mm:
        try:
            hp = win32print.OpenPrinter(name)
            try:
                info = win32print.GetPrinter(hp, 2)
                dm = info.get("pDevMode")
                drv = info.get("pDriverName")
                if dm is not None:
                    dm.PaperSize = getattr(win32con, "DMPAPER_USER", 256)
                    dm.PaperWidth = max(1, int(round(width_mm * 10)))    # 0.1 мм
                    dm.PaperLength = max(1, int(round(length_mm * 10)))
                    dm.Fields |= (win32con.DM_PAPERSIZE
                                  | win32con.DM_PAPERWIDTH
                                  | win32con.DM_PAPERLENGTH)
                    hdc = win32gui.CreateDC(drv, name, dm)
                    return win32ui.CreateDCFromHandle(hdc)
            finally:
                win32print.ClosePrinter(hp)
        except Exception:
            pass  # не вышло — печатаем с текущим размером драйвера
    dc = win32ui.CreateDC()
    dc.CreatePrinterDC(name)
    return dc


def send_raw_to_printer(printer_name, data: bytes):
    """«Нативный» проброс: отправляет байты как есть на принтер (RAW),
    минуя рендер драйвера Windows. Для Godex в режиме эмуляции EPL/GEPL —
    команды Argox (PPLB=EPL2) исполняет прошивка принтера. Возвращает
    число отправленных байт."""
    if not HAVE_WIN32:
        raise RuntimeError("pywin32 не установлен — печать недоступна")
    name = str(printer_name).strip()
    if not name:
        raise RuntimeError("не задано имя принтера (printer_name)")
    h = win32print.OpenPrinter(name)
    try:
        win32print.StartDocPrinter(h, 1, ("Argox EPL passthrough", None, "RAW"))
        try:
            win32print.StartPagePrinter(h)
            win32print.WritePrinter(h, data)
            win32print.EndPagePrinter(h)
        finally:
            win32print.EndDocPrinter(h)
    finally:
        win32print.ClosePrinter(h)
    return len(data)


def print_pages_to_printer(cfg, pages, output_file=None):
    """Печатает готовые растровые этикетки на принтер (драйвер Windows).
    Размер этикетки для каждого задания выставляется автоматически
    (auto_label_size) по командам q/Q — в свойствах Godex ничего менять
    не нужно. output_file — только для отладки. Возвращает число страниц."""
    if not HAVE_WIN32:
        raise RuntimeError("pywin32 не установлен — печать недоступна")
    if not pages:
        return 0
    name = str(cfg.get("printer_name", "")).strip()
    if not name:
        raise RuntimeError("не задано имя принтера (printer_name)")
    src_dpi = int(cfg.get("dpi", 300)) or 300
    rot = int(cfg.get("printer_rotate_deg", 0)) % 360
    auto = bool(cfg.get("auto_label_size", True))
    len_margin = float(cfg.get("label_length_margin_mm", 0.0))

    # готовим страницы: поворот + ч/б + физический размер (мм)
    prepared = []
    for page in pages:
        im = page.rotate(-rot, expand=True) if rot else page
        im = im.convert("1")            # чёткий чёрно-белый для термопечати
        w_mm = round(im.width / float(src_dpi) * 25.4, 1)
        l_mm = round(im.height / float(src_dpi) * 25.4 + len_margin, 1)
        prepared.append((im, w_mm, l_mm))

    # группируем подряд идущие этикетки одинакового размера -> один документ
    groups = []
    for item in prepared:
        size = (item[1], item[2])
        if groups and groups[-1][0] == size:
            groups[-1][1].append(item[0])
        else:
            groups.append((size, [item[0]]))

    count = 0
    for (w_mm, l_mm), imgs in groups:
        dc = _open_print_dc(name, w_mm if auto else 0, l_mm if auto else 0)
        try:
            pdpi = dc.GetDeviceCaps(_GDC_LOGPIXELSX) or src_dpi
            scale = pdpi / float(src_dpi)
            if output_file:
                dc.StartDoc("Argox -> Godex", output_file)
            else:
                dc.StartDoc("Argox -> Godex")
            for im in imgs:
                w = max(1, int(round(im.width * scale)))
                h = max(1, int(round(im.height * scale)))
                dc.StartPage()
                ImageWin.Dib(im).draw(dc.GetHandleOutput(), (0, 0, w, h))
                dc.EndPage()
                count += 1
            dc.EndDoc()
        finally:
            dc.DeleteDC()
    return count


# --------------------------------------------------------------------------
# TCP-сервер
# --------------------------------------------------------------------------

class EmulatorHandler(socketserver.BaseRequestHandler):
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
                break
            except (ConnectionResetError, ConnectionAbortedError):
                break
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > max_size:
                log(f"Job exceeds max_job_size_bytes ({max_size}), aborting", "ERROR")
                break

        log(f"Disconnected: {client_ip}")
        if not buf:
            log("No data received, ignoring empty connection", "WARNING")
            return

        data = bytes(buf)
        job_no = next_job_number()
        job_id = f"{job_no:06d}"
        log(f"Received {len(data)} bytes")
        log(f"Job #{job_id} created")

        if cfg["save_raw"]:
            raw_path = os.path.join(dir_for_today(JOBS_DIR), f"{job_id}.bin")
            with open(raw_path, "wb") as f:
                f.write(data)
            log(f"Saved RAW -> {raw_path}")

        if cfg["debug"]:
            log("---- HEX/ASCII dump ----", "DEBUG")
            for line in hex_ascii_dump(data, 1024).splitlines():
                log(line, "DEBUG")
            log("------------------------", "DEBUG")

        forward = cfg.get("forward_to_printer", False)
        mode = cfg.get("forward_mode", "raster")

        # Рендер нужен для PDF и для растровой печати; для epl-проброса — нет.
        need_render = cfg.get("save_pdf", True) or (forward and mode != "epl")
        label_pages = None
        if need_render:
            label_pages = self._render_and_savepdf(cfg, data, job_id)

        if forward:
            self._forward(cfg, data, job_id, label_pages)

        log(f"Job #{job_id} done")

    def _render_and_savepdf(self, cfg, data, job_id):
        """Отрисовывает задание, сохраняет PDF, возвращает список этикеток (или None)."""
        if not HAVE_PIL:
            log("Pillow не установлен — рендер пропущен (pip install Pillow python-barcode)",
                "WARNING")
            return None
        try:
            renderer = EPLRenderer(cfg)
            label_pages = renderer.render_job(data)   # реальные этикетки или []
            if label_pages:
                log(f"Отрисовано этикеток: {len(label_pages)}")
                for w in renderer.warnings[:10]:
                    log("render: " + w, "WARNING")
        except Exception as e:
            log(f"Ошибка рендера: {e!r}", "ERROR")
            return None

        if cfg.get("save_pdf", True):
            try:
                pdf_pages = label_pages or [render_hexdump_page(cfg, data)]
                pdf_path = os.path.join(dir_for_today(PDF_DIR), f"{job_id}.pdf")
                save_pdf(cfg, pdf_pages, pdf_path)
                log(f"Saved PDF -> {pdf_path}")
            except Exception as e:
                log(f"Ошибка сохранения PDF: {e!r}", "ERROR")
        return label_pages

    def _forward(self, cfg, data, job_id, label_pages):
        name = str(cfg.get("printer_name", "")).strip()
        if not name:
            log("Печать пропущена: не выбран принтер", "WARNING")
            return
        mode = cfg.get("forward_mode", "raster")

        if mode == "epl":
            # «Нативный» проброс: команды Argox (PPLB=EPL2) прямо на принтер.
            payload = data
            if cfg.get("epl_scale", False):
                log("epl_scale включён, но масштабирование ещё не реализовано — "
                    "шлю поток как есть (этикетка может выйти мельче)", "WARNING")
            try:
                n = send_raw_to_printer(name, payload)
                log(f"Отправлено на «{name}» нативно (EPL RAW): {n} байт. "
                    "Убедитесь, что принтер в режиме эмуляции EPL/GEPL.")
            except Exception as e:
                log(f"Нативная печать (EPL) не удалась: {e!r}", "ERROR")
            return

        # Растровый режим (через драйвер Windows)
        if not label_pages:
            log("Печать на Godex пропущена: поток не распознан как этикетка", "WARNING")
            return
        try:
            if cfg.get("auto_label_size", True):
                dpi = int(cfg.get("dpi", 300)) or 300
                p0 = label_pages[0]
                log(f"Размер этикетки: {p0.width / dpi * 25.4:.1f}×"
                    f"{p0.height / dpi * 25.4:.1f} мм (авто)")
            n = print_pages_to_printer(cfg, label_pages)
            log(f"Отправлено на принтер «{name}»: {n} этикет.")
        except Exception as e:
            log(f"Печать на Godex не удалась: {e!r}", "ERROR")


class ThreadingTCPServerReuse(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def create_server(cfg):
    """Создаёт (но НЕ запускает) сервер. Для GUI: serve_forever в потоке."""
    os.makedirs(JOBS_DIR, exist_ok=True)
    os.makedirs(PDF_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    init_job_counter()
    server = ThreadingTCPServerReuse((cfg["listen_ip"], cfg["port"]), EmulatorHandler)
    server.cfg = cfg
    return server


def run_server(cfg):
    server = create_server(cfg)

    log(f"Argox Emulator started on {cfg['listen_ip']}:{cfg['port']} @ {cfg['dpi']} dpi")
    log(f"RAW -> {JOBS_DIR}")
    log(f"PDF -> {PDF_DIR}")
    if not HAVE_PIL:
        log("ВНИМАНИЕ: Pillow не найден — PDF не будет создаваться "
            "(pip install Pillow python-barcode)", "WARNING")
    elif not HAVE_BARCODE:
        log("python-barcode не найден — штрихкоды будут плейсхолдерами "
            "(pip install python-barcode)", "WARNING")
    if cfg.get("forward_to_printer"):
        if not HAVE_WIN32:
            log("Печать на Godex включена, но pywin32 не найден "
                "(pip install pywin32)", "WARNING")
        else:
            log(f"Автопечать на Godex: «{cfg.get('printer_name', '')}»")
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
    parser = argparse.ArgumentParser(description="Argox 3140 Emulator -> PDF")
    parser.add_argument("--ip", help="IP для прослушивания")
    parser.add_argument("--port", type=int, help="TCP порт")
    parser.add_argument("--dpi", type=int, help="DPI принтера (203 или 300)")
    parser.add_argument("--debug", action="store_true", help="HEX/ASCII в лог")
    parser.add_argument("--render", metavar="FILE.bin",
                        help="Не поднимать сервер: отрисовать готовый RAW-дамп в PDF")
    args = parser.parse_args()

    cfg = load_config()
    if args.ip:
        cfg["listen_ip"] = args.ip
    if args.port:
        cfg["port"] = args.port
    if args.dpi:
        cfg["dpi"] = args.dpi
    if args.debug:
        cfg["debug"] = True

    if args.render:
        # Оффлайн-режим: перегнать ранее захваченный .bin в PDF.
        with open(args.render, "rb") as f:
            data = f.read()
        renderer = EPLRenderer(cfg)
        pages = renderer.render_job(data) or [render_hexdump_page(cfg, data)]
        out = os.path.splitext(args.render)[0] + ".pdf"
        save_pdf(cfg, pages, out)
        print(f"PDF: {out}  (страниц: {len(pages)})")
        return

    run_server(cfg)


if __name__ == "__main__":
    main()
