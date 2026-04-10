#!/usr/bin/env python3
"""BLE Debugger - Bluetooth Low Energy debugging tool with Web UI."""

import asyncio
import threading
import struct
from datetime import datetime
from flask import Flask, render_template
from flask_socketio import SocketIO, emit
from bleak import BleakScanner, BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic

app = Flask(__name__)
app.config["SECRET_KEY"] = "ble-debugger"
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

# Global state
scanner: BleakScanner | None = None
scanning = False
scan_task = None
ble_client: BleakClient | None = None
loop = asyncio.new_event_loop()
connected_device_address = None
notification_handlers = {}
discovered_devices = {}  # address -> BLEDevice object (kept alive for connection)


def run_async(coro):
    """Run an async coroutine in the background event loop."""
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=30)


def start_event_loop():
    """Start the asyncio event loop in a background thread."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


threading.Thread(target=start_event_loop, daemon=True).start()


@app.route("/")
def index():
    return render_template("index.html")


# --- Scanning ---

@socketio.on("start_scan")
def handle_start_scan():
    global scanning
    if scanning:
        emit("error", {"message": "Already scanning"})
        return
    scanning = True
    emit("scan_status", {"scanning": True})
    threading.Thread(target=_scan_loop, daemon=True).start()


async def _do_scan():
    """Use a persistent BleakScanner so BLEDevice objects stay alive in BlueZ."""
    global scanner, discovered_devices, scanning
    scanner = BleakScanner()
    await scanner.start()
    try:
        while scanning:
            await asyncio.sleep(3.0)
            devs = scanner.discovered_devices_and_advertisement_data
            result = []
            for addr, (dev, adv) in devs.items():
                discovered_devices[addr] = dev  # cache BLEDevice for connection
                result.append({
                    "address": addr,
                    "name": dev.name or adv.local_name or "Unknown",
                    "rssi": adv.rssi if adv.rssi is not None else -999,
                    "connectable": adv.connectable if hasattr(adv, "connectable") else None,
                })
            result.sort(key=lambda d: d["rssi"], reverse=True)
            socketio.emit("scan_results", {"devices": result})
    finally:
        await scanner.stop()
        scanner = None


def _scan_loop():
    global scanning
    try:
        asyncio.run_coroutine_threadsafe(_do_scan(), loop).result()
    except Exception as e:
        socketio.emit("error", {"message": f"Scan error: {e}"})
    finally:
        scanning = False
        socketio.emit("scan_status", {"scanning": False})


@socketio.on("stop_scan")
def handle_stop_scan():
    global scanning
    scanning = False
    emit("scan_status", {"scanning": False})


# --- Connection ---

@socketio.on("connect_device")
def handle_connect(data):
    global ble_client, connected_device_address, notification_handlers
    address = data["address"]
    try:
        if ble_client and ble_client.is_connected:
            run_async(ble_client.disconnect())
            notification_handlers.clear()

        # Use cached BLEDevice object instead of address string
        # This keeps the BlueZ D-Bus object alive and avoids "device not found"
        device = discovered_devices.get(address, address)
        client = BleakClient(device, disconnected_callback=_on_disconnect)
        run_async(client.connect())
        ble_client = client
        connected_device_address = address

        services = _get_services(client)
        emit("connected", {
            "address": address,
            "services": services,
        })
    except Exception as e:
        emit("error", {"message": f"Connect failed: {e}"})


def _on_disconnect(client):
    global ble_client, connected_device_address, notification_handlers
    ble_client = None
    connected_device_address = None
    notification_handlers.clear()
    socketio.emit("disconnected", {"address": client.address})


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


@socketio.on("disconnect_device")
def handle_disconnect():
    global ble_client, notification_handlers
    try:
        if ble_client and ble_client.is_connected:
            run_async(ble_client.disconnect())
        notification_handlers.clear()
        ble_client = None
        emit("disconnected", {"address": connected_device_address})
    except Exception as e:
        emit("error", {"message": f"Disconnect failed: {e}"})


# --- Read / Write / Notify ---

@socketio.on("read_char")
def handle_read(data):
    uuid = data["uuid"]
    try:
        if not ble_client or not ble_client.is_connected:
            emit("error", {"message": "Not connected"})
            return
        value = run_async(ble_client.read_gatt_char(uuid))
        emit("char_value", {
            "uuid": uuid,
            "value": value.hex(),
            "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "direction": "read",
        })
    except Exception as e:
        emit("error", {"message": f"Read failed: {e}"})


@socketio.on("write_char")
def handle_write(data):
    uuid = data["uuid"]
    value_hex = data["value"]
    write_type = data.get("type", "with_response")
    try:
        if not ble_client or not ble_client.is_connected:
            emit("error", {"message": "Not connected"})
            return
        value_bytes = bytes.fromhex(value_hex)
        response = write_type == "with_response"
        run_async(ble_client.write_gatt_char(uuid, value_bytes, response=response))
        emit("write_ok", {
            "uuid": uuid,
            "value": value_hex,
            "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3],
        })
    except Exception as e:
        emit("error", {"message": f"Write failed: {e}"})


@socketio.on("toggle_notify")
def handle_notify(data):
    uuid = data["uuid"]
    enable = data["enable"]
    try:
        if not ble_client or not ble_client.is_connected:
            emit("error", {"message": "Not connected"})
            return

        if enable:
            def callback(sender: BleakGATTCharacteristic, value: bytearray):
                socketio.emit("notification", {
                    "uuid": str(sender.uuid),
                    "value": value.hex(),
                    "timestamp": datetime.now().strftime("%H:%M:%S.%f")[:-3],
                })

            run_async(ble_client.start_notify(uuid, callback))
            notification_handlers[uuid] = callback
            emit("notify_status", {"uuid": uuid, "enabled": True})
        else:
            run_async(ble_client.stop_notify(uuid))
            notification_handlers.pop(uuid, None)
            emit("notify_status", {"uuid": uuid, "enabled": False})
    except Exception as e:
        emit("error", {"message": f"Notify toggle failed: {e}"})


@socketio.on("read_descriptor")
def handle_read_descriptor(data):
    handle = data["handle"]
    try:
        if not ble_client or not ble_client.is_connected:
            emit("error", {"message": "Not connected"})
            return
        value = run_async(ble_client.read_gatt_descriptor(handle))
        emit("descriptor_value", {
            "handle": handle,
            "value": value.hex(),
        })
    except Exception as e:
        emit("error", {"message": f"Read descriptor failed: {e}"})


if __name__ == "__main__":
    print("\n  BLE Debugger starting...")
    print("  Open http://localhost:5555 in your browser\n")
    socketio.run(app, host="0.0.0.0", port=5555, debug=False, allow_unsafe_werkzeug=True)
