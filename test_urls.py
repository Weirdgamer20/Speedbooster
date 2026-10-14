import requests
from requests.adapters import HTTPAdapter

class TestAdapter(HTTPAdapter):
    def __init__(self, source_ip, *args, **kwargs):
        self.source_ip = source_ip
        super().__init__(*args, **kwargs)
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs['source_address'] = (self.source_ip, 0)
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

urls = [
    'https://www.python.org/ftp/python/3.12.0/python-3.12.0-amd64.exe',
    'http://speedtest.tele2.net/100MB.zip'
]

for u in urls:
    print('Testing URL:', u)
    for ip, dev in [('192.168.1.9', 'eth1'), ('192.168.1.6', 'eth2')]:
        s = requests.Session()
        s.mount('http://', TestAdapter(ip))
        s.mount('https://', TestAdapter(ip))
        try:
            r = s.get(u, headers={'Range': 'bytes=0-10', 'User-Agent': 'Mozilla/5.0'}, timeout=5)
            print(f'  {dev} ({ip}) -> STATUS {r.status_code} {r.headers.get("Content-Range")}')
        except Exception as e:
            print(f'  {dev} ({ip}) -> ERROR: {e}')
