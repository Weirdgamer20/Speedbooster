import subprocess
import json

def get_hw_map():
    cmd = ['powershell.exe', '-NoProfile', '-Command', 
           'Get-NetAdapter | Select-Object Name, InterfaceDescription, MacAddress | ConvertTo-Json -Compress']
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        res = {}
        for item in data:
            mac = (item.get('MacAddress') or '').replace('-', ':').lower()
            if mac:
                res[mac] = {
                    'name': item.get('Name'),
                    'desc': item.get('InterfaceDescription')
                }
        return res
    except Exception as e:
        print('Error:', e)
        return {}

hw_map = get_hw_map()
for mac, info in hw_map.items():
    print(f"MAC {mac} -> {info['name']}: {info['desc']}")
