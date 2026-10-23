import os
import sys
from guangyapan.web_server import run_server

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8998))
    config_path = os.environ.get("CONFIG_PATH", "config.json")
    run_server(port=port, config_path=config_path)
