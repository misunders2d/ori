import socket
import subprocess

def test_check_ports():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(('127.0.0.1', 8000))
    if result == 0:
        print("Port 8000 is OPEN on 127.0.0.1")
    else:
        print(f"Port 8000 is CLOSED on 127.0.0.1 (Error: {result})")
    sock.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(('0.0.0.0', 8000))
    if result == 0:
        print("Port 8000 is OPEN on 0.0.0.0")
    else:
        print(f"Port 8000 is CLOSED on 0.0.0.0 (Error: {result})")
    sock.close()
    
    # Check if anything is listening on 8000
    try:
        output = subprocess.check_output(["netstat", "-tuln"], stderr=subprocess.STDOUT, text=True)
        print("NETSTAT OUTPUT:")
        print(output)
    except Exception as e:
        print(f"NETSTAT FAILED: {e}")
