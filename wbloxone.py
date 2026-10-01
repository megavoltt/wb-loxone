#!/usr/bin/env python3
"""
WB → Loxone: мост устройств Wiren Board в Loxone Miniserver.

Читает дерево MQTT контроллера (/devices/#), даёт в веб-интерфейсе выбрать
каналы и формирует шаблоны для импорта в Loxone Config:

  * VIU_*.xml — виртуальный вход UDP: мост шлёт на Miniserver строки
    `wb.<устройство>.<канал>=<значение>` при каждом изменении;
  * VO_*.xml  — виртуальный выход HTTP: Miniserver вызывает
    `http://<wb>:<порт>/set/<ключ>/<устройство>/<канал>/<значение>`,
    мост публикует значение в `/devices/<устройство>/controls/<канал>/on`.

Только стандартная библиотека Python 3.9+ — на контроллер ничего не ставится.
"""
import json
import os
import re
import secrets
import socket
import struct
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.3.0"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("WBLOX_CONFIG", "/mnt/data/etc/wb-loxone.json")

DEFAULTS = {
    "miniserver": "",          # IP Miniserver, куда слать UDP
    "udp_port": 7001,          # порт виртуального входа UDP в Loxone
    "http_port": 8099,         # порт этого веб-интерфейса и команд
    "wb_host": "",             # адрес WB для шаблона VO; пусто — определить самому
    "key": "",                 # ключ доступа в URL команд
    "title": "WirenBoard",     # имя блоков в Loxone Config
    "throttle_ms": 500,        # не чаще раза в N мс на канал (кроме switch/alarm)
    "resync_s": 300,           # полная пересылка значений раз в N секунд (0 — выкл.)
    "click_ms": 400,           # пауза после нажатия, за которую ждём следующее (двойное/тройное)
    "long_ms": 600,            # удержание дольше — долгое нажатие
    "pulse_ms": 150,           # длительность импульса события для Loxone
    "button_log": False,       # подробный журнал нажатий/отпусканий (для отладки задержек)
    "ui_password": "",         # пароль веб-интерфейса (пусто — без пароля)
    "selected": {},            # "устройство/канал" -> true
    "aliases": {},             # устройство -> имя для Loxone
}

# Устройства, которые обычно не нужны в Loxone — в интерфейсе свёрнуты внизу
SYSTEM_DEVICES = {
    "system", "network", "hwmon", "power_status", "buzzer", "alarms", "wbrules",
    "metrics", "wb-adc", "wb-gpio", "wbrules_hidden", "wb-mqtt-serial", "battery",
    "knx", "dali", "wb-mqtt-logs", "wb-mqtt-db", "wb-mqtt-confed", "ups",
}

# Устаревшие типы WB → единицы измерения для Loxone
TYPE_UNITS = {
    "temperature": "°C", "rel_humidity": "%", "atmospheric_pressure": "мбар",
    "rainfall": "мм/ч", "wind_speed": "м/с", "power": "Вт", "power_consumption": "кВт·ч",
    "voltage": "В", "water_flow": "м³/ч", "water_consumption": "м³", "resistance": "Ом",
    "concentration": "ppm", "heat_power": "Гкал/ч", "heat_energy": "Гкал",
    "current": "А", "pressure": "бар", "lux": "лк", "illuminance": "лк",
    "sound_level": "дБ", "pm1": "мкг/м³", "pm25": "мкг/м³", "pm10": "мкг/м³",
}
UNIT_RU = {
    "deg C": "°C", "%, RH": "%", "%": "%", "W": "Вт", "kW": "кВт", "kWh": "кВт·ч",
    "V": "В", "mV": "мВ", "A": "А", "mA": "мА", "Hz": "Гц", "lx": "лк", "ppm": "ppm",
    "ppb": "ppb", "dB": "дБ", "bar": "бар", "mbar": "мбар", "Pa": "Па", "s": "с",
    "ms": "мс", "min": "мин", "h": "ч", "m^3": "м³", "m^3/h": "м³/ч", "Ohm": "Ом",
    "mOhm": "мОм", "VA": "ВА", "var": "вар", "kVAh": "кВА·ч", "kvarh": "квар·ч",
    "deg": "°", "rpm": "об/мин", "ug/m^3": "мкг/м³", "m/s": "м/с", "mm/h": "мм/ч",
    "W/m^2": "Вт/м²", "kJ": "кДж", "kcal": "ккал", "Gcal": "Гкал",
}
WRITABLE_BY_DEFAULT = {"switch", "pushbutton", "range", "rgb"}
BINARY_TYPES = {"switch", "alarm", "pushbutton", "event"}

log_lock = threading.Lock()


def log(*a):
    with log_lock:
        t = time.time()
        print(time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d" % (t % 1 * 1000), *a, flush=True)


# --------------------------------------------------------------------------- config

class Config:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = dict(DEFAULTS)
        try:
            with open(path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception as e:
            log("Не прочитал конфиг", path, e)
        if not self.data["key"]:
            self.data["key"] = secrets.token_hex(4)
            self.save()

    def __getitem__(self, k):
        return self.data[k]

    def update(self, patch):
        with self.lock:
            for k, v in patch.items():
                if k in DEFAULTS:
                    self.data[k] = v
        self.save()

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- MQTT

class MiniMqtt:
    """Клиент MQTT 3.1.1, QoS 0: ровно то, что нужно мосту, без paho."""

    def __init__(self, host, port, on_message, on_connect):
        self.host, self.port = host, port
        self.on_message, self.on_connect = on_message, on_connect
        self.sock = None
        self.wlock = threading.Lock()
        self.connected = False
        self.pid = 1

    def _send(self, data):
        with self.wlock:
            if self.sock is None:
                raise OSError("нет соединения")
            self.sock.sendall(data)

    @staticmethod
    def _rl(n):
        out = bytearray()
        while True:
            b = n % 128
            n //= 128
            out.append(b | (0x80 if n else 0))
            if not n:
                return bytes(out)

    @staticmethod
    def _str(s):
        b = s.encode("utf-8")
        return struct.pack("!H", len(b)) + b

    def publish(self, topic, payload, retain=False):
        body = self._str(topic) + payload.encode("utf-8")
        self._send(bytes([0x30 | (1 if retain else 0)]) + self._rl(len(body)) + body)

    def subscribe(self, topic):
        self.pid = self.pid % 65535 + 1
        body = struct.pack("!H", self.pid) + self._str(topic) + b"\x00"
        self._send(b"\x82" + self._rl(len(body)) + body)

    def _recv_exact(self, n):
        # Через буферизованный файл: recv по одному байту съедал целое ядро ARM
        data = self.rf.read(n)
        if len(data) < n:
            raise OSError("брокер закрыл соединение")
        return data

    def _read_packet(self):
        hdr = self._recv_exact(1)[0]
        mult, length = 1, 0
        while True:
            b = self._recv_exact(1)[0]
            length += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        return hdr, self._recv_exact(length) if length else b""

    def run_forever(self):
        while True:
            try:
                self._session()
            except Exception as e:
                log("MQTT:", e)
            self.connected = False
            with self.wlock:
                try:
                    self.sock and self.sock.close()
                except OSError:
                    pass
                self.sock = None
            time.sleep(3)

    def _session(self):
        s = socket.create_connection((self.host, self.port), timeout=10)
        s.settimeout(None)
        self.sock = s
        self.rf = s.makefile("rb", buffering=65536)
        cid = "wb-loxone-%s" % secrets.token_hex(3)
        body = self._str("MQTT") + bytes([4, 0x02]) + struct.pack("!H", 60) + self._str(cid)
        self._send(b"\x10" + self._rl(len(body)) + body)
        hdr, data = self._read_packet()
        if hdr >> 4 != 2 or data[1] != 0:
            raise OSError("брокер отклонил подключение")
        self.connected = True
        log("MQTT: подключён к %s:%d" % (self.host, self.port))
        stop = threading.Event()

        def pinger():
            while not stop.wait(30):
                try:
                    self._send(b"\xc0\x00")
                except OSError:
                    return
        threading.Thread(target=pinger, daemon=True).start()
        try:
            self.on_connect(self)
            while True:
                hdr, data = self._read_packet()
                if hdr >> 4 == 3:
                    qos = (hdr >> 1) & 3
                    tl = struct.unpack("!H", data[:2])[0]
                    topic = data[2:2 + tl].decode("utf-8", "replace")
                    pos = 2 + tl
                    if qos:
                        pid = data[pos:pos + 2]
                        pos += 2
                        if qos == 1:
                            self._send(b"\x40\x02" + pid)
                    try:
                        self.on_message(topic, data[pos:].decode("utf-8", "replace"))
                    except Exception as e:
                        log("Ошибка обработки", topic, e)
        finally:
            stop.set()


# --------------------------------------------------------------------------- модель WB

TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p",
                     "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def safe_id(s):
    """Имя для строки UDP и Check в Loxone: латиница без пробелов и спецзнаков."""
    s = "".join(TRANSLIT.get(ch, ch) if ch.islower() else
                TRANSLIT.get(ch.lower(), ch).capitalize() for ch in s)
    return re.sub(r"[^A-Za-z0-9_\-]", "_", s)


def title_of(meta, fallback):
    t = meta.get("title")
    if isinstance(t, dict):
        return t.get("ru") or t.get("en") or next((v for v in t.values() if v), None) or fallback
    if isinstance(t, str) and t:
        return t
    return meta.get("name") or fallback


class Model:
    def __init__(self):
        self.lock = threading.Lock()
        self.devices = {}   # dev -> {"meta": {}, "controls": {ctrl: {"meta": {}, "value": str, "ts": float}}}
        self.listeners = []

    def dev(self, d):
        return self.devices.setdefault(d, {"meta": {}, "controls": {}})

    def ctl(self, d, c):
        return self.dev(d)["controls"].setdefault(c, {"meta": {}, "value": None, "ts": 0})

    def handle(self, topic, payload):
        p = topic.split("/")
        # ['', 'devices', dev, ...]
        if len(p) < 4 or p[1] != "devices":
            return
        d = p[2]
        with self.lock:
            if p[3] == "meta":
                m = self.dev(d)["meta"]
                if len(p) == 4:
                    try:
                        m.update(json.loads(payload) if payload else {})
                    except ValueError:
                        pass
                elif len(p) == 5:
                    m[p[4]] = payload
                return
            if p[3] != "controls" or len(p) < 5:
                return
            c = p[4]
            if len(p) == 5:
                if payload == "" and c in self.dev(d)["controls"]:
                    # пустое retained-сообщение — канал удалён
                    self.dev(d)["controls"][c]["value"] = None
                    return
                ctl = self.ctl(d, c)
                changed = ctl["value"] != payload
                ctl["value"], ctl["ts"] = payload, time.time()
            elif p[5] == "meta":
                m = self.ctl(d, c)["meta"]
                if len(p) == 6:
                    try:
                        m.update(json.loads(payload) if payload else {})
                    except ValueError:
                        pass
                else:
                    m[p[6]] = payload
                return
            else:
                return
        if changed:
            for fn in self.listeners:
                fn(d, c, payload)

    @staticmethod
    def ctype(meta):
        return meta.get("type") or "value"

    @classmethod
    def writable(cls, meta):
        ro = meta.get("readonly")
        if ro in (True, "1", "true", 1):
            return False
        if ro in (False, "0", "false", 0):
            return True
        return cls.ctype(meta) in WRITABLE_BY_DEFAULT

    @classmethod
    def readable(cls, meta):
        return cls.ctype(meta) != "pushbutton"

    @classmethod
    def unit(cls, meta):
        u = meta.get("units")
        if u:
            return UNIT_RU.get(u, u)
        return TYPE_UNITS.get(cls.ctype(meta), "")

    @staticmethod
    def precision(meta, value):
        p = meta.get("precision")
        try:
            if p not in (None, ""):
                p = float(p)
                return 0 if p >= 1 else min(3, len(("%g" % p).split(".")[-1]))
        except ValueError:
            pass
        if value and "." in value:
            return min(3, len(value.split(".")[-1]))
        return 0

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.devices))


# --------------------------------------------------------------------------- значения для Loxone

def to_loxone(ctype, value):
    """Значение WB → число для Loxone; None — отправлять нечего."""
    if value is None or value == "":
        return None
    if ctype == "rgb":
        # "R;G;B" 0..255 → формат Loxone BBBGGGRRR в процентах
        try:
            r, g, b = (int(round(int(x) * 100 / 255)) for x in value.split(";"))
            return str(b * 1000000 + g * 1000 + r)
        except ValueError:
            return None
    v = value.strip()
    if v.lower() in ("true", "on"):
        return "1"
    if v.lower() in ("false", "off"):
        return "0"
    try:
        float(v)
        return v
    except ValueError:
        return None


def from_loxone(meta, raw):
    """Значение из URL Loxone → полезная нагрузка для /on."""
    t = Model.ctype(meta)
    raw = raw.strip().replace(",", ".")
    if t == "rgb":
        # Loxone шлёт BBBGGGRRR (проценты); бывает и готовое "r;g;b"
        if ";" in raw:
            return raw
        try:
            n = int(float(raw))
        except ValueError:
            return None
        if n >= 200000000:  # формат температуры белого Loxone — не поддерживается
            return None
        r, g, b = n % 1000, n // 1000 % 1000, n // 1000000 % 1000
        return ";".join(str(int(round(min(100, x) * 255 / 100))) for x in (r, g, b))
    try:
        f = float(raw)
    except ValueError:
        return raw if t == "text" else None
    if t in BINARY_TYPES:
        return "1" if f != 0 else "0"
    mx = meta.get("max")
    try:
        if mx not in (None, ""):
            f = min(f, float(mx))
        mn = meta.get("min")
        if mn not in (None, ""):
            f = max(f, float(mn))
    except ValueError:
        pass
    return str(int(f)) if f == int(f) else ("%.3f" % f).rstrip("0")


# --------------------------------------------------------------------------- мост

class Bridge:
    def __init__(self, cfg, model):
        self.cfg, self.model = cfg, model
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.last_sent = {}      # key -> (time, value)
        self.pending = {}        # key -> value, ждёт окна троттлинга
        self.plock = threading.Lock()
        self.stats = {"udp": 0, "cmd": 0, "errors": 0, "last_cmd": ""}
        self.mqtt = None
        model.listeners.append(self.on_change)

    def selected(self, d, c):
        return bool(self.cfg["selected"].get(d + "/" + c))

    def on_change(self, d, c, value):
        if not self.selected(d, c):
            return
        meta = self.model.devices[d]["controls"][c]["meta"]
        t = Model.ctype(meta)
        if not Model.readable(meta):
            return
        v = to_loxone(t, value)
        if v is None:
            return
        key = d + "/" + c
        thr = 0 if t in BINARY_TYPES else self.cfg["throttle_ms"] / 1000.0
        now = time.time()
        with self.plock:
            last = self.last_sent.get(key, (0, None))[0]
            if now - last >= thr:
                self.last_sent[key] = (now, v)
                self.pending.pop(key, None)
            else:
                self.pending[key] = v
                return
        self.send(d, c, v)

    def send(self, d, c, v):
        ms = self.cfg["miniserver"]
        if not ms:
            return
        line = "wb.%s.%s=%s" % (safe_id(d), safe_id(c), v)
        try:
            self.udp.sendto(line.encode("utf-8"), (ms, int(self.cfg["udp_port"])))
            self.stats["udp"] += 1
        except OSError as e:
            self.stats["errors"] += 1
            log("UDP:", e)

    def flusher(self):
        while True:
            time.sleep(0.1)
            thr = self.cfg["throttle_ms"] / 1000.0
            now = time.time()
            due = []
            with self.plock:
                for key, v in list(self.pending.items()):
                    if now - self.last_sent.get(key, (0, None))[0] >= thr:
                        due.append((key, v))
                        del self.pending[key]
                        self.last_sent[key] = (now, v)
            for key, v in due:
                d, c = key.split("/", 1)
                self.send(d, c, v)

    def resync(self):
        """Переслать текущие значения всех выбранных каналов (после перезагрузки Miniserver)."""
        n = 0
        with self.model.lock:
            items = [(d, c, x["meta"], x["value"]) for d, dv in self.model.devices.items()
                     for c, x in dv["controls"].items()]
        for d, c, meta, value in items:
            if self.selected(d, c) and Model.readable(meta):
                v = to_loxone(Model.ctype(meta), value)
                if v is not None:
                    self.send(d, c, v)
                    n += 1
                    time.sleep(0.005)
        return n

    def resyncer(self):
        while True:
            time.sleep(5)
            period = int(self.cfg["resync_s"] or 0)
            if period > 0 and time.time() - getattr(self, "_last_resync", 0) >= period:
                self._last_resync = time.time()
                if self.mqtt and self.mqtt.connected:
                    self.resync()

    def command(self, d, c, raw):
        with self.model.lock:
            ctl = self.model.devices.get(d, {}).get("controls", {}).get(c)
            meta = dict(ctl["meta"]) if ctl else None
        if meta is None:
            return 404, "нет такого канала"
        if not self.selected(d, c):
            return 403, "канал не выбран для Loxone"
        if not Model.writable(meta):
            return 403, "канал только для чтения"
        payload = from_loxone(meta, raw)
        if payload is None:
            return 400, "не понял значение"
        try:
            self.mqtt.publish("/devices/%s/controls/%s/on" % (d, c), payload)
        except OSError as e:
            return 503, "MQTT: %s" % e
        self.stats["cmd"] += 1
        self.stats["last_cmd"] = "%s %s/%s = %s" % (time.strftime("%H:%M:%S"), d, c, payload)
        return 200, "ok"

    def local_ip(self):
        if self.cfg["wb_host"]:
            return self.cfg["wb_host"]
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((self.cfg["miniserver"] or "192.0.2.1", 7))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return "127.0.0.1"


# --------------------------------------------------------------------------- нажатия кнопок

BUTTON_EVENTS = (("click", "одинарное"), ("double", "двойное"), ("triple", "тройное"), ("long", "долгое"))


class Buttons:
    """
    Одинарное / двойное / тройное / долгое нажатие на входах модулей WB.

    Шина опрашивает вход по кругу и короткие импульсы теряет, поэтому число
    нажатий берётся из аппаратного счётчика `Input N counter` (модуль считает
    сам), а удержание — по состоянию `Input N`: его длительность больше круга
    опроса. Настройки модулей не меняются.

    Кнопкой считается любая пара каналов `X` + `X counter`. Для неё в модели
    появляются служебные каналы `X#click`, `X#double`, `X#triple`, `X#long` —
    их выбирают и выгружают в шаблон как обычные; событие уходит в Loxone
    импульсом 1 → 0.
    """

    def __init__(self, cfg, model, bridge):
        self.cfg, self.model, self.bridge = cfg, model, bridge
        self.lock = threading.Lock()
        self.st = {}        # (dev, вход) -> состояние распознавания
        self.offs = []      # (время, dev, канал) — снять импульс
        model.listeners.append(self.on_change)

    def _ensure(self, d, base):
        """Завести служебные каналы событий, если у входа есть счётчик."""
        key = (d, base)
        if key in self.st:
            return self.st[key]
        with self.model.lock:
            ctrls = self.model.devices.get(d, {}).get("controls", {})
            if base not in ctrls or base + " counter" not in ctrls:
                return None
            bm = ctrls[base]["meta"]
            try:
                order = float(bm.get("order") or 9999)
            except ValueError:
                order = 9999
            for i, (ev, ru) in enumerate(BUTTON_EVENTS):
                ctrls.setdefault(base + "#" + ev, {
                    "meta": {"type": "event", "readonly": True, "order": order + 0.1 * (i + 1),
                             "title": "%s: %s" % (title_of(bm, base), ru), "virtual": True},
                    "value": "0", "ts": 0})
            cnt = ctrls[base + " counter"]["value"]
            st = self.st[key] = {
                "counter": int(cnt) if cnt and cnt.isdigit() else None,
                "presses": 0, "deadline": 0.0, "down": ctrls[base]["value"] == "1",
                # вход уже замкнут на старте (выключатель, датчик) — это не удержание кнопки
                "down_at": time.time(), "long": ctrls[base]["value"] == "1",
            }
            return st

    def trace(self, d, base, what):
        if self.cfg["button_log"] and any(self.bridge.selected(d, base + "#" + e) for e, _ in BUTTON_EVENTS):
            log("  вход %s/%s: %s" % (d, base, what))

    def on_change(self, d, c, value, now=None):
        now = time.time() if now is None else now
        if c.endswith(" counter"):
            base = c[:-len(" counter")]
            with self.lock:
                st = self._ensure(d, base)
                if st is None:
                    return
                try:
                    n = int(value)
                except (TypeError, ValueError):
                    return
                prev, st["counter"] = st["counter"], n
                self.trace(d, base, "счётчик %s → %s" % (prev, n))
                if prev is None or n <= prev or n - prev > 20:
                    return  # первое значение, сброс счётчика или явный мусор
                if st["long"] or now < st.get("ignore_until", 0):
                    return  # нажатие уже засчитано как долгое
                st["presses"] += n - prev
                st["deadline"] = now + self.cfg["click_ms"] / 1000.0
        elif "#" not in c:
            with self.lock:
                st = self._ensure(d, c)
                if st is None:
                    return
                down = value == "1"
                if down != st["down"]:
                    self.trace(d, c, "нажата" if down else "отпущена")
                if down and not st["down"]:
                    st["down_at"], st["long"] = now, False
                elif not down and st["down"]:
                    if st["long"]:
                        # счётчик с этим же нажатием может прийти позже отпускания
                        st["ignore_until"] = now + self.cfg["click_ms"] / 1000.0
                    st["long"] = False
                    if st["presses"]:
                        st["deadline"] = now + self.cfg["click_ms"] / 1000.0
                st["down"] = down

    def ticker(self):
        while True:
            time.sleep(0.03)
            self.step(time.time())

    def step(self, now):
        """Один шаг распознавания: вернуть события, ушедшие в Loxone на этом шаге."""
        fire = []
        with self.lock:
            for (d, base), st in self.st.items():
                if st["down"] and not st["long"] and now - st["down_at"] >= self.cfg["long_ms"] / 1000.0:
                    st["long"], st["presses"] = True, 0
                    fire.append((d, base + "#long"))
                elif st["presses"]:
                    # Ждём следующего нажатия, только если более длинная серия выбрана
                    # для Loxone: иначе событие уходит сразу, без паузы click_ms
                    n = min(st["presses"], 3)
                    more = any(self.bridge.selected(d, base + "#" + e) for e in ("double", "triple")[n - 1:])
                    if n >= 2 and not more:
                        ev = ("double", "triple")[n - 2]
                        if st["down"]:
                            st["long"] = True  # кнопку ещё держат — это не долгое нажатие
                    elif n == 1 and not more and not st["down"]:
                        ev = "click"
                    elif not st["down"] and now >= st["deadline"]:
                        ev = ("click", "double", "triple")[n - 1]
                    else:
                        continue
                    st["presses"] = 0
                    fire.append((d, base + "#" + ev))
            offs = [o for o in self.offs if o[0] <= now]
            self.offs = [o for o in self.offs if o[0] > now]
        for d, c in fire:
            self.emit(d, c, "1")
            log("Нажатие: %s/%s" % (d, c))
            with self.lock:
                self.offs.append((now + self.cfg["pulse_ms"] / 1000.0, d, c))
        for _, d, c in offs:
            self.emit(d, c, "0")
        return fire

    def emit(self, d, c, v):
        with self.model.lock:
            ctl = self.model.devices[d]["controls"][c]
            ctl["value"], ctl["ts"] = v, time.time()
        if self.bridge.selected(d, c):
            self.bridge.send(d, c, v)


# --------------------------------------------------------------------------- шаблоны Loxone

def x(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def selected_items(cfg, model, dev=None, snap=None):
    snap = snap if snap is not None else model.snapshot()
    out = []
    for d in sorted(snap):
        if dev is not None and d != dev:
            continue
        dv = snap[d]
        dname = dev_name(cfg, d, dv)
        ctrls = sorted(dv["controls"].items(),
                       key=lambda kv: (float(kv[1]["meta"].get("order") or 9999), kv[0]))
        for c, ctl in ctrls:
            if cfg["selected"].get(d + "/" + c):
                out.append((d, c, dname, ctl))
    return out


def dev_name(cfg, d, dv):
    return cfg["aliases"].get(d) or title_of(dv["meta"], d)


def input_ok(ctl):
    m = ctl["meta"]
    if not Model.readable(m):
        return False
    # строка вроде action=single — числом в Loxone не передать
    return not (Model.ctype(m) == "text" and to_loxone("text", ctl["value"]) is None)


def tpl_inputs(cfg, model, bridge, dev=None, title=None):
    """Виртуальный вход UDP. dev=None — все выбранные каналы одним блоком."""
    title = title or cfg["title"]
    o = ['<?xml version="1.0" encoding="utf-8"?>',
         '<VirtualInUdp Title="%s" Comment="Wiren Board %s · мост WB→Loxone" Address="%s" Port="%d">'
         % (x(title), x(bridge.local_ip()), x(bridge.local_ip()), int(cfg["udp_port"])),
         '\t<Info templateType="1" minVersion="16000610"/>']
    for d, c, dname, ctl in selected_items(cfg, model, dev):
        if not input_ok(ctl):
            continue
        m = ctl["meta"]
        t = Model.ctype(m)
        unit = Model.unit(m)
        if t in BINARY_TYPES or t == "rgb":
            u = "<v>"
        else:
            p = Model.precision(m, ctl["value"])
            u = ("<v.%d>" % p if p else "<v>") + (" " + unit if unit else "")
        # Сначала канал, потом устройство: в узком столбце Config строки различимы
        name = "%s — %s" % (title_of(m, c), dname)
        o.append('\t<VirtualInUdpCmd Title="%s" Comment="%s/%s" Address="" Check="wb.%s.%s=\\v" '
                 'Signed="true" Analog="true" SourceValLow="0" DestValLow="0" SourceValHigh="100" '
                 'DestValHigh="100" DefVal="0" MinVal="-2147483648" MaxVal="2147483647" Unit="%s" HintText=""/>'
                 % (x(name), x(d), x(c), x(safe_id(d)), x(safe_id(c)), x(u)))
    o.append("</VirtualInUdp>")
    return "\n".join(o) + "\n"


def tpl_outputs(cfg, model, bridge, dev=None, title=None):
    """Виртуальный выход HTTP. dev=None — все выбранные каналы одним блоком."""
    title = title or cfg["title"]
    addr = "http://%s:%d" % (bridge.local_ip(), int(cfg["http_port"]))
    o = ['<?xml version="1.0" encoding="utf-8"?>',
         '<VirtualOut Title="%s" Comment="Wiren Board · мост WB→Loxone" Address="%s" CmdInit="" '
         'CloseAfterSend="true" CmdSep="">' % (x(title), x(addr)),
         '\t<Info templateType="3" minVersion="16000610"/>']
    for d, c, dname, ctl in selected_items(cfg, model, dev):
        m = ctl["meta"]
        if not Model.writable(m):
            continue
        t = Model.ctype(m)
        base = "/set/%s/%s/%s" % (cfg["key"], urllib.parse.quote(d, safe=""), urllib.parse.quote(c, safe=""))
        name = "%s — %s" % (title_of(m, c), dname)
        if t == "pushbutton":
            on, off, analog = base + "/1", "", "false"
        elif t in BINARY_TYPES:
            on, off, analog = base + "/1", base + "/0", "false"
        else:
            on, off, analog = base + "/<v>", "", "true"
        o.append('\t<VirtualOutCmd Title="%s" Comment="%s/%s" CmdOnMethod="GET" CmdOn="%s" CmdOnHTTP="" '
                 'CmdOnPost="" CmdOffMethod="GET" CmdOff="%s" CmdOffHTTP="" CmdOffPost="" CmdAnswer="" '
                 'HintText="" Analog="%s" Repeat="0" RepeatRate="0"/>'
                 % (x(name), x(d), x(c), x(on), x(off), analog))
    o.append("</VirtualOut>")
    return "\n".join(o) + "\n"


def tpl_counts(cfg, model, dev, snap=None):
    """Сколько каналов устройства попадёт во вход и в выход."""
    items = selected_items(cfg, model, dev, snap)
    return (sum(1 for it in items if input_ok(it[3])),
            sum(1 for it in items if Model.writable(it[3]["meta"])))


def tpl_filename(prefix, title):
    """Как в MegaFlex: префикс Loxone (VIU_ / VO_) + имя без запрещённых знаков."""
    t = re.sub(r'[\\/:*?"<>|]+', "", title).strip()
    return prefix + (re.sub(r"\s+", "_", t) or "WirenBoard") + ".xml"


def tpl_zip(cfg, model, bridge):
    """Архив: по паре VIU_/VO_ на каждое устройство с выбранными каналами."""
    import io
    import zipfile
    buf = io.BytesIO()
    used = set()
    snap = model.snapshot()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for d in sorted({it[0] for it in selected_items(cfg, model)}):
            name = dev_name(cfg, d, snap[d])
            n_in, n_out = tpl_counts(cfg, model, d, snap)
            for prefix, n, fn in (("VIU_", n_in, tpl_inputs), ("VO_", n_out, tpl_outputs)):
                if not n:
                    continue
                f = tpl_filename(prefix, name)
                if f in used:  # одинаковые имена устройств — добавить ID
                    f = tpl_filename(prefix, name + " " + d)
                used.add(f)
                z.writestr(f, "\ufeff" + fn(cfg, model, bridge, d, name))
    return buf.getvalue()


# --------------------------------------------------------------------------- HTTP

def make_handler(cfg, model, bridge):
    with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
        page = f.read()

    class H(BaseHTTPRequestHandler):
        server_version = "wb-loxone/" + VERSION

        def log_message(self, fmt, *a):
            pass

        def reply(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, name, body, ctype, ascii_name=None):
            # Русское имя — через filename*, латинское — запасное для старых браузеров
            ascii_name = ascii_name or re.sub(r"[^A-Za-z0-9_.\-]", "_", name)
            return self.reply(200, body, ctype, {
                "Content-Disposition": "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                % (ascii_name, urllib.parse.quote(name))})

        def authed(self):
            pw = cfg["ui_password"]
            if not pw:
                return True
            import base64
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    u, _, p = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                    if secrets.compare_digest(p, pw):
                        return True
                except Exception:
                    pass
            self.reply(401, "нужен пароль", extra={"WWW-Authenticate": 'Basic realm="WB-Loxone"'})
            return False

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            parts = [urllib.parse.unquote(p) for p in path.split("/")]
            # Команды Loxone: /set/<ключ>/<устройство>/<канал>/<значение> — по ключу, без пароля
            if len(parts) >= 6 and parts[1] == "set":
                if not secrets.compare_digest(parts[2], cfg["key"]):
                    return self.reply(403, "неверный ключ")
                code, msg = bridge.command(parts[3], parts[4], "/".join(parts[5:]))
                if code != 200:
                    log("Команда отклонена:", path, msg)
                return self.reply(code, msg)
            if not self.authed():
                return
            if path in ("/", "/index.html"):
                return self.reply(200, page, "text/html; charset=utf-8")
            if path == "/api/state":
                return self.reply(200, json.dumps(self.state(), ensure_ascii=False),
                                  "application/json; charset=utf-8")
            if path in ("/template/viu.xml", "/template/vo.xml"):
                # ?dev=<устройство> — шаблон одного устройства, назван по нему (как в MegaFlex);
                # без параметра — все выбранные каналы одним блоком с общим именем
                inputs = path.endswith("viu.xml")
                dev = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("dev", [None])[0]
                if dev is not None and dev not in model.devices:
                    return self.reply(404, "нет такого устройства")
                title = dev_name(cfg, dev, model.devices[dev]) if dev else cfg["title"]
                prefix = "VIU_" if inputs else "VO_"
                body = (tpl_inputs if inputs else tpl_outputs)(cfg, model, bridge, dev, title)
                return self.send_file(tpl_filename(prefix, title), "\ufeff" + body,
                                      "application/xml; charset=utf-8",
                                      tpl_filename(prefix, safe_id(dev or cfg["title"])))
            if path == "/template/all.zip":
                return self.send_file("WB-Loxone_" + safe_id(cfg["title"]) + ".zip",
                                      tpl_zip(cfg, model, bridge), "application/zip")
            self.reply(404, "нет такой страницы")

        def do_POST(self):
            if not self.authed():
                return
            path = urllib.parse.urlsplit(self.path).path
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self.reply(400, "плохой JSON")
            if path == "/api/config":
                patch = {k: v for k, v in body.items() if k in DEFAULTS and k != "key"}
                for k in ("udp_port", "http_port", "throttle_ms", "resync_s", "click_ms", "long_ms", "pulse_ms"):
                    if k in patch:
                        patch[k] = int(patch[k])
                cfg.update(patch)
                bridge.resync()
                return self.reply(200, "ok")
            if path == "/api/newkey":
                cfg.update({"key": secrets.token_hex(4)})
                return self.reply(200, cfg["key"])
            if path == "/api/resync":
                return self.reply(200, str(bridge.resync()))
            if path == "/api/test":
                d, c, v = body.get("dev"), body.get("ctrl"), str(body.get("value", ""))
                code, msg = bridge.command(d, c, v)
                return self.reply(code, msg)
            self.reply(404, "нет такого метода")

        def state(self):
            snap = model.snapshot()
            devs = []
            for d in sorted(snap):
                dv = snap[d]
                ctrls = []
                for c, ctl in sorted(dv["controls"].items(),
                                     key=lambda kv: (float(kv[1]["meta"].get("order") or 9999), kv[0])):
                    m = ctl["meta"]
                    if ctl["value"] is None and not m:
                        continue
                    ctrls.append({
                        "id": c, "title": title_of(m, c), "type": Model.ctype(m),
                        "value": ctl["value"], "unit": Model.unit(m),
                        "r": Model.readable(m), "w": Model.writable(m),
                        "num": ctl["value"] is None or to_loxone(Model.ctype(m), ctl["value"]) is not None,
                        "error": m.get("error", ""), "min": m.get("min"), "max": m.get("max"),
                    })
                if not ctrls:
                    continue
                n_in, n_out = tpl_counts(cfg, model, d, snap)
                devs.append({
                    "id": d, "title": title_of(dv["meta"], d), "n_in": n_in, "n_out": n_out,
                    "driver": dv["meta"].get("driver", ""),
                    "system": d in SYSTEM_DEVICES or d.startswith(("wb-adc", "wbrules", "system")),
                    "controls": ctrls,
                })
            pub = {k: v for k, v in cfg.data.items() if k != "ui_password"}
            pub["has_password"] = bool(cfg["ui_password"])
            return {
                "version": VERSION, "config": pub, "devices": devs, "stats": bridge.stats,
                "mqtt": bool(bridge.mqtt and bridge.mqtt.connected), "wb_ip": bridge.local_ip(),
            }

    return H


# --------------------------------------------------------------------------- main

def main():
    if "--version" in sys.argv:
        print("wb-loxone " + VERSION)
        return
    import faulthandler
    import signal
    faulthandler.register(signal.SIGUSR1, all_threads=True)  # kill -USR1 — стеки потоков в журнал
    cfg = Config(CONFIG_PATH)
    model = Model()
    bridge = Bridge(cfg, model)

    def on_connect(cl):
        cl.subscribe("/devices/#")

    mqtt = MiniMqtt(os.environ.get("WBLOX_MQTT_HOST", "127.0.0.1"),
                    int(os.environ.get("WBLOX_MQTT_PORT", "1883")), model.handle, on_connect)
    bridge.mqtt = mqtt
    threading.Thread(target=mqtt.run_forever, daemon=True).start()
    buttons = Buttons(cfg, model, bridge)
    threading.Thread(target=buttons.ticker, daemon=True).start()
    threading.Thread(target=bridge.flusher, daemon=True).start()
    threading.Thread(target=bridge.resyncer, daemon=True).start()

    port = int(cfg["http_port"])
    srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(cfg, model, bridge))
    srv.daemon_threads = True
    log("WB→Loxone %s: веб-интерфейс на порту %d, конфиг %s" % (VERSION, port, CONFIG_PATH))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
