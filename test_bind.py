import socket

def test_iface(iface_name):
    print(f"Testing {iface_name}...")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, 25, iface_name.encode())
        s.settimeout(5.0)
        s.connect(("api.ipify.org", 80))
        s.sendall(b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n")
        response = b""
        while True:
            chunk = s.recv(1024)
            if not chunk:
                break
            response += chunk
        s.close()
        body = response.split(b"\r\n\r\n")[-1].decode().strip()
        print(f"  [SUCCESS] {iface_name} -> Public IP: {body}")
        return body
    except Exception as e:
        print(f"  [ERROR] {iface_name} -> {e}")
        return None

if __name__ == "__main__":
    test_iface("eth3")
    test_iface("eth4")
