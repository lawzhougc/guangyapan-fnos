#!/usr/bin/env python3
import sys
import os
import signal
import glob

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from guangyapan.web_server import run_server

def handle_sig(sig, frame):
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)
    port = int(os.environ.get("PORT", 8998))
    config_path = os.environ.get("CONFIG_PATH", "")
    if not config_path or not os.path.exists(config_path):
        candidate = os.path.join(BASE_DIR, "config.json")
        if os.path.exists(candidate):
            config_path = candidate
        elif os.path.exists("/var/apps/guangyapan/var/config.json"):
            config_path = "/var/apps/guangyapan/var/config.json"
        elif os.path.exists("/var/apps/guangyapan/target/config.json"):
            config_path = "/var/apps/guangyapan/target/config.json"
        else:
            candidates = glob.glob("/vol*/@appdata/guangyapan/config.json") + glob.glob("/usr/local/apps/@appdata/guangyapan/config.json")
            config_path = candidates[0] if candidates else os.path.join(BASE_DIR, "config.json")
    run_server(port=port, config_path=config_path)

