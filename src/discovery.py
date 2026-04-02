"""
Printer Discovery — find printers on the local network.

Supports BambuLab (UDP broadcast on port 2021, MQTT port 8883 scan)
and Klipper/Moonraker (HTTP port 7125 scan).
"""

import concurrent.futures
import json
import logging
import socket
import ssl
import struct
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Bambu printers broadcast on this port
BAMBU_DISCOVERY_PORT = 2021
# SSDP multicast group used by Bambu
BAMBU_MULTICAST_GROUP = "239.255.255.250"
# MQTT TLS port for fallback scanning
MQTT_TLS_PORT = 8883


def discover_printers(timeout: float = 5.0) -> List[dict]:
    """
    Listen for BambuLab printer UDP broadcasts on port 2021.

    Bambu printers periodically send JSON payloads containing:
    - dev_name: printer name
    - dev_id: serial number
    - dev_ip: IP address
    - dev_type: model (e.g. "3DPrinter-P1S-v1")
    - dev_signal: wifi signal strength
    - dev_connection_type: connection type

    Returns a list of discovered printer dicts.
    """
    discovered = {}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(1.0)

    try:
        sock.bind(("", BAMBU_DISCOVERY_PORT))

        # Join multicast group on all interfaces
        mreq = struct.pack(
            "4s4s",
            socket.inet_aton(BAMBU_MULTICAST_GROUP),
            socket.inet_aton("0.0.0.0"),
        )
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass  # May fail if not multicast, still works with broadcast

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
                try:
                    payload = json.loads(data.decode("utf-8", errors="replace"))
                    dev_id = payload.get("dev_id", "")
                    if dev_id and dev_id not in discovered:
                        printer_info = {
                            "name": payload.get("dev_name", f"Printer-{addr[0]}"),
                            "host": payload.get("dev_ip", addr[0]),
                            "serial": dev_id,
                            "model": payload.get("dev_type", "Unknown"),
                            "signal": payload.get("dev_signal", ""),
                            "connection_type": payload.get("dev_connection_type", ""),
                        }
                        discovered[dev_id] = printer_info
                        logger.info(f"Discovered: {printer_info['name']} at {printer_info['host']}")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            except socket.timeout:
                continue
    except OSError as e:
        logger.warning(f"UDP discovery error: {e}")
    finally:
        sock.close()

    return list(discovered.values())


def scan_port(host: str, port: int = MQTT_TLS_PORT, timeout: float = 1.5) -> bool:
    """Check if a host has the MQTT TLS port open."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except (OSError, socket.error):
        return False


def test_bambu_connection(host: str, access_code: str, serial: str,
                          port: int = MQTT_TLS_PORT, timeout: float = 5.0) -> dict:
    """
    Test MQTT connection to a Bambu printer.
    Returns {"ok": True/False, "message": "...", "state": {...}}.
    """
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        return {"ok": False, "message": "paho-mqtt not installed"}

    result = {"ok": False, "message": "Connection timed out"}
    connected_event = threading.Event()
    state_data = {}

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            result["ok"] = True
            result["message"] = "Connected successfully"
            client.subscribe(f"device/{serial}/report")
            # Request status
            client.publish(
                f"device/{serial}/request",
                json.dumps({
                    "pushing": {
                        "command": "pushall",
                        "sequence_id": str(int(time.time())),
                    }
                }),
            )
            connected_event.set()
        else:
            result["message"] = f"MQTT connection refused (rc={rc})"
            connected_event.set()

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            if "print" in payload:
                p = payload["print"]
                state_data["status"] = p.get("gcode_state", "")
                state_data["nozzle_temper"] = p.get("nozzle_temper", 0)
                state_data["bed_temper"] = p.get("bed_temper", 0)
                state_data["subtask_name"] = p.get("subtask_name", "")
                state_data["mc_percent"] = p.get("mc_percent", 0)
        except Exception:
            pass

    client = mqtt.Client(
        client_id=f"farm_test_{int(time.time())}",
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set("bblp", access_code)

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    client.tls_set_context(ssl_ctx)

    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(host, port, keepalive=10)
        client.loop_start()
        connected_event.wait(timeout=timeout)

        if result["ok"]:
            # Wait a moment for status data
            time.sleep(2)
            result["state"] = state_data

        client.loop_stop()
        client.disconnect()
    except Exception as e:
        result["message"] = str(e)

    return result


def scan_subnet(subnet_prefix: str, port: int = MQTT_TLS_PORT,
                timeout: float = 1.0, max_workers: int = 50) -> List[str]:
    """
    Scan a /24 subnet for hosts with MQTT TLS port open.
    subnet_prefix should be like "192.168.1" (first 3 octets).
    Returns list of IPs with port open.
    """
    hosts = [f"{subnet_prefix}.{i}" for i in range(1, 255)]
    found = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(scan_port, h, port, timeout): h for h in hosts}
        for future in concurrent.futures.as_completed(futures):
            host = futures[future]
            try:
                if future.result():
                    found.append(host)
                    logger.info(f"Port {port} open on {host}")
            except Exception:
                pass

    return sorted(found, key=lambda ip: list(map(int, ip.split("."))))


def get_local_subnets() -> List[str]:
    """Get the local machine's subnet prefixes (first 3 octets)."""
    subnets = set()
    try:
        # Get all IPs bound to this machine
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                parts = ip.split(".")
                subnets.add(".".join(parts[:3]))
    except Exception:
        pass

    # Fallback: try to get default interface IP
    if not subnets:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            parts = ip.split(".")
            subnets.add(".".join(parts[:3]))
        except Exception:
            pass

    return list(subnets)


# ── Klipper / Moonraker Discovery ─────────────────────────

MOONRAKER_PORT = 7125


def scan_moonraker_port(subnet_prefix: str, port: int = MOONRAKER_PORT,
                        timeout: float = 1.0, max_workers: int = 50) -> List[str]:
    """
    Scan a /24 subnet for hosts with Moonraker port open.
    Returns list of IPs with port open.
    """
    hosts = [f"{subnet_prefix}.{i}" for i in range(1, 255)]
    found = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(scan_port, h, port, timeout): h for h in hosts}
        for future in concurrent.futures.as_completed(futures):
            host = futures[future]
            try:
                if future.result():
                    found.append(host)
                    logger.info(f"Moonraker port {port} open on {host}")
            except Exception:
                pass

    return sorted(found, key=lambda ip: list(map(int, ip.split("."))))


def test_klipper_connection(host: str, port: int = MOONRAKER_PORT,
                            api_key: str = "", timeout: float = 5.0) -> dict:
    """
    Test HTTP connection to a Klipper printer via Moonraker.
    Returns {"ok": True/False, "message": "...", "state": {...}}.
    """
    try:
        import requests
    except ImportError:
        return {"ok": False, "message": "requests library not installed"}

    base_url = f"http://{host}:{port}"
    headers = {}
    if api_key:
        headers["X-Api-Key"] = api_key

    try:
        resp = requests.get(f"{base_url}/printer/info", headers=headers, timeout=timeout)
        if resp.status_code == 200:
            info = resp.json().get("result", {})
            state = {
                "klipper_state": info.get("state", ""),
                "software_version": info.get("software_version", ""),
                "hostname": info.get("hostname", ""),
            }
            return {
                "ok": True,
                "message": f"Connected — Klipper {info.get('state', 'ready')}",
                "state": state,
            }
        elif resp.status_code == 401:
            return {"ok": False, "message": "Unauthorized — API key may be required"}
        else:
            return {"ok": False, "message": f"Moonraker returned HTTP {resp.status_code}"}
    except requests.ConnectionError:
        return {"ok": False, "message": f"Cannot reach Moonraker at {base_url}"}
    except requests.Timeout:
        return {"ok": False, "message": "Connection timed out"}
    except Exception as e:
        return {"ok": False, "message": str(e)}
