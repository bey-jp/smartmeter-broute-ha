#!/usr/bin/env python3
"""スマートメーター Bルート (RL7023 Stick-D/IPS, SKSTACK IP) → MQTT → Home Assistant

https://github.com/bey-jp/smartmeter-broute-ha (MIT License)

- 設定: ~/.config/smartmeter/config.ini（Bルート ID・パスワード、MQTT。リポジトリに入れない）
- 状態: ~/.local/state/smartmeter.json（見つけたメーターを覚えておき、再接続を速くする）
- HA へは MQTT Discovery で sensor を自動登録する。
- ID 未設定のあいだは待機し、設定ファイルを10分ごとに読み直す。

よくある「取れたり取れなかったりする」スクリプトとの違い:
  * SKSENDTO の後に1行だけ読むと、エコーや EVENT 21 を応答と取り違える
    → エコーを切り（SFE 0）、締め切りまで ERXUDP を待って TID で照合する。
  * 切断に気づかず止まる → 応答なしが続いたら再参加、だめなら再スキャン、
    それでもだめなら RL7023 をリセット。systemd の Restart=always で最後の保険。
  * E8（瞬時電流）は 0.1A 単位。積算電力量は D3（係数）と E1（単位）をメーターから読む。
"""
import configparser
import json
import logging
import os
import re
import signal
import sys
import time

import serial
import paho.mqtt.client as mqtt

CONFIG = os.path.expanduser("~/.config/smartmeter/config.ini")
STATE = os.path.expanduser("~/.local/state/smartmeter.json")
TOPIC = "smartmeter"
DISC = "homeassistant"

log = logging.getLogger("smartmeter")


class LinkLost(Exception):
    """メーターとのセッションが切れた（再参加が必要）"""


# ---------------------------------------------------------------- 設定・状態
def load_config():
    c = configparser.ConfigParser()
    c.read(CONFIG, encoding="utf-8")
    return {
        "id": c.get("broute", "id", fallback="").strip(),
        "pwd": c.get("broute", "password", fallback="").strip(),
        "mqtt_host": c.get("mqtt", "host", fallback="127.0.0.1"),
        "mqtt_port": c.getint("mqtt", "port", fallback=1883),
        "mqtt_user": c.get("mqtt", "username", fallback=""),
        "mqtt_pwd": c.get("mqtt", "password", fallback=""),
        "serial": c.get("device", "serial"),
        "interval": c.getint("device", "interval", fallback=15),
        "energy_interval": c.getint("device", "energy_interval", fallback=60),
    }


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE)


# ---------------------------------------------------------------- MQTT
class Mqtt:
    def __init__(self, cfg):
        kw = {}
        if hasattr(mqtt, "CallbackAPIVersion"):   # paho-mqtt 2.x
            kw["callback_api_version"] = mqtt.CallbackAPIVersion.VERSION1
        self.c = mqtt.Client(client_id="smartmeter-broute", clean_session=True, **kw)
        if cfg["mqtt_user"]:
            self.c.username_pw_set(cfg["mqtt_user"], cfg["mqtt_pwd"])
        self.c.will_set(f"{TOPIC}/status", "offline", qos=1, retain=True)
        self.c.on_connect = self._on_connect
        self.c.reconnect_delay_set(1, 60)
        self.c.connect_async(cfg["mqtt_host"], cfg["mqtt_port"], 60)
        self.c.loop_start()
        self.link = "OFF"

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.warning("MQTT 接続失敗 rc=%s", rc)
            return
        log.info("MQTT 接続")
        self._discovery()
        client.publish(f"{TOPIC}/status", "online", qos=1, retain=True)
        client.publish(f"{TOPIC}/link", self.link, qos=1, retain=True)

    def _discovery(self):
        dev = {"identifiers": ["smartmeter_broute"], "name": "スマートメーター",
               "model": "Bルート (RL7023 Stick-D/IPS)"}
        avail = [{"topic": f"{TOPIC}/status"}]
        sensors = {
            "power": dict(name="瞬時電力", device_class="power", unit_of_measurement="W",
                          state_class="measurement", expire_after=180, icon="mdi:flash"),
            "current_r": dict(name="瞬時電流 R相", device_class="current", unit_of_measurement="A",
                              state_class="measurement", expire_after=180),
            "current_t": dict(name="瞬時電流 T相", device_class="current", unit_of_measurement="A",
                              state_class="measurement", expire_after=180),
            "energy": dict(name="積算電力量", device_class="energy", unit_of_measurement="kWh",
                           state_class="total_increasing"),
        }
        for key, s in sensors.items():
            s.update(unique_id=f"smartmeter_{key}", default_entity_id=f"sensor.smartmeter_{key}",
                     state_topic=f"{TOPIC}/{key}", availability=avail, device=dev)
            self.c.publish(f"{DISC}/sensor/smartmeter/{key}/config", json.dumps(s, ensure_ascii=False),
                           qos=1, retain=True)
        b = dict(name="メーター接続", device_class="connectivity", unique_id="smartmeter_link",
                 default_entity_id="binary_sensor.smartmeter_link", state_topic=f"{TOPIC}/link",
                 availability=avail, device=dev, entity_category="diagnostic")
        self.c.publish(f"{DISC}/binary_sensor/smartmeter/link/config", json.dumps(b, ensure_ascii=False),
                       qos=1, retain=True)

    def pub(self, key, value, retain=True):
        self.c.publish(f"{TOPIC}/{key}", str(value), qos=1, retain=retain)

    def set_link(self, on):
        v = "ON" if on else "OFF"
        if v != self.link:
            self.link = v
            log.info("メーター接続: %s", v)
        self.pub("link", v)

    def close(self):
        try:
            self.c.publish(f"{TOPIC}/status", "offline", qos=1, retain=True).wait_for_publish(3)
        except Exception:
            pass
        self.c.loop_stop()
        self.c.disconnect()


# ---------------------------------------------------------------- SKSTACK IP
class Sk:
    def __init__(self, port):
        self.s = serial.Serial(port, 115200, timeout=1)
        self.ip = None
        self.tid = 0
        self.tx_hold_until = 0  # ARIB の送信時間制限（EVENT 32）中は送らない

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass

    def readline(self):
        b = self.s.readline()
        if not b:
            return None
        line = b.decode("ascii", errors="replace").strip()
        if line:
            self._watch_event(line)
        return line

    def _watch_event(self, line):
        """どの待ち受け中でも拾うべきイベント"""
        if not line.startswith("EVENT "):
            return
        code = line.split()[1]
        if code == "32":
            log.warning("送信時間制限に入った（EVENT 32）")
            self.tx_hold_until = time.time() + 3600
        elif code == "33":
            log.info("送信時間制限が解除（EVENT 33）")
            self.tx_hold_until = 0
        elif code in ("26", "27", "28"):
            raise LinkLost(f"セッション終了 EVENT {code}")
        elif code == "24" and self.ip:
            raise LinkLost("再認証に失敗 EVENT 24")
        elif code == "29":
            log.info("セッション期限（EVENT 29）。自動で再認証される")
        elif code == "25" and self.ip:
            log.info("再認証に成功（EVENT 25）")

    def cmd(self, c, timeout=5, secret=False):
        """コマンドを送り OK まで待つ。OK より前の行を返す。FAIL なら例外"""
        self.s.reset_input_buffer()
        self.s.write((c + "\r\n").encode())
        lines, end = [], time.time() + timeout
        while time.time() < end:
            l = self.readline()
            if l is None:
                continue
            if l == "OK" or l.startswith("OK "):
                return lines
            if l.startswith("FAIL"):
                shown = c.split()[0] if secret else c
                raise RuntimeError(f"{shown} → {l}")
            lines.append(l)
        raise TimeoutError(f"{c.split()[0]} に応答なし")

    def setup(self, cfg):
        self.cmd("SKRESET", timeout=5)
        time.sleep(1)
        self.cmd("SKSREG SFE 0")            # エコーを切る
        self.cmd(f"SKSETPWD {len(cfg['pwd']):X} " + cfg["pwd"], secret=True)
        self.cmd("SKSETRBID " + cfg["id"], secret=True)

    def scan(self):
        """ペアリング ID 付きスキャン。自宅のメーターだけが応答する"""
        for dur in (6, 7, 8):
            log.info("スキャン（duration %d）", dur)
            self.s.reset_input_buffer()
            self.s.write(f"SKSCAN 2 FFFFFFFF {dur}\r\n".encode())
            found, end = {}, time.time() + 120
            while time.time() < end:
                l = self.readline()
                if l is None:
                    continue
                if l.startswith("EVENT 22"):
                    break
                m = re.match(r"(Channel|Pan ID|Addr|LQI|PairID):([0-9A-F]+)$", l)
                if m:
                    found[m.group(1)] = m.group(2)
            if "Addr" in found:
                log.info("メーター発見 Channel %s Pan %s Addr %s LQI %s",
                         found.get("Channel"), found.get("Pan ID"), found["Addr"], found.get("LQI"))
                return {"channel": found["Channel"], "pan": found["Pan ID"], "addr": found["Addr"]}
        return None

    def join(self, meter):
        self.ip = None
        self.cmd("SKSREG S2 " + meter["channel"])
        self.cmd("SKSREG S3 " + meter["pan"])
        self.s.reset_input_buffer()
        self.s.write(f"SKLL64 {meter['addr']}\r\n".encode())
        ip, end = None, time.time() + 5
        while time.time() < end and not ip:
            l = self.readline()
            if l and re.fullmatch(r"[0-9A-F]{4}(:[0-9A-F]{4}){7}", l):
                ip = l
        if not ip:
            raise TimeoutError("SKLL64 に応答なし")
        self.cmd("SKJOIN " + ip)
        end = time.time() + 90
        while time.time() < end:
            l = self.readline()
            if l is None:
                continue
            if l.startswith("EVENT 24"):
                return False
            if l.startswith("EVENT 25"):
                self.ip = ip
                return True
        return False

    def term(self):
        try:
            if self.ip:
                self.ip = None
                self.cmd("SKTERM", timeout=10)
        except Exception:
            pass

    # ------------------------------------------------ ECHONET Lite
    def get(self, epcs, timeout=10):
        """低圧スマート電力量メータ(028801)へ Get。{EPC: bytes} を返す。応答なしなら None"""
        wait = self.tx_hold_until - time.time()
        if wait > 0:
            log.info("送信時間制限中。%d 秒待つ", wait)
            time.sleep(min(wait, 60))
            return {}   # 失敗に数えない
        self.tid = (self.tid + 1) & 0xFFFF
        frame = bytes([0x10, 0x81]) + self.tid.to_bytes(2, "big") + bytes.fromhex("05FF01028801") \
            + bytes([0x62, len(epcs)]) + b"".join(bytes([e, 0]) for e in epcs)
        self.s.reset_input_buffer()
        self.s.write(f"SKSENDTO 1 {self.ip} 0E1A 1 {len(frame):04X} ".encode() + frame)
        end = time.time() + timeout
        while time.time() < end:
            l = self.readline()
            if l is None or not l.startswith("ERXUDP"):
                continue
            res = parse_erxudp(l)
            if not res or res["tid"] != self.tid or res["seoj"] != "028801":
                continue
            if res["esv"] in (0x72, 0x52):   # Get_Res / Get_SNA（一部だけ応答）
                return res["props"]
        return None


def parse_erxudp(line):
    """ERXUDP <送信元> <宛先> <送信元ポート> <宛先ポート> <MAC> <暗号化> [<SIDE>] <長さ> <データ>"""
    cols = line.split()
    if len(cols) < 9 or cols[3] != "0E1A":
        return None
    try:
        n = int(cols[-2], 16)
        data = bytes.fromhex(cols[-1])
    except ValueError:
        return None
    if len(data) != n or len(data) < 12 or data[0:2] != b"\x10\x81":
        return None
    props, i = {}, 12
    for _ in range(data[11]):
        if i + 2 > len(data):
            break
        epc, pdc = data[i], data[i + 1]
        props[epc] = data[i + 2:i + 2 + pdc]
        i += 2 + pdc
    return {"tid": int.from_bytes(data[2:4], "big"), "seoj": data[4:7].hex().upper(),
            "esv": data[10], "props": props}


UNIT = {0x00: 1, 0x01: 0.1, 0x02: 0.01, 0x03: 0.001, 0x04: 0.0001,
        0x0A: 10, 0x0B: 100, 0x0C: 1000, 0x0D: 10000}


def decode_current(b):
    """E8: R相・T相 各2バイト符号付き、0.1A 単位。0x7FFE は「なし」（単相2線の T相など）"""
    out = []
    for k in (0, 2):
        v = int.from_bytes(b[k:k + 2], "big", signed=True)
        out.append(None if v in (0x7FFE, 0x7FFF) else round(v * 0.1, 1))
    return out


# ---------------------------------------------------------------- メイン
running = True


def stop(*_):
    global running
    running = False


def wait_config(m):
    """ID・パスワードが設定されるまで待つ"""
    while running:
        cfg = load_config()
        if cfg["id"] and cfg["pwd"]:
            return cfg
        log.info("Bルート ID / パスワードが未設定（%s）。10分後に読み直す", CONFIG)
        m.set_link(False)
        for _ in range(600):
            if not running:
                return None
            time.sleep(1)
    return None


def session(cfg, m, st):
    """1回の接続〜計測。戻ったら（例外も含め）呼び出し側が作り直す"""
    sk = Sk(cfg["serial"])
    try:
        sk.setup(cfg)
        meter = st.get("meter")
        joined = False
        if meter:
            log.info("前回のメーター情報で参加（Addr %s）", meter["addr"])
            joined = sk.join(meter)
        if not joined:
            meter = sk.scan()
            if not meter:
                log.warning("メーターが見つからない（ID が違う・電波が弱い）")
                return 300
            st["meter"] = meter
            save_state(st)
            joined = sk.join(meter)
        if not joined:
            log.warning("PANA 認証に失敗（パスワード違いの可能性）")
            st.pop("meter", None)
            save_state(st)
            return 120
        log.info("メーターに接続 %s", sk.ip)
        m.set_link(True)

        # 積算電力量の係数・単位
        p = sk.get([0xD3, 0xE1]) or {}
        coef = int.from_bytes(p[0xD3], "big") if len(p.get(0xD3, b"")) == 4 else 1
        unit = UNIT.get(p[0xE1][0], 0.1) if len(p.get(0xE1, b"")) == 1 else 0.1
        log.info("積算電力量 係数 %d、単位 %s kWh", coef, unit)

        fails, next_energy = 0, 0
        while running:
            t0 = time.time()
            epcs = [0xE7, 0xE8]
            if t0 >= next_energy:
                epcs.append(0xE0)
            p = sk.get(epcs)
            if p is None:
                fails += 1
                log.info("応答なし（%d 回連続）", fails)
                if fails >= 4:
                    raise LinkLost("応答なしが続いた")
                time.sleep(2)
                continue
            fails = 0
            m.set_link(True)
            if len(p.get(0xE7, b"")) == 4:
                w = int.from_bytes(p[0xE7], "big", signed=True)
                if -100000 < w < 100000:
                    m.pub("power", w)
            if len(p.get(0xE8, b"")) == 4:
                r, t = decode_current(p[0xE8])
                if r is not None:
                    m.pub("current_r", r)
                if t is not None:
                    m.pub("current_t", t)
            if len(p.get(0xE0, b"")) == 4:
                raw = int.from_bytes(p[0xE0], "big")
                if raw != 0xFFFFFFFE:
                    m.pub("energy", round(raw * coef * unit, 4))
                    next_energy = t0 + cfg["energy_interval"]
            rest = cfg["interval"] - (time.time() - t0)
            if rest > 0:
                time.sleep(rest)
        return 0
    finally:
        sk.term()
        sk.close()


def check():
    """RL7023 が応答するかだけ確かめる（ID 不要）: smartmeter.py --check"""
    sk = Sk(load_config()["serial"])
    try:
        sk.cmd("SKRESET")
        time.sleep(1)
        sk.cmd("SKSREG SFE 0")
        print("RL7023:", " ".join(sk.cmd("SKVER")) or "OK")
    finally:
        sk.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    if "--check" in sys.argv:
        return check()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    cfg = load_config()
    m = Mqtt(cfg)
    st = load_state()
    backoff = 10
    try:
        while running:
            cfg = wait_config(m)
            if not cfg:
                break
            try:
                wait = session(cfg, m, st)
                backoff = 10
            except LinkLost as e:
                log.warning("接続が切れた: %s。再接続する", e)
                wait = 5
            except (serial.SerialException, OSError) as e:
                log.warning("シリアルのエラー: %s", e)
                wait, backoff = backoff, min(backoff * 2, 300)
            except (RuntimeError, TimeoutError) as e:
                log.warning("RL7023 のエラー: %s", e)
                wait, backoff = backoff, min(backoff * 2, 300)
            m.set_link(False)
            for _ in range(int(wait)):
                if not running:
                    break
                time.sleep(1)
    finally:
        m.set_link(False)
        m.close()


if __name__ == "__main__":
    main()
