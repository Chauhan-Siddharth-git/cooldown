"""The CA is constrained to the hosts this box gates, and the constraint is enforced.

The SD card is unencrypted, so the key is copyable and that is accepted by design. What
is NOT accepted is the copy being worth a trust anchor for every site on the internet.
These tests assert the thing that limits the damage, and — because a name-constraints
extension that is present but ignored looks exactly like one that works — they assert it
by *verifying certificates*, not by reading the extension back out.
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "deploy"))

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None,
                                reason="needs the openssl CLI")


@pytest.fixture(scope="module")
def ca(tmp_path_factory):
    """One CA for the whole module — RSA keygen is not free, and every test below asks
    the same CA a different question."""
    d = tmp_path_factory.mktemp("ca")
    r = subprocess.run([os.path.join(ROOT, "deploy", "gen_ca.sh"), str(d)],
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, f"gen_ca.sh failed:\n{r.stdout}\n{r.stderr}"
    return d


def _verify(ca_dir, host, extra_san=None):
    """Sign a leaf for `host` with the CA and ask openssl to verify the chain.
    True = the CA is able to vouch for that host."""
    key, csr, crt = (str(ca_dir / f"probe.{x}") for x in ("key", "csr", "crt"))
    subprocess.run(["openssl", "req", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", key, "-out", csr, "-subj", f"/CN={host}"],
                   capture_output=True, timeout=60, check=True)
    ext = str(ca_dir / "probe.ext")
    with open(ext, "w") as f:
        san = f"DNS:{host}" + (f",{extra_san}" if extra_san else "")
        f.write(f"subjectAltName={san}\n")
    subprocess.run(["openssl", "x509", "-req", "-in", csr,
                    "-CA", str(ca_dir / "mitmproxy-ca-cert.pem"),
                    "-CAkey", str(ca_dir / "mitmproxy-ca.pem"),
                    "-CAcreateserial", "-out", crt, "-days", "1", "-extfile", ext],
                   capture_output=True, timeout=60, check=True)
    out = subprocess.run(["openssl", "verify", "-CAfile",
                          str(ca_dir / "mitmproxy-ca-cert.pem"), crt],
                         capture_output=True, text=True, timeout=60)
    return out.returncode == 0


# Hosts the box gates, so the CA must be able to vouch for them or browsing breaks.
IN_SCOPE = ["www.reddit.com", "reddit.com", "www.youtube.com", "open.spotify.com",
            "www.cnn.com", "bbc.co.uk", "www.bbc.co.uk", "m.facebook.com", "mitm.it"]

# Hosts it does not gate. A stolen key must be useless against every one of these.
# The last two are F4's shapes: a substring test would have matched them, and the DNS
# constraint semantics reject them for the same reason host_matches() does.
OUT_OF_SCOPE = ["mybank.example", "accounts.google.com", "login.microsoftonline.com",
                "github.com", "evil-reddit.com", "reddit.com.attacker.io"]


@pytest.mark.parametrize("host", IN_SCOPE)
def test_the_ca_can_vouch_for_every_gated_host(ca, host):
    """If this fails the box cannot intercept that site at all — the gate breaks, which
    is a louder failure than the one below but a failure all the same."""
    assert _verify(ca, host), f"{host} is gated but the CA cannot vouch for it"


@pytest.mark.parametrize("host", OUT_OF_SCOPE)
def test_a_stolen_ca_cannot_vouch_for_anything_else(ca, host):
    """The whole point. Whoever copies the card gets a key that works against the
    distraction sites and nothing else — not your bank, not your email."""
    assert not _verify(ca, host), (
        f"the CA signed a usable cert for {host}, which is outside its constraints — "
        f"a stolen key is a trust anchor for the whole internet again")


# The destination IP mitmproxy puts in the SAN in transparent mode. Any address works;
# these are just plausible ones.
TRANSPARENT_IPS = ["IP:142.250.190.46", "IP:151.101.65.140", "IP:2607:f8b0:4006:802::200e"]


@pytest.mark.parametrize("ip_san", TRANSPARENT_IPS)
def test_the_ca_can_vouch_for_a_transparently_proxied_host(ca, ip_san):
    """A leaf carrying the destination IP alongside the hostname must still validate.

    This is the shape mitmproxy actually mints for a transparently-proxied client, and it
    is not optional: addons/tlsconfig.py appends the server address to the SAN
    unconditionally, and in transparent mode that address is an IP from SO_ORIGINAL_DST.

    The CA used to carry `excluded;IP:0.0.0.0/0` on the reasoning that a name type absent
    from the permitted subtrees is unconstrained. Correct in the abstract, and it rejected
    every certificate the box issued to the phone -- an excluded-subtree violation on every
    gated site. Devices on the REGULAR proxy port were fine, because there the destination
    arrives as a hostname via CONNECT, so the laptop passed every test while the phone
    failed completely and the constraint looked innocent.

    Kept as a standing test because the exclusion is the obvious thing to re-add: it reads
    like tightening, and nothing else here would notice it had broken every device that
    reaches the box transparently.
    """
    assert _verify(ca, "www.reddit.com", extra_san=ip_san), (
        f"the CA cannot vouch for a leaf carrying {ip_san} -- transparent-mode clients "
        "will reject every gated site (see F38)")


@pytest.mark.parametrize("host", OUT_OF_SCOPE)
def test_an_ip_san_does_not_smuggle_an_out_of_scope_host(ca, host):
    """Permitting IPs must not weaken the DNS bound, which is the property that matters.

    Attaching an IP SAN is not a way to get a certificate for a name the CA may not vouch
    for: the DNS name is still checked against the permitted subtrees on its own.
    """
    assert not _verify(ca, host, extra_san="IP:142.250.190.46"), (
        f"the CA signed {host} when an IP SAN was attached -- the DNS constraint is "
        "being bypassed")


def test_the_constraints_and_the_decrypt_allowlist_come_from_one_list(ca):
    """CLAUDE.md tracks three places that must agree about which hosts are gated. The CA
    is a fourth, and the failure mode is asymmetric: a domain in --allow-hosts but not in
    the constraints means the proxy intercepts a site it cannot produce a valid cert for,
    so that site breaks with a certificate error and no other signal.

    Asserted against the derivation rather than the file, so this cannot pass by both
    being wrong in the same way."""
    import gen_allow_hosts

    text = (ca / "mitmproxy-ca-cert.pem").read_text()
    dump = subprocess.run(["openssl", "x509", "-noout", "-text"],
                          input=text, capture_output=True, text=True, timeout=60).stdout
    permitted, seen_permitted = set(), False
    for line in dump.splitlines():
        line = line.strip()
        if line == "Permitted:":
            seen_permitted = True
        elif line == "Excluded:":
            seen_permitted = False
        elif seen_permitted and line.startswith("DNS:"):
            permitted.add(line[4:])
    assert permitted, "no permitted DNS subtrees parsed out of the CA — " \
                      "an empty set would make every assertion below vacuous"
    assert permitted == set(gen_allow_hosts.domains()), (
        f"only in CA: {sorted(permitted - set(gen_allow_hosts.domains()))}; "
        f"only in allowlist: {sorted(set(gen_allow_hosts.domains()) - permitted)}")


def test_mitmproxy_uses_the_ca_we_generated(ca):
    """mitmproxy generates its own unconstrained CA when confdir has none. The generator
    writes the layout it expects, so it adopts ours instead of silently making a new one
    — which would undo all of the above without changing any output anyone reads."""
    from mitmproxy import certs
    import pathlib

    store = certs.CertStore.from_store(pathlib.Path(str(ca)), "mitmproxy", 2048)
    entry = store.get_cert("www.reddit.com", ["www.reddit.com"])
    issued = entry.cert.to_pem().decode()
    (ca / "mitm_leaf.pem").write_text(issued)
    out = subprocess.run(["openssl", "verify", "-CAfile",
                          str(ca / "mitmproxy-ca-cert.pem"), str(ca / "mitm_leaf.pem")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, (
        "a cert mitmproxy issued does not chain to our constrained CA — it is probably "
        f"using one of its own: {out.stdout}{out.stderr}")


def test_rotation_verifies_the_new_ca_before_restarting_the_proxy():
    """Order-of-operations, asserted against the source because the failure is invisible
    in the result.

    mitmproxy generates a fresh UNCONSTRAINED CA whenever confdir is empty. So a rotation
    that restarts the proxy first and checks afterwards finds a perfectly good
    certificate file, reports success, and has silently thrown away the constraint. This
    was written after doing exactly that: the check has to come before the restart.
    """
    src = open(os.path.join(ROOT, "rotate-ca.sh")).read()
    check = src.find('grep -q "Name Constraints"')
    restart = src.find('systemctl restart "$SERVICE"\nNEWFP')
    assert check != -1, "rotate-ca.sh no longer verifies the new CA is constrained"
    assert restart != -1, "could not locate the post-rotation restart — update this test"
    assert check < restart, (
        "rotate-ca.sh restarts the proxy before confirming the new CA is constrained; "
        "mitmproxy will fill an empty confdir with an unconstrained CA and the check "
        "will then pass against it")


def test_rotation_repins_the_fingerprint_the_audit_compares_against():
    """A deliberate rotation must not look like the theft the pin exists to detect.

    cooldown-audit.sh notes "CA FINGERPRINT CHANGED" on any mismatch and only writes the
    baseline when the file is empty, so without this the hourly audit reports a finding
    forever after every rotation. A detector that cries wolf after every legitimate
    change is one you learn to ignore, which is the same as not having it.

    Asserted against the source, and against the audit's own path, so the two cannot
    drift to different files and both look correct in isolation.
    """
    rot = open(os.path.join(ROOT, "rotate-ca.sh")).read()
    audit = open(os.path.join(ROOT, "deploy", "cooldown-audit.sh")).read()

    pin_paths = re.findall(r"^PIN=(\S+)", audit, re.M)
    assert pin_paths, "could not find PIN= in cooldown-audit.sh — update this test"
    assert pin_paths[0] in rot, (
        f"rotate-ca.sh does not touch the audit baseline {pin_paths[0]}; a rotation will "
        f"trip 'CA FINGERPRINT CHANGED' every hour until it is cleared by hand")

    repin = rot.find(pin_paths[0])
    newfp = rot.find("NEWFP=")
    assert newfp < repin, "re-pin must come after the new fingerprint is known"


def test_the_generator_refuses_to_ship_an_unconstrained_ca(tmp_path):
    """gen_ca.sh verifies its own output before installing it. Prove that check can fail:
    point it at a derivation that permits everything and it must refuse rather than write
    a CA that vouches for the internet."""
    fake = tmp_path / "deploy"
    fake.mkdir()
    shutil.copy(os.path.join(ROOT, "deploy", "gen_ca.sh"), fake / "gen_ca.sh")
    # A derivation that emits no constraints at all — the shape a broken generator has.
    (fake / "gen_allow_hosts.py").write_text("print('')\n")
    r = subprocess.run(["bash", str(fake / "gen_ca.sh"), str(tmp_path / "out")],
                       capture_output=True, text=True, timeout=180)
    assert r.returncode != 0, "gen_ca.sh built a CA from an empty constraint list"
    assert not (tmp_path / "out" / "mitmproxy-ca.pem").exists(), \
        "gen_ca.sh wrote a CA it had already decided was unsafe"
