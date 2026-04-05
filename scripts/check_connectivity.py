import subprocess
import re
import socket
import http.client

def check_port(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', port)) == 0

def get_tunnel_url():
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", "100", "bezos-tunnel"],
            capture_output=True, text=True, timeout=10
        )
        combined = result.stdout + result.stderr
        urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", combined)
        return urls[-1] if urls else None
    except:
        return None

print(f"Port 8002 open: {check_port(8002)}")
print(f"Tunnel URL from logs: {get_tunnel_url()}")

try:
    conn = http.client.HTTPConnection("localhost", 8002)
    conn.request("GET", "/agent.json")
    resp = conn.getresponse()
    print(f"Local agent.json: {resp.status} {resp.reason}")
except Exception as e:
    print(f"Local agent.json error: {e}")
