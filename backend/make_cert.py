"""
make_cert.py — makes a certificate so the POS can run over HTTPS (PINs and passwords are then encrypted on the shop network).

    python make_cert.py                      (run in backend\, once)
    python make_cert.py --host 192.168.1.20  (add the POS computer's shop-network address so other tills accept it)

It writes certs\pos.pem and certs\pos-key.pem. Once they exist, start.bat / start.sh start the POS over https:// by themselves.
The certificate is self-signed: each browser shows a warning ONCE ("not private"). Open the POS address, choose Advanced -> Continue,
and the warning is gone for that browser. Delete the certs folder to go back to plain http.
"""
import argparse
import datetime as dt
import ipaddress
import os
import socket
import sys

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
except ImportError:
    sys.exit("This needs one extra package:  python -m pip install cryptography   then run this again.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", action="append", default=[], help="an extra IP address or name the certificate must be valid for (repeatable)")
    ap.add_argument("--days", type=int, default=825)
    a = ap.parse_args()
    names = {"localhost", socket.gethostname()}
    ips = {"127.0.0.1"}
    for h in a.host:
        try:
            ips.add(str(ipaddress.ip_address(h)))
        except ValueError:
            names.add(h)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "POS")])
    now = dt.datetime.now(dt.timezone.utc)
    san = [x509.DNSName(n) for n in sorted(names)] + [x509.IPAddress(ipaddress.ip_address(i)) for i in sorted(ips)]
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=a.days))
            .add_extension(x509.SubjectAlternativeName(san), critical=False).add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
    os.makedirs(here, exist_ok=True)
    with open(os.path.join(here, "pos-key.pem"), "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    try:
        os.chmod(os.path.join(here, "pos-key.pem"), 0o600)
    except OSError:
        pass
    with open(os.path.join(here, "pos.pem"), "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    print("Certificate written to", here)
    print("Valid for:", ", ".join(sorted(names) + sorted(ips)))
    print("Next: close the POS windows and double-click start.bat again. Open https://localhost:5173 and accept the one-time browser warning.")


if __name__ == "__main__":
    main()
