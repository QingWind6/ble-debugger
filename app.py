#!/usr/bin/env python3
"""BLE Debugger - Bluetooth Low Energy debugging tool with Web UI and REST API."""

import asyncio
import threading
import time
from collections import deque
from datetime import datetime

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = "ble-debugger"
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

# Global state
scanner: BleakScanner | None = None
scanning = False
ble_client: BleakClient | None = None
loop = asyncio.new_event_loop()
connected_device_address: str | None = None
notification_handlers: dict[str, object] = {}
notification_buffers: dict[str, bytearray] = {}
notification_lock = threading.Lock()
discovered_devices = {}  # address -> BLEDevice object (kept alive for connection)
last_scan_results: list[dict] = []

EVENT_HISTORY_LIMIT = 200
event_condition = threading.Condition()
event_sequence = 0
notification_events = deque(maxlen=EVENT_HISTORY_LIMIT)
frame_events = deque(maxlen=EVENT_HISTORY_LIMIT)

AT_RESPONSE_SUFFIX = b"\r\nok\r\n"
AT_SIMPLE_OK = b"ok\r\n"
READY_PREFIX = b"+READY:"
LINE_SUFFIX = b"\r\n"
MAX_NOTIFICATION_BUFFER = 64 * 1024
RETERMINAL_SERVICE_DATA_UUID = "00002886-0000-1000-8000-00805f9b34fb"
RETERMINAL_SERVICE_DATA_UUID16 = "2886"
RETERMINAL_PRODUCT_INFO_VERSION = 1
RETERMINAL_BOARD_TYPE_VERSION = 2
RETERMINAL_PRODUCT_ID_LENGTH = 5


class ApiError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=30)


def now_timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def decode_payload_text(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace")


def record_event(kind: str, uuid: str, payload: bytes, timestamp: str | None = None):
    global event_sequence
    event = {
        "uuid": uuid,
        "value": payload.hex(),
        "text": decode_payload_text(payload),
        "timestamp": timestamp or now_timestamp(),
    }
    history = frame_events if kind == "frame" else notification_events
    with event_condition:
        event_sequence += 1
        event["id"] = event_sequence
        event["kind"] = kind
        history.append(event)
        event_condition.notify_all()
    return event


def get_latest_event_id() -> int:
    with event_condition:
        return event_sequence


def get_recorded_events(kind: str, uuid: str | None = None, since_id: int = 0, limit: int = 50):
    history = frame_events if kind == "frame" else notification_events
    with event_condition:
        items = [
            dict(item)
            for item in history
            if item["id"] > since_id and (uuid is None or item["uuid"] == uuid)
        ]
    if limit > 0:
        items = items[-limit:]
    return items


def clear_recorded_events(kind: str | None = None):
    with event_condition:
        if kind is None or kind == "notification":
            notification_events.clear()
        if kind is None or kind == "frame":
            frame_events.clear()


def wait_for_event(kind: str, uuid: str | None, after_id: int, timeout_s: float):
    deadline = time.monotonic() + timeout_s
    history = frame_events if kind == "frame" else notification_events
    with event_condition:
        while True:
            for item in history:
                if item["id"] > after_id and (uuid is None or item["uuid"] == uuid):
                    return dict(item)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            event_condition.wait(timeout=remaining)


def json_ok(result=None, status_code: int = 200):
    payload = {"ok": True}
    if result is not None:
        payload["result"] = result
    return jsonify(payload), status_code


def require_json_body():
    data = request.get_json(silent=True)
    if data is None:
        raise ApiError("JSON body is required")
    if not isinstance(data, dict):
        raise ApiError("JSON body must be an object")
    return data


def require_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(f'"{key}" is required')
    return value.strip()


def require_int(value, key: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(f'"{key}" must be an integer') from None


def parse_bool(value, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ApiError(f'"{key}" must be a boolean')


def normalize_event_kind(value: str | None) -> str:
    if value is None:
        return "notification"
    normalized = value.strip().lower()
    if normalized in {"notification", "raw"}:
        return "notification"
    if normalized == "frame":
        return "frame"
    raise ApiError('"kind" must be "notification" or "frame"')


def encode_write_value(raw_value: str, encoding: str) -> bytes:
    normalized = encoding.strip().lower()
    if normalized == "hex":
        try:
            return bytes.fromhex(raw_value)
        except ValueError as exc:
            raise ApiError(f"Invalid hex payload: {exc}") from exc
    if normalized == "text":
        return raw_value.encode("utf-8")
    raise ApiError('"encoding" must be "hex" or "text"')


def is_reterminal_service_data_uuid(uuid: str) -> bool:
    normalized = str(uuid).lower().replace("-", "")
    return normalized == RETERMINAL_SERVICE_DATA_UUID.replace("-", "") or normalized == RETERMINAL_SERVICE_DATA_UUID16


def parse_reterminal_product_info(adv):
    service_data = getattr(adv, "service_data", None) or {}
    for uuid, value in service_data.items():
        if not is_reterminal_service_data_uuid(uuid):
            continue

        payload = bytes(value)
        if payload.startswith(b"\x86\x28"):
            payload = payload[2:]

        if len(payload) < 1:
            return {"uuid": str(uuid), "raw": bytes(value).hex(), "valid": False, "error": "service data too short"}

        version = payload[0]
        info = {"uuid": str(uuid), "raw": bytes(value).hex(), "version": version, "valid": False}

        if version == RETERMINAL_PRODUCT_INFO_VERSION:
            if len(payload) < 1 + RETERMINAL_PRODUCT_ID_LENGTH:
                info["error"] = "v1 service data too short"
                return info

            product_bytes = payload[1:1 + RETERMINAL_PRODUCT_ID_LENGTH]
            try:
                product_id = product_bytes.decode("ascii")
            except UnicodeDecodeError:
                info["error"] = "product_id is not ASCII"
                return info

            info["product_id"] = product_id
            info["display_value"] = product_id
            info["format"] = "product_id"
            info["valid"] = True
            return info

        board_type_bytes = payload[1:]
        try:
            board_type = board_type_bytes.decode("ascii")
        except UnicodeDecodeError:
            return {"uuid": str(uuid), "raw": bytes(value).hex(), "version": version, "valid": False, "error": "board_type is not ASCII"}

        if not board_type:
            info["error"] = "board_type is empty"
            return info

        info["board_type"] = board_type
        info["display_value"] = board_type
        info["format"] = "board_type"
        info["valid"] = version == RETERMINAL_BOARD_TYPE_VERSION
        if not info["valid"]:
            info["warning"] = f"unknown format version {version}"
        return info

    return None


def serialize_scan_snapshot(discovered):
    result = []
    for addr, (dev, adv) in discovered.items():
        discovered_devices[addr] = dev
        product_info = parse_reterminal_product_info(adv)
        result.append({
            "address": addr,
            "name": dev.name or adv.local_name or "Unknown",
            "rssi": adv.rssi if adv.rssi is not None else -999,
            "connectable": adv.connectable if hasattr(adv, "connectable") else None,
            "product_id": product_info.get("display_value") if product_info else None,
            "product_info": product_info,
        })
    result.sort(key=lambda item: item["rssi"], reverse=True)
    return result


def clear_notification_buffers(uuid: str | None = None):
    with notification_lock:
        if uuid is None:
            notification_buffers.clear()
        else:
            notification_buffers.pop(uuid, None)


def extract_notification_frames(uuid: str, value: bytes | bytearray):
    frames = []
    with notification_lock:
        buffer = notification_buffers.setdefault(uuid, bytearray())
        buffer.extend(value)

        if len(buffer) > MAX_NOTIFICATION_BUFFER:
            socketio.emit("error", {"message": f"Notification buffer overflow for {uuid}"})
            notification_buffers.pop(uuid, None)
            return frames

        while buffer:
            frame_end = None

            if buffer.startswith(READY_PREFIX):
                ready_end = buffer.find(LINE_SUFFIX)
                if ready_end != -1:
                    frame_end = ready_end + len(LINE_SUFFIX)

            at_end = buffer.find(AT_RESPONSE_SUFFIX)
            if at_end != -1:
                at_end += len(AT_RESPONSE_SUFFIX)
                if frame_end is None or at_end < frame_end:
                    frame_end = at_end

            if frame_end is None and buffer.startswith(AT_SIMPLE_OK):
                frame_end = len(AT_SIMPLE_OK)

            if frame_end is None:
                break

            frames.append(bytes(buffer[:frame_end]))
            del buffer[:frame_end]

        if not buffer:
            notification_buffers.pop(uuid, None)

    return frames


def handle_notification_payload(sender_uuid: str, value: bytes):
    notification_event = record_event("notification", sender_uuid, value)
    socketio.emit("notification", {
        "uuid": sender_uuid,
        "value": notification_event["value"],
        "timestamp": notification_event["timestamp"],
    })

    for frame in extract_notification_frames(sender_uuid, value):
        frame_event = record_event("frame", sender_uuid, frame)
        socketio.emit("notification_frame", {
            "uuid": sender_uuid,
            "value": frame_event["value"],
            "timestamp": frame_event["timestamp"],
        })


def ensure_connected_client() -> BleakClient:
    if not ble_client or not ble_client.is_connected:
        raise ApiError("Not connected", status_code=409)
    return ble_client


def get_connection_state():
    connected = bool(ble_client and ble_client.is_connected)
    return {
        "scanning": scanning,
        "scan_results_count": len(last_scan_results),
        "connected": connected,
        "address": connected_device_address,
        "notify_enabled": sorted(notification_handlers.keys()),
        "latest_event_id": get_latest_event_id(),
    }


async def _stop_active_scanner():
    global scanner
    active_scanner = scanner
    if active_scanner is None:
        return
    scanner = None
    try:
        await active_scanner.stop()
    except Exception:
        pass


async def _find_device_by_address(address: str, timeout: float = 8.0):
    active_scanner = scanner
    if active_scanner is not None:
        try:
            devs = active_scanner.discovered_devices_and_advertisement_data
            for addr, (dev, _adv) in devs.items():
                if addr.lower() == address.lower():
                    return dev
        except Exception:
            pass
    return await BleakScanner.find_device_by_address(address, timeout=timeout)


def start_event_loop():
    asyncio.set_event_loop(loop)
    loop.run_forever()


threading.Thread(target=start_event_loop, daemon=True).start()


@app.errorhandler(ApiError)
def handle_api_error(exc: ApiError):
    return jsonify({"ok": False, "error": exc.message}), exc.status_code


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def api_health():
    return json_ok({"service": "ble-debugger", "state": get_connection_state()})


@app.route("/api/state")
def api_state():
    return json_ok(get_connection_state())


# --- Scanning ---

def start_scan():
    global scanning, last_scan_results
    if scanning:
        raise ApiError("Already scanning", status_code=409)
    scanning = True
    last_scan_results = []
    socketio.emit("scan_status", {"scanning": True})
    threading.Thread(target=_scan_loop, daemon=True).start()
    return {"scanning": True}


def stop_scan():
    global scanning
    scanning = False
    socketio.emit("scan_status", {"scanning": False})
    return {"scanning": False}


@socketio.on("start_scan")
def handle_start_scan():
    try:
        start_scan()
    except Exception as exc:
        emit("error", {"message": str(exc), "type": "scan"})
        emit("scan_status", {"scanning": False}) # 修复：扫描失败时重置UI状态


async def _do_scan():
    global scanner, last_scan_results
    active_scanner = BleakScanner()
    scanner = active_scanner
    await active_scanner.start()
    try:
        while scanning and scanner is active_scanner:
            await asyncio.sleep(3.0)
            devs = active_scanner.discovered_devices_and_advertisement_data
            last_scan_results = serialize_scan_snapshot(devs)
            socketio.emit("scan_results", {"devices": last_scan_results})
    finally:
        if scanner is active_scanner:
            scanner = None
        try:
            await active_scanner.stop()
        except Exception:
            pass


def _scan_loop():
    global scanning
    try:
        asyncio.run_coroutine_threadsafe(_do_scan(), loop).result()
    except Exception as exc:
        socketio.emit("error", {"message": f"Scan error: {exc}", "type": "scan"})
    finally:
        scanning = False
        socketio.emit("scan_status", {"scanning": False})


@socketio.on("stop_scan")
def handle_stop_scan():
    stop_scan()


@app.route("/api/scan/start", methods=["POST"])
def api_scan_start():
    return json_ok(start_scan())


@app.route("/api/scan/stop", methods=["POST"])
def api_scan_stop():
    return json_ok(stop_scan())


@app.route("/api/scan/results")
def api_scan_results():
    return json_ok({"scanning": scanning, "devices": last_scan_results})


# --- Connection ---

def connect_device(address: str):
    global ble_client, connected_device_address, notification_handlers, scanning
    try:
        if ble_client and ble_client.is_connected:
            run_async(ble_client.disconnect())
            notification_handlers.clear()
            clear_notification_buffers()
            clear_recorded_events()

        if scanning:
            scanning = False
            socketio.emit("scan_status", {"scanning": False})
            run_async(_stop_active_scanner())

        address_keys = [address, address.upper(), address.lower()]
        cached_device = next((discovered_devices.get(key) for key in address_keys if discovered_devices.get(key)), None)
        candidates = []
        if cached_device is not None:
            candidates.append(("cached", cached_device))
        refreshed_device = run_async(_find_device_by_address(address))
        if refreshed_device is not None:
            discovered_devices[address] = refreshed_device
            discovered_devices[address.upper()] = refreshed_device
            discovered_devices[address.lower()] = refreshed_device
            candidates.append(("refreshed", refreshed_device))
        candidates.append(("address", address))

        errors = []
        client = None
        for label, candidate in candidates:
            last_exc = None
            for attempt in range(3):
                try:
                    client = BleakClient(candidate, disconnected_callback=_on_disconnect)
                    run_async(client.connect())
                    break
                except Exception as exc:
                    last_exc = exc
                    try:
                        if client is not None and client.is_connected:
                            run_async(client.disconnect())
                    except Exception:
                        pass
                    client = None
                    if "InProgress" in str(exc) and attempt < 2:
                        time.sleep(1.0)
                        continue
            if client is not None:
                break
            if last_exc is not None:
                errors.append(f"{label}: {last_exc}")

        if client is None:
            direct_refresh = run_async(_find_device_by_address(address, timeout=12.0))
            if direct_refresh is not None:
                try:
                    client = BleakClient(direct_refresh, disconnected_callback=_on_disconnect)
                    run_async(client.connect())
                except Exception as exc:
                    errors.append(f"direct_refresh: {exc}")
                    try:
                        if client is not None and client.is_connected:
                            run_async(client.disconnect())
                    except Exception:
                        pass
                    client = None
            else:
                errors.append("direct_refresh: device not found")

        if client is None:
            raise ApiError("Connect failed: " + " | ".join(errors), status_code=500)

        ble_client = client
        connected_device_address = address
        services = _get_services(client)
        return {"address": address, "services": services}
    except Exception as exc:
        if isinstance(exc, ApiError):
            raise
        raise ApiError(f"Connect failed: {exc}", status_code=500) from exc


@socketio.on("connect_device")
def handle_connect(data):
    try:
        result = connect_device(data["address"])
        emit("connected", result)
    except Exception as exc:
        emit("error", {"message": str(exc), "type": "connect"})
        # 修复：确保即使发生异常，UI也能收到断开连接的状态，避免死等
        emit("disconnected") 


def _on_disconnect(client):
    global ble_client, connected_device_address, notification_handlers
    ble_client = None
    connected_device_address = None
    notification_handlers.clear()
    clear_notification_buffers()
    socketio.emit("disconnected")


def _get_services(client: BleakClient):
    services = []
    for svc in client.services:
        chars = []
        for char in svc.characteristics:
            props = char.properties
            descriptors = []
            for desc in char.descriptors:
                descriptors.append({
                    "uuid": str(desc.uuid),
                    "handle": desc.handle,
                })
            chars.append({
                "uuid": str(char.uuid),
                "handle": char.handle,
                "properties": list(props),
                "descriptors": descriptors,
            })
        services.append({
            "uuid": str(svc.uuid),
            "handle": svc.handle,
            "characteristics": chars,
        })
    return services


def disconnect_device():
    global ble_client, notification_handlers, connected_device_address
    address = connected_device_address
    try:
        if ble_client and ble_client.is_connected:
            run_async(ble_client.disconnect())
        else:
            ble_client = None
            connected_device_address = None
        notification_handlers.clear()
        clear_notification_buffers()
        clear_recorded_events()
        return {"address": address, "connected": False}
    except Exception as exc:
        raise ApiError(f"Disconnect failed: {exc}", status_code=500) from exc


@socketio.on("disconnect_device")
def handle_disconnect():
    try:
        result = disconnect_device()
        emit("disconnected")
    except Exception as exc:
        emit("error", {"message": str(exc), "type": "disconnect"})


@app.route("/api/connect", methods=["POST"])
def api_connect():
    data = require_json_body()
    return json_ok(connect_device(require_str(data, "address")))


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    return json_ok(disconnect_device())


@app.route("/api/services")
def api_services():
    client = ensure_connected_client()
    return json_ok({
        "address": connected_device_address,
        "services": _get_services(client),
    })


# --- Read / Write / Notify ---

def read_characteristic(uuid: str):
    client = ensure_connected_client()
    try:
        value = run_async(client.read_gatt_char(uuid))
        return {
            "uuid": uuid,
            "value": value.hex(),
            "text": decode_payload_text(bytes(value)),
            "timestamp": now_timestamp(),
        }
    except Exception as exc:
        raise ApiError(f"Read failed: {exc}", status_code=500) from exc


@socketio.on("read_char")
def handle_read(data):
    try:
        result = read_characteristic(data["uuid"])
        emit("char_value", {
            **result,
            "direction": "read",
        })
    except Exception as exc:
        emit("error", {"message": str(exc), "type": "read"})


def write_characteristic(uuid: str, value_bytes: bytes, response: bool = True):
    client = ensure_connected_client()
    try:
        run_async(client.write_gatt_char(uuid, value_bytes, response=response))
        return {
            "uuid": uuid,
            "value": value_bytes.hex(),
            "text": decode_payload_text(value_bytes),
            "timestamp": now_timestamp(),
            "with_response": response,
        }
    except Exception as exc:
        raise ApiError(f"Write failed: {exc}", status_code=500) from exc


@socketio.on("write_char")
def handle_write(data):
    try:
        result = write_characteristic(
            data["uuid"],
            bytes.fromhex(data["value"]),
            response=data.get("type", "with_response") == "with_response",
        )
        emit("write_ok", {
            "uuid": result["uuid"],
            "value": result["value"],
            "timestamp": result["timestamp"],
        })
    except Exception as exc:
        emit("error", {"message": str(exc), "type": "write"})


def set_notify(uuid: str, enable: bool):
    client = ensure_connected_client()
    try:
        if enable:
            if uuid in notification_handlers:
                return {"uuid": uuid, "enabled": True}

            clear_notification_buffers(uuid)

            def callback(sender: BleakGATTCharacteristic, value: bytearray):
                handle_notification_payload(str(sender.uuid), bytes(value))

            run_async(client.start_notify(uuid, callback))
            notification_handlers[uuid] = callback
            return {"uuid": uuid, "enabled": True}

        if uuid in notification_handlers:
            run_async(client.stop_notify(uuid))
            notification_handlers.pop(uuid, None)
        clear_notification_buffers(uuid)
        return {"uuid": uuid, "enabled": False}
    except Exception as exc:
        raise ApiError(f"Notify toggle failed: {exc}", status_code=500) from exc


@socketio.on("toggle_notify")
def handle_notify(data):
    try:
        result = set_notify(data["uuid"], bool(data["enable"]))
        emit("notify_status", result)
    except Exception as exc:
        emit("error", {"message": str(exc), "type": "notify"})


def read_descriptor(handle: int):
    client = ensure_connected_client()
    try:
        value = run_async(client.read_gatt_descriptor(handle))
        return {
            "handle": handle,
            "value": value.hex(),
            "text": decode_payload_text(bytes(value)),
        }
    except Exception as exc:
        raise ApiError(f"Read descriptor failed: {exc}", status_code=500) from exc


@socketio.on("read_descriptor")
def handle_read_descriptor(data):
    try:
        emit("descriptor_value", read_descriptor(data["handle"]))
    except Exception as exc:
        emit("error", {"message": str(exc), "type": "descriptor"})


@app.route("/api/read", methods=["POST"])
def api_read():
    data = require_json_body()
    return json_ok(read_characteristic(require_str(data, "uuid")))


@app.route("/api/write", methods=["POST"])
def api_write():
    data = require_json_body()
    encoding = data.get("encoding", "hex")
    with_response = parse_bool(data.get("with_response", True), "with_response")
    value = require_str(data, "value")
    payload = encode_write_value(value, encoding)
    return json_ok(write_characteristic(require_str(data, "uuid"), payload, response=with_response))


@app.route("/api/notify", methods=["POST"])
def api_notify():
    data = require_json_body()
    enable = parse_bool(data.get("enable", True), "enable")
    return json_ok(set_notify(require_str(data, "uuid"), enable))


@app.route("/api/read_descriptor", methods=["POST"])
def api_read_descriptor():
    data = require_json_body()
    return json_ok(read_descriptor(require_int(data.get("handle"), "handle")))


@app.route("/api/events", methods=["GET", "DELETE"])
def api_events():
    if request.method == "DELETE":
        kind_param = request.args.get("kind")
        if kind_param is None or kind_param == "all":
            clear_recorded_events()
        else:
            clear_recorded_events(normalize_event_kind(kind_param))
        return json_ok({"cleared": kind_param or "all"})

    kind = normalize_event_kind(request.args.get("kind"))
    uuid = request.args.get("uuid")
    since_id = require_int(request.args.get("since_id", 0), "since_id")
    limit = require_int(request.args.get("limit", 50), "limit")
    if limit < 1 or limit > EVENT_HISTORY_LIMIT:
        raise ApiError(f'"limit" must be between 1 and {EVENT_HISTORY_LIMIT}')

    return json_ok({
        "kind": kind,
        "events": get_recorded_events(kind, uuid=uuid, since_id=since_id, limit=limit),
        "latest_event_id": get_latest_event_id(),
    })


@app.route("/api/exchange", methods=["POST"])
def api_exchange():
    data = require_json_body()
    write_uuid = require_str(data, "write_uuid")
    notify_uuid = require_str(data, "notify_uuid")
    value = require_str(data, "value")
    encoding = data.get("encoding", "text")
    with_response = parse_bool(data.get("with_response", True), "with_response")
    auto_enable_notify = parse_bool(data.get("auto_enable_notify", True), "auto_enable_notify")
    clear_buffers = parse_bool(data.get("clear_buffers", True), "clear_buffers")
    wait_kind = normalize_event_kind(data.get("wait_kind", "frame"))
    timeout_ms = require_int(data.get("timeout_ms", 5000), "timeout_ms")
    if timeout_ms < 1:
        raise ApiError('"timeout_ms" must be greater than 0')

    if auto_enable_notify:
        set_notify(notify_uuid, True)
    if clear_buffers:
        clear_notification_buffers(notify_uuid)

    after_id = get_latest_event_id()
    write_result = write_characteristic(
        write_uuid,
        encode_write_value(value, encoding),
        response=with_response,
    )
    event = wait_for_event(wait_kind, notify_uuid, after_id, timeout_ms / 1000.0)
    if event is None:
        raise ApiError(
            f"Timed out waiting for {wait_kind} on {notify_uuid} after write to {write_uuid}",
            status_code=504,
        )

    return json_ok({
        "write": write_result,
        "event": event,
    })


@app.route("/api/at/command", methods=["POST"])
def api_at_command():
    data = require_json_body()
    write_uuid = require_str(data, "write_uuid")
    notify_uuid = require_str(data, "notify_uuid")
    command = require_str(data, "command")
    with_response = parse_bool(data.get("with_response", True), "with_response")
    auto_enable_notify = parse_bool(data.get("auto_enable_notify", True), "auto_enable_notify")
    clear_buffers = parse_bool(data.get("clear_buffers", True), "clear_buffers")
    timeout_ms = require_int(data.get("timeout_ms", 5000), "timeout_ms")
    if timeout_ms < 1:
        raise ApiError('"timeout_ms" must be greater than 0')

    if auto_enable_notify:
        set_notify(notify_uuid, True)
    if clear_buffers:
        clear_notification_buffers(notify_uuid)

    after_id = get_latest_event_id()
    write_result = write_characteristic(
        write_uuid,
        command.encode("utf-8"),
        response=with_response,
    )
    event = wait_for_event("frame", notify_uuid, after_id, timeout_ms / 1000.0)
    if event is None:
        raise ApiError(
            f"Timed out waiting for AT response frame on {notify_uuid}",
            status_code=504,
        )

    return json_ok({
        "write": write_result,
        "frame": event,
    })


if __name__ == "__main__":
    print("\n  BLE Debugger starting...")
    print("  Open http://localhost:5555 in your browser\n")
    socketio.run(app, host="0.0.0.0", port=5555, debug=False, allow_unsafe_werkzeug=True)