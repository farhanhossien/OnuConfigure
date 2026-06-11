from flask import Flask, request, jsonify, send_from_directory
import socket
import time
import re
import threading

app = Flask(__name__, static_folder='.')

# PON → Service VLAN mapping
PON_VLAN = {
    0: 897, 1: 898, 2: 899, 3: 900, 4: 901, 5: 902,
    6: 903, 7: 904, 8: 905, 9: 906, 10: 907, 11: 908,
    12: 909, 13: 910, 14: 911, 15: 912
}

sessions = {}
sessions_lock = threading.Lock()


class OLTSession:
    def __init__(self, host, port, username, password):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sock = None
        self._buf = b""

    def _send(self, text):
        self.sock.sendall(text.encode("ascii"))

    def _negotiate_telnet(self, data):
        out = b""
        i = 0
        while i < len(data):
            if data[i] == 0xFF and i + 2 < len(data):
                cmd = data[i + 1]
                opt = data[i + 2]
                if cmd in (0xFB, 0xFD):
                    reply_cmd = 0xFE if cmd == 0xFD else 0xFC
                    self.sock.sendall(bytes([0xFF, reply_cmd, opt]))
                i += 3
            else:
                out += bytes([data[i]])
                i += 1
        return out

    def _recv_raw(self, timeout=1.0):
        self.sock.settimeout(timeout)
        try:
            chunk = self.sock.recv(4096)
            return self._negotiate_telnet(chunk) if chunk else b""
        except socket.timeout:
            return b""

    def _read_until(self, *patterns, timeout=12):
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self._recv_raw(timeout=0.5)
            if chunk:
                self._buf += chunk
            text = self._buf.decode("utf-8", errors="replace").lower()
            for pat in patterns:
                p = pat.decode("utf-8", errors="replace").lower() if isinstance(pat, bytes) else pat.lower()
                if p in text:
                    self._buf = b""
                    return text
        consumed = self._buf.decode("utf-8", errors="replace")
        self._buf = b""
        return consumed

    def _read_output(self, timeout=3):
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self._recv_raw(timeout=0.4)
            if chunk:
                buf += chunk
                deadline = time.time() + 0.5
        return buf.decode("utf-8", errors="replace")

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(15)
        self.sock.connect((self.host, self.port))

        got = self._read_until("user name", "username", "login", timeout=12)
        if not any(p in got for p in ("user name", "username", "login")):
            raise Exception(f"No login prompt received. Got: {got[:200]!r}")

        self._send(self.username + "\r\n")

        got = self._read_until("password", "passwd", timeout=10)
        if "password" not in got and "passwd" not in got:
            raise Exception(f"No password prompt received. Got: {got[:200]!r}")

        self._send(self.password + "\r\n")

        time.sleep(2.0)
        out = self._read_output(timeout=4)
        out_lower = out.lower()

        if any(k in out_lower for k in ("bad password", "login failed", "incorrect", "invalid", "authentication fail", "access denied")):
            raise Exception("Login failed — wrong username or password")

        self._send("enable\r\n")
        # MA5800 'enable' may ask for a password (">>User password:").
        # Wait up to 5 s; if we see a password prompt, send the same password.
        enable_out = self._read_until(
            "password", "passwd", "#", ">", timeout=5
        )
        if "password" in enable_out.lower() or "passwd" in enable_out.lower():
            self._send(self.password + "\r\n")
            time.sleep(1.5)
            self._read_output(timeout=3)
        else:
            # Already got a prompt — nothing more to do
            pass

        return True

    def send_command(self, cmd, wait=2.0, press_enter_twice=False):
        self._send(cmd + "\r\n")

        if press_enter_twice:
            # MA5800 shows { <cr>||<K> }: before executing some commands.
            # Wait for that prompt, then send Enter to confirm.
            # If the prompt never arrives (command ran immediately), skip.
            CONFIRM_PROMPTS = [
                "{ <cr>", "<cr>", "{ cr", "confirm", "(y/n)", "[y/n]", "}:",
            ]
            deadline = time.time() + 2.0
            buf = b""
            found_confirm = False
            while time.time() < deadline:
                chunk = self._recv_raw(timeout=0.5)
                if chunk:
                    buf += chunk
                text = buf.decode("utf-8", errors="replace").lower()
                if any(p.lower() in text for p in CONFIRM_PROMPTS):
                    found_confirm = True
                    break
            if found_confirm:
                self._send("\r\n")
            self._buf = b""  # discard stale data so next _read_output is clean

        time.sleep(wait)
        output = self._read_output(timeout=wait + 1)

        # Handle ---- More ---- pagination by aborting it with 'q' to return to prompt
        while True:
            output_lower = output.lower()
            recent = output_lower[-250:]
            if "---- more ----" in recent or "  ---- more" in recent or "more ( press" in recent or "press 'q' to break" in recent:
                self._send("q\r\n")
                time.sleep(1.0)
                more = self._read_output(timeout=2)
                if more:
                    output += more
                break
            else:
                break
        return output

    def wait_for_mode(self, *prompts, timeout=5):
        """
        Read until the OLT prompt matches one of the given strings.
        Returns the received text. Used after mode-changing commands
        (config, interface ...) to confirm the mode switch happened.
        """
        deadline = time.time() + timeout
        buf = ""
        while time.time() < deadline:
            chunk = self._recv_raw(timeout=0.5)
            if chunk:
                buf += chunk.decode("utf-8", errors="replace")
            for p in prompts:
                if p.lower() in buf.lower():
                    return buf
        return buf

    def disconnect(self):
        if self.sock:
            try:
                self._send("quit\r\n")
                time.sleep(0.3)
                self.sock.close()
            except Exception:
                pass


def get_session(session_id):
    with sessions_lock:
        return sessions.get(session_id)


# ------------------------------------------------------------------ #
#  Routes
# ------------------------------------------------------------------ #
@app.route("/")
def index():
    return send_from_directory('.', 'index.html')


@app.route("/api/connect", methods=["POST"])
def connect():
    data = request.json
    host = data.get("host", "").strip()
    port = int(data.get("port", 23))
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not host or not username:
        return jsonify({"success": False, "error": "Host and username required"}), 400

    session_id = f"{host}_{username}_{int(time.time())}"
    olt = OLTSession(host, port, username, password)

    try:
        olt.connect()
        with sessions_lock:
            sessions[session_id] = olt
        return jsonify({"success": True, "session_id": session_id, "message": f"Connected to {host}"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/disconnect", methods=["POST"])
def disconnect():
    data = request.json
    session_id = data.get("session_id")
    olt = get_session(session_id)
    if olt:
        olt.disconnect()
        with sessions_lock:
            del sessions[session_id]
    return jsonify({"success": True})


@app.route("/api/autofind", methods=["POST"])
def autofind():
    data = request.json
    session_id = data.get("session_id")
    olt = get_session(session_id)
    if not olt:
        return jsonify({"success": False, "error": "Session not found"}), 404

    try:
        full_output = olt.send_command("display ont autofind all", wait=5.0, press_enter_twice=True)
        onus = parse_autofind(full_output)
        return jsonify({"success": True, "onus": onus, "raw": full_output})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def parse_autofind(output):
    onus = []

    blocks_b = re.split(r'-{10,}', output)

    for block in blocks_b:
        block = block.strip()
        if not block:
            continue

        fsp_match = re.search(
            r'F/S/P\s*:\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)',
            block, re.IGNORECASE
        )
        if not fsp_match:
            continue

        frame = int(fsp_match.group(1))
        slot  = int(fsp_match.group(2))
        pon   = int(fsp_match.group(3))

        EPON_SLOTS = {7}
        if slot in EPON_SLOTS:
            onu_type = "EPON"
        elif re.search(r'GPON', block, re.IGNORECASE) and not re.search(r'EPON|ONT MAC', block, re.IGNORECASE):
            onu_type = "GPON"
        else:
            onu_type = "EPON" if re.search(r'ONT MAC\s*:', block, re.IGNORECASE) else "GPON"

        if onu_type == "EPON":
            mac_match = re.search(
                r'ONT MAC\s*:\s*([0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4})',
                block, re.IGNORECASE
            )
            if not mac_match:
                mac_match = re.search(
                    r'([0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}|'
                    r'[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})',
                    block
                )
            if mac_match:
                identifier = mac_match.group(1) if '-' in mac_match.group(0) else mac_match.group(0)
                if ":" in identifier:
                    parts = identifier.replace(":", "")
                    identifier = f"{parts[0:4]}-{parts[4:8]}-{parts[8:12]}".upper()
                else:
                    identifier = identifier.upper()
            else:
                identifier = "UNKNOWN"
        else:
            sn_match = re.search(r'SN\s*:\s*([0-9A-Fa-f]{8,16})', block, re.IGNORECASE)
            identifier = sn_match.group(1).upper() if sn_match else "UNKNOWN"

        onus.append({
            "frame": frame, "slot": slot, "pon": pon,
            "type": onu_type, "identifier": identifier,
            "fsp": f"{frame}/{slot}/{pon}",
            "service_vlan": PON_VLAN.get(pon, 0)
        })

    if not onus:
        for line in output.splitlines():
            m = re.match(r'\s*\d+\s+(\d+)/\s*(\d+)/\s*(\d+)\s+(\S+)', line)
            if not m:
                continue
            frame, slot, pon = int(m.group(1)), int(m.group(2)), int(m.group(3))
            raw_id = m.group(4)
            EPON_SLOTS = {7}
            onu_type = "EPON" if slot in EPON_SLOTS else "GPON"
            if re.match(r'[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}', raw_id):
                identifier = raw_id.upper()
                onu_type = "EPON"
            elif re.match(r'[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}', raw_id):
                parts = raw_id.replace(":", "")
                identifier = f"{parts[0:4]}-{parts[4:8]}-{parts[8:12]}".upper()
                onu_type = "EPON"
            else:
                identifier = raw_id.upper()

            onus.append({
                "frame": frame, "slot": slot, "pon": pon,
                "type": onu_type, "identifier": identifier,
                "fsp": f"{frame}/{slot}/{pon}",
                "service_vlan": PON_VLAN.get(pon, 0)
            })

    return onus


@app.route("/api/get_current_config", methods=["POST"])
def get_current_config():
    data = request.json
    session_id = data.get("session_id")
    frame = data.get("frame", 0)
    slot = data.get("slot")
    pon = data.get("pon")
    olt = get_session(session_id)
    if not olt:
        return jsonify({"success": False, "error": "Session not found"}), 404

    try:
        cmd = f"display current-configuration port {frame}/{slot}/{pon}"
        full_output = olt.send_command(cmd, wait=3.0, press_enter_twice=True)
        used_ids = parse_used_ont_ids(full_output)
        free_id = next((i for i in range(64) if i not in used_ids), None)
        return jsonify({
            "success": True,
            "used_ids": sorted(list(used_ids)),
            "free_id": free_id,
            "raw": full_output
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def parse_used_ont_ids(config_output):
    used = set()
    for m in re.finditer(r'\bont\s+add\s+\d+\s+(\d+)\b', config_output):
        used.add(int(m.group(1)))
    for m in re.finditer(r'\bont\s+port\s+native-vlan\s+\d+\s+(\d+)\b', config_output):
        used.add(int(m.group(1)))
    for m in re.finditer(r'\bont\s+(\d+)\s+multi-service\b', config_output):
        used.add(int(m.group(1)))
    return used


@app.route("/api/configure_onu", methods=["POST"])
def configure_onu():
    data = request.json
    session_id = data.get("session_id")
    frame = data.get("frame", 0)
    slot = data.get("slot")
    pon = data.get("pon")
    ont_id = data.get("ont_id")
    mac = data.get("mac")
    service_vlan = PON_VLAN.get(int(pon), 0)

    olt = get_session(session_id)
    if not olt:
        return jsonify({"success": False, "error": "Session not found"}), 404

    steps = []

    def run(cmd, wait=2.5, enter_twice=False):
        out = olt.send_command(cmd, wait=wait, press_enter_twice=enter_twice)
        steps.append({"cmd": cmd, "output": out})
        return out

    try:
        # ── Step 1: Enter config mode ──────────────────────────────────────
        # Send 'config' and wait explicitly for the (config)# prompt.
        # Some MA5800 firmware shows { <cr>||<K> }: before executing, so we
        # use press_enter_twice=True to handle that automatically.
        olt._send("config\r\n")
        steps.append({"cmd": "config", "output": ""})
        config_buf = olt.wait_for_mode("(config)#", "(config)", timeout=8)
        steps[-1]["output"] = config_buf

        if "unknown command" in config_buf.lower():
            raise Exception(f"'config' command rejected. Got: {config_buf.strip()[:200]}")

        # If the OLT is showing a { <cr> } confirmation prompt, send Enter
        if any(p in config_buf.lower() for p in ("{ <cr>", "<cr>", "}:")):
            olt._send("\r\n")
            config_buf2 = olt.wait_for_mode("(config)#", "(config)", timeout=6)
            steps[-1]["output"] += config_buf2
            config_buf = config_buf2

        if "(config)" not in config_buf.lower():
            raise Exception(f"Could not enter config mode. Got: {config_buf.strip()[:200]}")

        # ── Step 2: Board confirm ──────────────────────────────────────────
        run(f"board confirm {frame}/{slot}", wait=2.0)

        # ── Step 3: Enter EPON interface ───────────────────────────────────
        # MUST be in (config)# mode here. Send command then wait for the
        # interface prompt before proceeding — do NOT rely on timed sleep alone.
        olt._send(f"interface epon {frame}/{slot}\r\n")
        steps.append({"cmd": f"interface epon {frame}/{slot}", "output": ""})
        iface_buf = olt.wait_for_mode("(config-if-epon", "(epon-", "epon)", timeout=8)
        steps[-1]["output"] = iface_buf

        if "unknown command" in iface_buf.lower() or (
                "%" in iface_buf and "error" in iface_buf.lower()):
            raise Exception(
                f"Could not enter interface epon {frame}/{slot}. Got: {iface_buf.strip()[:200]}")

        # ── Step 4: Add ONT (confirmation prompt expected) ─────────────────
        ont_cmd = (f"ont add {pon} {ont_id} mac-auth {mac} "
                   f"oam ont-lineprofile-id 20 ont-srvprofile-id 20")
        run(ont_cmd, wait=3.0, enter_twice=True)

        # ── Step 5: Set native VLAN ────────────────────────────────────────
        run(f"ont port native-vlan {pon} {ont_id} eth 1 vlan 100", wait=2.0)

        # ── Step 6: Exit interface back to (config)# with 'quit' ───────────
        # 'return' goes all the way back to enable mode; 'quit' goes up one
        # level to (config)# which is where service-port must be run.
        olt._send("quit\r\n")
        steps.append({"cmd": "quit", "output": ""})
        # Wait specifically for the global config prompt '(config)#' but NOT the interface prompt '(config-if'
        deadline = time.time() + 8.0
        quit_buf = ""
        while time.time() < deadline:
            chunk = olt._recv_raw(timeout=0.5)
            if chunk:
                quit_buf += chunk.decode("utf-8", errors="replace")
            if "(config)" in quit_buf.lower() and "(config-if" not in quit_buf.lower():
                break
        steps[-1]["output"] = quit_buf

        # ── Step 7: service-port (must run in config mode) ─────────────────
        svc_cmd = (f"service-port vlan {service_vlan} epon {frame}/{slot}/{pon} "
                   f"ont {ont_id} multi-service user-vlan 100 tag-transform translate")
        run(svc_cmd, wait=3.0, enter_twice=True)

        return jsonify({
            "success": True,
            "message": f"ONU configured successfully! Frame:{frame} Slot:{slot} PON:{pon} ONT-ID:{ont_id}",
            "steps": steps
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e), "steps": steps}), 500


if __name__ == "__main__":
    print("=" * 50)
    print("  Huawei OLT Manager - Starting on port 5000")
    print("  Open: http://localhost:5000")
    print("=" * 50)
    app.run(debug=False, host="0.0.0.0", port=5000)
