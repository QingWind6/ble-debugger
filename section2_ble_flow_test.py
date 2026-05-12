#!/usr/bin/env python3
"""Automate the section-2 BLE workflow through the local BLE debugger HTTP API."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_BASE_URL = "http://127.0.0.1:5555"


class ApiClient:
    def __init__(self, base_url: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict | None = None, query: dict | None = None):
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        payload = None
        headers = {}
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {raw}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Cannot reach BLE debugger API at {self.base_url}. "
                f"Make sure `python app.py` is running. Detail: {exc}"
            ) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{method} {path} returned non-JSON data: {raw}") from exc

        if not data.get("ok", False):
            raise RuntimeError(f"{method} {path} failed: {data.get('error', raw)}")
        return data.get("result")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run the section-2 BLE flow against a target device via the local BLE debugger API.",
    )
    parser.add_argument("--address", required=True, help="Target BLE MAC address, e.g. 44:1b:f6:83:8e:3a")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="BLE debugger base URL")
    parser.add_argument(
        "--mode",
        choices=["basic", "provision", "unbind"],
        default="basic",
        help="basic=deviceinfo+wifitable, provision=bindstate(1)+wifitable+wifi, unbind=bindstate(0)",
    )
    parser.add_argument("--write-uuid", help="Override detected write characteristic UUID")
    parser.add_argument("--notify-uuid", help="Override detected notify characteristic UUID")
    parser.add_argument("--scan-timeout", type=float, default=20.0, help="Seconds to wait for scan results")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="Polling interval during scan")
    parser.add_argument("--api-timeout", type=float, default=10.0, help="HTTP timeout per BLE debugger API call")
    parser.add_argument("--command-timeout-ms", type=int, default=5000, help="Wait timeout for each AT response")
    parser.add_argument("--bind-state", type=int, choices=[0, 1], help="Explicit bound state to send")
    parser.add_argument("--wifi-ssid", help="SSID used for AT+wifi")
    parser.add_argument("--wifi-password", help="Password used for AT+wifi")
    parser.add_argument("--skip-scan", action="store_true", help="Skip active scanning and connect directly")
    parser.add_argument("--keep-connected", action="store_true", help="Do not disconnect at the end")
    parser.add_argument("--json", action="store_true", help="Print the full report JSON at the end")
    return parser.parse_args()


def trim_text(value: str, limit: int = 200) -> str:
    clean = value.replace("\r", "\\r").replace("\n", "\\n")
    return clean if len(clean) <= limit else f"{clean[:limit]}..."


def mark_step(report: list[dict], name: str, status: str, detail: str, extra: dict | None = None):
    entry = {"name": name, "status": status, "detail": detail}
    if extra:
        entry["extra"] = extra
    report.append(entry)
    return entry


def infer_bind_state(mode: str, bind_state: int | None):
    if bind_state is not None:
        return bind_state
    if mode == "provision":
        return 1
    if mode == "unbind":
        return 0
    return None


def start_scan_and_wait_for_device(client: ApiClient, address: str, timeout_s: float, poll_interval_s: float):
    client.request("POST", "/api/scan/start")
    deadline = time.monotonic() + timeout_s
    last_devices = []
    while time.monotonic() < deadline:
        result = client.request("GET", "/api/scan/results")
        devices = result.get("devices", [])
        last_devices = devices
        for device in devices:
            if device.get("address", "").lower() == address.lower():
                return device
        time.sleep(poll_interval_s)
    addresses = ", ".join(device.get("address", "?") for device in last_devices[:10])
    raise RuntimeError(f"Device {address} was not found during scan. Last results: {addresses or 'none'}")


def collect_service_candidates(services: list[dict]):
    groups = []
    for svc in services:
        write_chars = []
        notify_chars = []
        for char in svc.get("characteristics", []):
            props = set(char.get("properties", []))
            if "write" in props:
                write_chars.append(("write", char))
            elif "write-without-response" in props:
                write_chars.append(("write-without-response", char))

            if "notify" in props:
                notify_chars.append(("notify", char))
            elif "indicate" in props:
                notify_chars.append(("indicate", char))

        if write_chars and notify_chars:
            groups.append({
                "service_uuid": svc.get("uuid"),
                "write_chars": write_chars,
                "notify_chars": notify_chars,
            })
    return groups


def resolve_characteristics(services: list[dict], write_uuid: str | None, notify_uuid: str | None):
    if write_uuid and notify_uuid:
        return write_uuid, notify_uuid

    candidates = collect_service_candidates(services)
    if not candidates:
        raise RuntimeError("No service exposes both write and notify/indicate characteristics.")

    if len(candidates) != 1:
        summary = []
        for group in candidates:
            write_list = [char["uuid"] for _, char in group["write_chars"]]
            notify_list = [char["uuid"] for _, char in group["notify_chars"]]
            summary.append(
                f"service={group['service_uuid']} write={write_list} notify={notify_list}"
            )
        raise RuntimeError(
            "Multiple candidate services were found. Re-run with --write-uuid and --notify-uuid. "
            + " | ".join(summary)
        )

    group = candidates[0]
    if write_uuid is None:
        if len(group["write_chars"]) != 1:
            raise RuntimeError(
                f"Write characteristic is ambiguous for service {group['service_uuid']}. "
                f"Candidates: {[char['uuid'] for _, char in group['write_chars']]}"
            )
        write_uuid = group["write_chars"][0][1]["uuid"]

    if notify_uuid is None:
        if len(group["notify_chars"]) != 1:
            raise RuntimeError(
                f"Notify characteristic is ambiguous for service {group['service_uuid']}. "
                f"Candidates: {[char['uuid'] for _, char in group['notify_chars']]}"
            )
        notify_uuid = group["notify_chars"][0][1]["uuid"]

    return write_uuid, notify_uuid


def build_bindstate_command(bound: int):
    payload = json.dumps({"data": {"bound": bound}}, separators=(",", ":"))
    return f"AT+bindstate={payload}"


def build_wifi_command(ssid: str, password: str):
    payload = json.dumps({"ssid": ssid, "password": password}, separators=(",", ":"), ensure_ascii=False)
    return f"AT+wifi={payload}"


def run_at_command(client: ApiClient, write_uuid: str, notify_uuid: str, command: str, timeout_ms: int):
    return client.request(
        "POST",
        "/api/at/command",
        {
            "write_uuid": write_uuid,
            "notify_uuid": notify_uuid,
            "command": command,
            "with_response": True,
            "timeout_ms": timeout_ms,
            "auto_enable_notify": True,
            "clear_buffers": True,
        },
    )


def build_command_plan(args):
    commands = [("deviceinfo", "AT+deviceinfo?")]

    if args.mode in {"basic", "provision"}:
        commands.append(("wifitable", "AT+wifitable?"))

    bind_state = infer_bind_state(args.mode, args.bind_state)
    if bind_state is not None:
        commands.append((f"bindstate_{bind_state}", build_bindstate_command(bind_state)))

    if args.wifi_ssid or args.wifi_password:
        if not (args.wifi_ssid and args.wifi_password):
            raise RuntimeError("--wifi-ssid and --wifi-password must be provided together")
        commands.append(("wifi", build_wifi_command(args.wifi_ssid, args.wifi_password)))

    return commands


def print_human_report(report: dict):
    print(f"Target: {report['address']}")
    print(f"Mode: {report['mode']}")
    print(f"Overall: {'PASS' if report['passed'] else 'FAIL'}")
    print()
    for step in report["steps"]:
        print(f"[{step['status']}] {step['name']}: {step['detail']}")


def main():
    args = parse_args()
    client = ApiClient(args.base_url, args.api_timeout)

    report = {
        "address": args.address,
        "mode": args.mode,
        "passed": False,
        "steps": [],
    }
    scan_started = False

    try:
        health = client.request("GET", "/api/health")
        mark_step(report["steps"], "health", "PASS", "BLE debugger API is reachable", {"state": health.get("state")})

        # Reset stale local state from previous runs before starting a new flow.
        try:
            client.request("POST", "/api/scan/stop")
        except Exception:
            pass
        try:
            client.request("POST", "/api/disconnect")
        except Exception:
            pass

        if not args.skip_scan:
            scan_started = True
            device = start_scan_and_wait_for_device(client, args.address, args.scan_timeout, args.poll_interval)
            detail = f"found {device.get('address')} RSSI={device.get('rssi')}"
            product_info = device.get("product_info")
            if product_info:
                detail += f" product={product_info.get('display_value') or product_info.get('raw')}"
            mark_step(report["steps"], "scan", "PASS", detail, {"device": device})
        else:
            mark_step(report["steps"], "scan", "SKIP", "scan skipped by flag")

        connect_result = client.request("POST", "/api/connect", {"address": args.address})
        if scan_started:
            try:
                client.request("POST", "/api/scan/stop")
            except Exception:
                pass
            scan_started = False
        services = connect_result.get("services", [])
        write_uuid, notify_uuid = resolve_characteristics(services, args.write_uuid, args.notify_uuid)
        mark_step(
            report["steps"],
            "connect",
            "PASS",
            f"connected, services={len(services)}, write_uuid={write_uuid}, notify_uuid={notify_uuid}",
            {"services": services},
        )

        client.request("DELETE", "/api/events")
        notify_result = client.request("POST", "/api/notify", {"uuid": notify_uuid, "enable": True})
        mark_step(report["steps"], "notify", "PASS", f"notify enabled on {notify_result['uuid']}")

        for name, command in build_command_plan(args):
            result = run_at_command(client, write_uuid, notify_uuid, command, args.command_timeout_ms)
            frame = result["frame"]
            detail = trim_text(frame.get("text", ""))
            mark_step(
                report["steps"],
                name,
                "PASS",
                detail,
                {"command": command, "frame": frame, "write": result.get("write")},
            )

        if not args.keep_connected:
            disconnect = client.request("POST", "/api/disconnect")
            mark_step(report["steps"], "disconnect", "PASS", f"disconnected from {disconnect.get('address')}")
        else:
            mark_step(report["steps"], "disconnect", "SKIP", "connection kept open by flag")

        report["passed"] = True
        print_human_report(report)
        if args.json:
            print()
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        mark_step(report["steps"], "failure", "FAIL", str(exc))
        print_human_report(report)
        if args.json:
            print()
            print(json.dumps(report, ensure_ascii=False, indent=2))

        if not args.keep_connected:
            try:
                client.request("POST", "/api/disconnect")
            except Exception:
                pass
        if scan_started:
            try:
                client.request("POST", "/api/scan/stop")
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
