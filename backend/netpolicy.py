"""
netpolicy.py — which network connections the POS accepts, and a check that the approved one still exists.

Two checks run on EVERY request, first of all (stage 1 of the pipeline):
  A. WHO is calling: the caller must be this computer itself or a computer on a private (shop) network. Anything
     arriving from a public address is refused. Always on.
  B. WHERE it arrived: if the owner chose an "approved connection" (an IP address of this computer, e.g. its Ethernet
     address), requests that arrive through any other network interface of this computer are refused. Empty list = any
     interface. Calls from this computer itself (localhost) are always accepted, so a wrong choice can never lock the
     owner out of the screen on the POS computer.

The owner changes the approved connection in Settings → Network (after re-typing the password). If the cable breaks or
the address changes, alerts.py raises a critical alert and the owner picks the current connection there: no call to
the developers needed. Only a developer can switch the protection off completely (maintenance), and that is shown as
an alert for as long as it stays off.

Not verified by Claude: real Ethernet/Wi-Fi switching on Windows (the logic is tested with simulated addresses).
"""
import ipaddress
import os
import socket
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, SecretStr

import context
import credentials
import settings
from database import audit, transaction

router = APIRouter()


def _ip(host: Optional[str]):
    try:
        ip = ipaddress.ip_address((host or "").split("%")[0])
    except ValueError:
        if os.environ.get("POS_TEST_MODE") == "1" and host in ("testclient", "testserver"):
            return ipaddress.ip_address("127.0.0.1")
        return None
    return ip.ipv4_mapped if getattr(ip, "ipv4_mapped", None) else ip


def kind(ip) -> str:
    if ip.is_loopback:
        return "loopback"
    return "private" if (ip.is_private or ip.is_link_local) else "public"


def _blocked(message: str) -> HTTPException:
    return HTTPException(403, {"code": "network_blocked", "message": message, "override": False})


def check(request: Request) -> None:
    """Stage 1 of the pipeline. Raises 403 network_blocked."""
    pol = settings.get("network")
    if not pol.get("enforce", True):
        return
    client = _ip(request.client.host if request.client else "")
    if client is None:
        raise _blocked("This connection could not be identified, so it was refused.")
    k = kind(client)
    if k == "public":
        raise _blocked("Only computers on the shop's own network may connect to the POS.")
    if k == "loopback":
        return
    approved = pol.get("approved_ips") or []
    if approved:
        srv = _ip((request.scope.get("server") or ("",))[0])
        if srv is not None and not srv.is_loopback and str(srv) not in approved:
            raise _blocked("This computer is not connected through the shop's approved network connection. "
                           "Ask the owner to check Settings → Network.")


# ───────────── what this computer has ─────────────
def local_ips() -> list[str]:
    ips = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))               # UDP "connect" sends nothing: it only asks which address would be used
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


# ── which address is Ethernet and which is Wi-Fi (labels only: the rules still work by address) ──
_ADAPTER_CACHE: dict = {"t": 0.0, "data": {}}


def parse_linux_adapters(ip_out: str, wireless: set[str]) -> dict[str, dict]:
    """`ip -4 -o addr` lines look like:  2: eth0    inet 192.168.1.20/24 brd ... ;  `wireless` = interface names that have /sys/class/net/<n>/wireless"""
    out: dict[str, dict] = {}
    for line in ip_out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "inet":
            name, ip = parts[1], parts[3].split("/")[0]
            out[ip] = {"name": name, "type": "Wi-Fi" if name in wireless else ("This computer" if name == "lo" else "Ethernet")}
    return out


def parse_windows_adapters(js: str) -> dict[str, dict]:
    """PowerShell: Get-NetAdapter | Get-NetIPAddress -AddressFamily IPv4 joined with the adapter's MediaType, as a JSON list of {ip, name, media}."""
    import json as _json
    data = _json.loads(js) if js.strip() else []
    if isinstance(data, dict):
        data = [data]
    out: dict[str, dict] = {}
    for d in data:
        media, name = str(d.get("media", "")), str(d.get("name", ""))
        low = (media + " " + name).lower()
        out[str(d.get("ip"))] = {"name": name, "type": "Wi-Fi" if ("802.11" in low or "wi-fi" in low or "wifi" in low or "wireless" in low) else "Ethernet"}
    return out


def adapter_info() -> dict[str, dict]:
    """ip -> {name, type}. Best effort and cached 30 s; any failure just means 'unknown' (the owner still picks by address)."""
    import subprocess, sys, time as _t
    if _t.time() - _ADAPTER_CACHE["t"] < 30:
        return _ADAPTER_CACHE["data"]
    data: dict[str, dict] = {}
    try:
        if sys.platform.startswith("linux"):
            r = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=3)
            names = os.listdir("/sys/class/net") if os.path.isdir("/sys/class/net") else []
            data = parse_linux_adapters(r.stdout, {n for n in names if os.path.isdir(f"/sys/class/net/{n}/wireless")})
        elif sys.platform == "win32":
            ps = ("$a=Get-NetAdapter | Where-Object Status -eq 'Up'; $r=@(); foreach($x in $a){ Get-NetIPAddress -InterfaceIndex $x.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
                  "ForEach-Object { $r += [pscustomobject]@{ip=$_.IPAddress; name=$x.Name; media=[string]$x.PhysicalMediaType} } }; $r | ConvertTo-Json -Compress")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=8)
            data = parse_windows_adapters(r.stdout)
    except Exception:                                   # noqa: BLE001 — labels are a convenience, never a reason to fail
        data = {}
    _ADAPTER_CACHE.update(t=_t.time(), data=data)
    return data


def is_present(ip: str) -> bool:
    """True if this computer currently owns that address (a pulled cable or switched-off Wi-Fi makes it vanish)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def missing_interfaces() -> list[str]:
    pol = settings.get("network")
    if not pol.get("enforce", True):
        return []
    return [ip for ip in (pol.get("approved_ips") or []) if not is_present(ip)]


# ───────────── routes ─────────────
class PolicyIn(BaseModel):
    approved_ips: list[str]
    password: SecretStr


class OverrideIn(BaseModel):
    enforce: bool


@router.get("/network")
def network_status():
    pol = settings.get("network")
    approved = pol.get("approved_ips") or []
    info = adapter_info()
    detected = [{"ip": ip, "kind": kind(ipaddress.ip_address(ip)), "approved": ip in approved,
                 "type": info.get(ip, {}).get("type"), "name": info.get(ip, {}).get("name")} for ip in local_ips()]
    return {"enforce": pol.get("enforce", True), "approved_ips": approved, "missing": missing_interfaces(), "detected": detected,
            "bind_host": os.environ.get("POS_BIND_HOST", "127.0.0.1")}


@router.post("/network/policy")
def set_policy(b: PolicyIn):
    me = context.current_user()
    credentials.check_my_password(me["id"], b.password.get_secret_value())
    ips: list[str] = []
    for raw in b.approved_ips:
        try:
            ip = ipaddress.ip_address(raw.strip())
        except ValueError:
            raise HTTPException(422, f"“{raw}” is not an IP address.") from None
        if ip.version != 4 or kind(ip) == "public":
            raise HTTPException(422, f"{ip} is not a shop-network (private IPv4) address.")
        if not is_present(str(ip)):
            raise HTTPException(422, f"{ip} is not an address of this computer right now, so it was not saved "
                                     "(that could lock the tills out). Pick one from the detected list.")
        if str(ip) not in ips:
            ips.append(str(ip))
    if len(ips) > 8:
        raise HTTPException(422, "At most 8 addresses.")
    with transaction():
        old = settings.get("network")
        settings.put("network", {**old, "approved_ips": ips}, me["username"])
        audit("network.policy_changed", "network", "approved", f"Approved network connection: {old.get('approved_ips') or 'any'} → {ips or 'any'}",
              {"old": old.get("approved_ips"), "new": ips})
    import alerts
    alerts.monitor_once(debounce=1)               # a fixed connection clears its alert straight away
    return {"ok": True, "approved_ips": ips}


@router.post("/network/override")
def override(b: OverrideIn):
    me = context.current_user()
    with transaction():
        old = settings.get("network")
        settings.put("network", {**old, "enforce": b.enforce}, me["username"])
        audit("network.protection", "network", "enforce", f"Network protection switched {'ON' if b.enforce else 'OFF'} by the developer", {"enforce": b.enforce})
    import alerts
    alerts.monitor_once(debounce=1)
    return {"ok": True, "enforce": b.enforce}
