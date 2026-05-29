<div align="center">

# BLE Debugger

**Web-based Bluetooth Low Energy debugging tool**

一个通用的浏览器 BLE 调试工具，支持扫描、广播数据查看、GATT 调试、通知订阅、REST API、中英双语与深色/护眼双主题。

A generic browser-based BLE debugging tool with advertisement inspection, GATT workflows, notifications, REST API access, bilingual UI, and dual themes.

![Python](https://img.shields.io/badge/Python-3.10+-3776ab?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.0+-000?logo=flask)
![License](https://img.shields.io/badge/License-MIT-green)

</div>

---

## Features / 功能一览

<p align="center">
  <img src="docs/features.svg" width="800" alt="Features overview">
</p>

| Feature | Description |
|---------|-------------|
| **BLE Scan** | Real-time device discovery with RSSI signal strength indicators (color-coded) |
| **Advertisement Inspector** | Inspect local name, service UUIDs, Service Data, Manufacturer Data, TX power, connectable state, and reconstructed raw AD payloads |
| **GATT Explorer** | Browse services, characteristics, and descriptors in a collapsible tree view |
| **Read / Write / Notify** | Read characteristic values, write in HEX or Text mode, subscribe to notifications |
| **Communication Log** | Timestamped log with direction tags (RX / TX / NOTIFY) and auto hex-to-text decode |
| **Frame View** | Reassemble common line / OK-style notification frames for easier debugging |
| **REST API** | Automate scan, connect, read, write, notify, event polling, and command exchange workflows |
| **Bilingual UI** | Switch between 中文 and English with one click, preference saved locally |
| **Dual Themes** | Dark mode and Eyecare (warm beige) mode, preference saved locally |

---

## Preview / 界面预览

### Dark Theme / 深色主题

<p align="center">
  <img src="docs/preview-dark.svg" width="960" alt="Dark theme preview">
</p>

### Eyecare Theme / 护眼主题

<p align="center">
  <img src="docs/preview-eyecare.svg" width="960" alt="Eyecare theme preview">
</p>

---

## Quick Start / 快速开始

### 1. Install dependencies / 安装依赖

```bash
pip install -r requirements.txt
```

> Requires Python 3.10+ and a Bluetooth adapter on your system.

### 2. Run / 运行

```bash
python3 app.py
```

### 3. Open browser / 打开浏览器

Visit [http://localhost:5555](http://localhost:5555)

---

## Usage / 使用方法

```
1. Click "Start Scan" to discover nearby BLE devices
   点击「开始扫描」发现附近的 BLE 设备

2. Click a device to connect and browse its GATT services
   点击设备进行连接，浏览其 GATT 服务

3. Select a characteristic to read, write, or subscribe to notifications
   选择特征值进行读取、写入或订阅通知

4. Expand a scan result to inspect generic advertisement data
   展开扫描结果，查看通用广播数据

5. All communication is logged in the right panel with timestamps
   所有通信记录会在右侧面板带时间戳地显示
```

---

## REST API / 自动化接口

All API responses use a common JSON envelope: `{ "ok": true, "result": ... }` for success and `{ "ok": false, "error": "..." }` for failures.

所有接口成功时返回 `{ "ok": true, "result": ... }`，失败时返回 `{ "ok": false, "error": "..." }`。

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | Health check and current state |
| `GET` | `/api/state` | Scan, connection, notify, and event state |
| `POST` | `/api/scan/start` | Start BLE scanning |
| `POST` | `/api/scan/stop` | Stop BLE scanning |
| `GET` | `/api/scan/results` | Latest scan results with advertisement details |
| `POST` | `/api/connect` | Connect to a BLE device by address |
| `POST` | `/api/disconnect` | Disconnect current device |
| `GET` | `/api/services` | List GATT services for the connected device |
| `POST` | `/api/read` | Read a characteristic |
| `POST` | `/api/write` | Write HEX or text to a characteristic |
| `POST` | `/api/notify` | Enable or disable notification / indication |
| `POST` | `/api/read_descriptor` | Read a descriptor by handle |
| `GET` / `DELETE` | `/api/events` | Read or clear notification / frame events |
| `POST` | `/api/exchange` | Write data and wait for a notification or frame |
| `POST` | `/api/command` | Write a text command and wait for a notification frame |

Example / 示例:

```bash
curl -X POST http://localhost:5555/api/write \
  -H 'Content-Type: application/json' \
  -d '{"uuid":"0000xxxx-0000-1000-8000-00805f9b34fb","value":"48656c6c6f","encoding":"hex","with_response":true}'
```

---

## Tech Stack / 技术栈

| Layer | Technology |
|-------|-----------|
| Backend | [Flask](https://flask.palletsprojects.com/) + [Flask-SocketIO](https://flask-socketio.readthedocs.io/) |
| BLE | [Bleak](https://bleak.readthedocs.io/) (cross-platform BLE library) |
| Frontend | Vanilla HTML/CSS/JS + Socket.IO client |
| Real-time | WebSocket via Socket.IO |

---

## Project Structure / 项目结构

```
ble-debugger/
├── app.py              # Flask backend + BLE logic
├── requirements.txt    # Python dependencies
├── templates/
│   └── index.html      # Single-page frontend (i18n + themes built-in)
└── docs/
    ├── features.svg
    ├── preview-dark.svg
    └── preview-eyecare.svg
```

---

## Requirements / 系统要求

- Python 3.10+
- Bluetooth adapter (built-in or USB dongle)
- Linux: BlueZ 5.43+ (`sudo apt install bluez`)
- macOS: Built-in CoreBluetooth (no extra setup)
- Windows: Windows 10+

---

## License

MIT
