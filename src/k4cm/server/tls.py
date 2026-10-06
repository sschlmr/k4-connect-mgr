"""Self-signed TLS certificate generation for the server.

The site has no public CA-issued certificate, so `k4cm-server init` mints a
long-lived self-signed one covering the public hostname (mh01.net by default)
plus any extra names or IPs. Clients pin it by fingerprint.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

DEFAULT_VALID_DAYS = 3650


def _san_entry(name: str):
    try:
        return x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        return x509.DNSName(name)


def generate_self_signed(
    cert_path: Path,
    key_path: Path,
    hostnames: list[str],
    valid_days: int = DEFAULT_VALID_DAYS,
) -> str:
    """Write a self-signed cert/key pair and return the cert's SHA-256 fingerprint."""
    if not hostnames:
        raise ValueError("at least one hostname or IP is required for the certificate")

    cert_path, key_path = Path(cert_path), Path(key_path)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0]),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "K4 Connect Manager"),
        ]
    )
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=valid_days))
        .add_extension(x509.SubjectAlternativeName([_san_entry(h) for h in hostnames]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    try:
        key_path.chmod(0o600)
    except OSError:
        pass  # Windows ACLs govern instead

    return fingerprint(cert_path)


def fingerprint(cert_path: Path) -> str:
    """SHA-256 fingerprint of a PEM certificate, colon-separated uppercase hex."""
    pem = Path(cert_path).read_bytes()
    cert = x509.load_pem_x509_certificate(pem)
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))
