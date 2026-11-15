#!/usr/bin/env python3
"""TLS for the sandbox's mock portals. DEV ONLY; nothing here is shipped.

The mocks answer under the portals' real names (account.sseairtricity.com,
myaccount.esbnetworks.ie, login.esbnetworks.ie) inside the sandbox's Docker network,
so the integration runs exactly as installed, with no "point at a test server"
setting. For its HTTPS to succeed, the sandbox's Home Assistant trusts one extra,
throwaway certificate authority made here:

    certs/ca.pem          the sandbox CA (valid 30 days, key discarded after signing)
    certs/mock.pem        the mocks' certificate for the three names
    certs/mock-key.pem    its key
    certs/bundle.pem      the public CAs Home Assistant normally trusts + the sandbox CA

docker-compose.yml hands bundle.pem to the sandbox only, as REQUESTS_CA_BUNDLE, which
Home Assistant's HTTP client honours. Your own machine and browser are not touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

HERE = Path(__file__).resolve().parent
OUT = HERE / "certs"
NAMES = ("account.sseairtricity.com", "myaccount.esbnetworks.ie", "login.esbnetworks.ie")
LIFETIME = timedelta(days=30)


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def fresh() -> bool:
    """Certificates exist, cover today's names and have a week left."""
    try:
        cert = x509.load_pem_x509_certificate((OUT / "mock.pem").read_bytes())
    except (FileNotFoundError, ValueError):
        return False
    names = set(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName))
    return names == set(NAMES) and cert.not_valid_after_utc - datetime.now(UTC) > timedelta(days=7) and (OUT / "bundle.pem").exists()


def make() -> None:
    OUT.mkdir(exist_ok=True)
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder()
        .subject_name(_name("Electricity Analyzer sandbox CA (dev only)"))
        .issuer_name(_name("Electricity Analyzer sandbox CA (dev only)"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + LIFETIME)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name(NAMES[0]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + LIFETIME)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in NAMES]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = ca.public_bytes(serialization.Encoding.PEM)
    (OUT / "ca.pem").write_bytes(ca_pem)
    (OUT / "mock.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_file = OUT / "mock-key.pem"
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    key_file.chmod(0o600)
    (OUT / "bundle.pem").write_bytes(Path(certifi.where()).read_bytes().rstrip() + b"\n" + ca_pem)
    # The CA key is never written: nothing else can ever be signed with it.


def ensure() -> None:
    if not fresh():
        make()
        print(f"      made sandbox TLS certificates in {OUT.relative_to(HERE.parent)}")


if __name__ == "__main__":
    make()
    print(f"written to {OUT}")
