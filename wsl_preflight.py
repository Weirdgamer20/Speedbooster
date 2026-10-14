"""WSL pre-flight network validation script."""
import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from network_manager import NetworkManager

nm = NetworkManager()
nm.discover_interfaces()
r = nm.validate_paths()

print("\n" + "="*60)
print("  SpeedBond WSL Pre-Flight Network Check")
print("="*60)
ifaces = list(nm.interfaces.items())
print(f"  Interfaces found: {len(ifaces)}")
for n, i in ifaces:
    print(f"  [{n}]  ip={i.ip}  gw={i.gateway}  pub={i.public_ip}  latency={i.latency_ms:.0f}ms  hw={i.hw_desc}")
print()
print(f"  Independent Egress: {r['independent_egress_proven']}")
print(f"  Status: {r['status_message']}")
print("="*60)
