#!/usr/bin/env python3
"""What the lumina role decides about the certificate already on the host, in every state it can be in.

Run it from the ``ansible`` directory:

    python tests/certificate_states.py

Why this exists rather than a comment in the role. The rule is that certbot is skipped only when the
certificate it was asked for is already there, and that everything else at that path gets replaced: a
self-signed certificate, a staging one once staging is off, an expired one, an unreadable one. That is
five states and two decisions per state, expressed as Jinja over a fact set, and the failure mode is
silent. The old rule, "skip when a file exists at that path", passed every review and shipped, and the
only thing that would have caught it is this: real certificates on disk, the role's own tasks, and an
expectation per state.

The tasks and the certbot command line are read out of the role file rather than restated here, so an
edit to either is reflected rather than shadowed. If a task is renamed this fails loudly instead of
testing nothing.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROLE = HERE.parent / "roles" / "lumina" / "tasks" / "main.yml"
NAME = "lumina.test.example"

# The tasks that decide what the certificate is, in the order the role runs them.
DECIDING = [
    "Look for a Let's Encrypt certificate",
    "Look for its private key",
    "Look for the certbot lineage behind it",
    "Check that the lineage's links all resolve",
    "Read the certificate that is already there",
    "Work out what that certificate is",
    "Decide whether that certificate is the one certbot was asked for",
    "Serve the certbot certificate if it is usable, the self-signed one otherwise",
]


def role_tasks():
    tasks = yaml.safe_load(ROLE.read_text())
    by_name = {t["name"]: t for t in tasks if isinstance(t, dict) and "name" in t}
    missing = [n for n in DECIDING if n not in by_name] + [
        n for n in ("Try for a real certificate",) if n not in by_name
    ]
    if missing:
        sys.exit("these tasks are not in %s any more, so this test is stale rather than passing: %s"
                 % (ROLE, missing))
    return by_name


def make_fixtures(root):
    """One certificate per state, in the shapes the host can really hold."""
    def run(*args):
        subprocess.run(args, check=True, capture_output=True)

    ss = root / "selfsigned.pem"
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
        "-keyout", str(root / "ss.key"), "-out", str(ss), "-subj", "/CN=" + NAME,
        "-addext", "subjectAltName=DNS:" + NAME)

    # A stand-in CA, so "issued by somebody else" is a real signature rather than a relabelled name.
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
        "-keyout", str(root / "ca.key"), "-out", str(root / "ca.pem"),
        "-subj", "/C=US/O=Pretend Authority/CN=R11")
    run("openssl", "req", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(root / "leaf.key"), "-out", str(root / "leaf.csr"), "-subj", "/CN=" + NAME)

    def sign(ca, key, out, *extra):
        run("openssl", "x509", "-req", "-in", str(root / "leaf.csr"), "-CA", str(ca),
            "-CAkey", str(key), "-out", str(out), *extra)

    real = root / "real.pem"
    sign(root / "ca.pem", root / "ca.key", real, "-days", "90")
    # fullchain.pem is what certbot writes: the leaf followed by its issuer.
    fullchain = root / "ca-issued.pem"
    fullchain.write_bytes(real.read_bytes() + (root / "ca.pem").read_bytes())

    # Let's Encrypt names every staging CA "(STAGING) ...", which is how a trial certificate is
    # recognisable from the file alone.
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
        "-keyout", str(root / "stca.key"), "-out", str(root / "stca.pem"),
        "-subj", "/C=US/O=(STAGING) Let's Encrypt/CN=(STAGING) Artificial Apricot R3")
    sign(root / "stca.pem", root / "stca.key", root / "staging.pem", "-days", "90")

    # Through `openssl ca` rather than `openssl x509 -req -not_before/-not_after`, which is what this
    # wants and cannot use: those two options arrived in OpenSSL 3.4, and the CI runner image ships
    # 3.0.13. The first version of this passed on a workstation with 3.5 and could only ever have
    # failed in CI. `openssl ca -startdate/-enddate` has been there for many years.
    ca_dir = root / "ca"
    (ca_dir / "newcerts").mkdir(parents=True)
    (ca_dir / "index.txt").write_text("")
    (ca_dir / "serial").write_text("01\n")
    (root / "ca.cnf").write_text(
        "[ca]\ndefault_ca = CA_default\n"
        "[CA_default]\n"
        "dir = %s\ndatabase = $dir/index.txt\nnew_certs_dir = $dir/newcerts\nserial = $dir/serial\n"
        "default_md = sha256\npolicy = policy_any\nemail_in_dn = no\nrand_serial = no\n"
        "unique_subject = no\ncopy_extensions = none\n"
        "[policy_any]\ncommonName = supplied\ncountryName = optional\n"
        "organizationName = optional\n" % ca_dir)
    run("openssl", "ca", "-batch", "-config", str(root / "ca.cnf"),
        "-cert", str(root / "ca.pem"), "-keyfile", str(root / "ca.key"),
        "-startdate", "20200101000000Z", "-enddate", "20200401000000Z",
        "-in", str(root / "leaf.csr"), "-out", str(root / "expired.pem"), "-notext")

    (root / "unreadable.pem").write_text("this is not a certificate\n")
    return ss


# label; the fixture to put at the certbot path (None for nothing, DANGLE for symlinks into a pruned
# archive); whether a renewal config exists, which is what makes it a certbot *lineage* and the only
# state certbot can renew in place; whether the private key is there; whether staging was asked for;
# then what the role must conclude, whether certbot must run, and whether the request must carry
# --force-renewal.
#
# The lineage column is the one that was missing. --force-renewal keyed on a file existing at the
# leaf path asked certbot to renew things it had no lineage for, which issues rather than renews, and
# on a lineage it cannot load certbot writes to <name>-0001 and still exits 0.
CASES = [
    # label, fixture at the leaf, renewal config present, private key present, staging asked for,
    # expected facts, certbot must run, request must carry --force-renewal
    ("nothing there", None, False, False, False,
     dict(usable=False, real=False, loadable=False, obstructed=False, serving="selfsigned"),
     True, False),
    # The requirement, in the shape somebody actually gets into: a certificate placed at certbot's
    # path by hand, with no lineage behind it. certbot cannot write there, so it has to be cleared
    # first, and forcing a renewal of a lineage that does not exist renews nothing.
    ("self-signed, no lineage", "selfsigned.pem", False, True, False,
     dict(usable=True, selfsigned=True, real=False, loadable=False, obstructed=True,
          serving="certbot"),
     True, False),
    ("CA-issued lineage", "ca-issued.pem", True, True, False,
     dict(usable=True, selfsigned=False, real=True, loadable=True, obstructed=False,
          serving="certbot"),
     False, None),
    # A real certificate with no key. nginx will not load that pair, and checking only the
    # certificate both pointed the vhost at it and told certbot there was nothing to do.
    ("CA-issued, key missing", "ca-issued.pem", True, False, False,
     dict(usable=False, selfsigned=False, real=False, loadable=False, obstructed=True,
          serving="selfsigned"),
     True, False),
    ("staging lineage, staging off", "staging.pem", True, True, False,
     dict(usable=True, staging=True, real=False, loadable=True, obstructed=False,
          serving="certbot"),
     True, True),
    ("staging lineage, staging on", "staging.pem", True, True, True,
     dict(usable=True, staging=True, real=True, loadable=True, obstructed=False,
          serving="certbot"),
     False, None),
    ("expired lineage", "expired.pem", True, True, False,
     dict(usable=False, real=False, loadable=True, obstructed=False, serving="selfsigned"),
     True, True),
    ("unreadable lineage", "unreadable.pem", True, True, False,
     dict(usable=False, real=False, loadable=True, obstructed=False, serving="selfsigned"),
     True, True),
    # The renewal config survives but the archive is gone. certbot cannot load this, so it takes the
    # new-request branch, collides with the surviving config, and writes <name>-0001 while exiting 0.
    # Clearing it first is the only thing that makes the request land where this role reads.
    ("dangling lineage", "DANGLE", True, True, False,
     dict(usable=False, real=False, loadable=False, obstructed=True, serving="selfsigned"),
     True, False),
    # Files gone, config left behind. Same trap, reached by deleting live/ by hand.
    ("lineage config, no files", None, True, False, False,
     dict(usable=False, real=False, loadable=False, obstructed=True, serving="selfsigned"),
     True, False),
]


def ansible(play, tmp):
    path = tmp / "play.yml"
    path.write_text(yaml.safe_dump(play, sort_keys=False))
    r = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(path)],
                       capture_output=True, text=True, cwd=str(HERE.parent))
    if r.returncode != 0:
        sys.exit("ansible-playbook failed:\n%s\n%s" % (r.stdout[-3000:], r.stderr[-1500:]))


def main():
    if not shutil.which("openssl") or not shutil.which("ansible-playbook"):
        sys.exit("needs openssl and ansible-playbook on PATH")

    by_name = role_tasks()
    deciding = [by_name[n] for n in DECIDING]
    outer = by_name["Try for a real certificate"]
    ask = next(t for t in outer["block"] if t["name"] == "Ask Let's Encrypt")
    # A `creates:` here would be a second guard keyed on the path merely existing, which is the rule
    # this test exists to keep out: it would suppress the request for the certificate it is meant to
    # replace, and it would do so silently, because the task reports "ok" either way.
    creates = ask["ansible.builtin.command"].get("creates")
    command = " ".join(ask["ansible.builtin.command"]["cmd"].split())
    # The failure back-off clause is about elapsed time, not about the certificate.
    guard = [c for c in outer["when"] if "marker" not in str(c)]

    problems = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "tasks.yml").write_text(yaml.safe_dump(deciding, sort_keys=False))
        selfsigned = make_fixtures(tmp)

        for (label, fixture, has_conf, has_key, staging,
             expected, want_runs, want_force) in CASES:
            # Laid out exactly as certbot does it, because the shape is the thing under test:
            # renewal/<name>.conf is the lineage, and live/<name>/*.pem are symlinks into
            # ../../archive/<name>/.
            root = tmp / "le"
            shutil.rmtree(root, ignore_errors=True)
            live = root / "live" / NAME
            live.mkdir(parents=True)
            (root / "renewal").mkdir(parents=True)
            archive = root / "archive" / NAME
            archive.mkdir(parents=True)

            def link(kind, source=None, dangle=False):
                target = live / ("%s.pem" % kind)
                if dangle:
                    os.symlink("../../archive/%s/%s7.pem" % (NAME, kind), target)
                    return
                (archive / ("%s1.pem" % kind)).write_bytes(source)
                os.symlink("../../archive/%s/%s1.pem" % (NAME, kind), target)

            leaf = (tmp / fixture).read_bytes() if fixture and fixture != "DANGLE" else None
            key = (tmp / "leaf.key").read_bytes()
            if fixture == "DANGLE":
                for kind in ("cert", "privkey", "chain", "fullchain"):
                    link(kind, dangle=True)
            elif fixture:
                for kind in ("cert", "fullchain"):
                    link(kind, leaf)
                link("chain", leaf)
                if has_key:
                    link("privkey", key)
            elif has_key:
                link("privkey", key)

            if has_conf:
                (root / "renewal" / ("%s.conf" % NAME)).write_text(
                    "version = 3.1.0\narchive_dir = %s\n" % archive)

            le_cert = live / "fullchain.pem"
            variables = {
                "lumina_tls_enabled": True, "lumina_tls_selfsigned": True, "lumina_certbot": True,
                "lumina_certbot_possible": True, "lumina_certbot_staging": staging,
                "lumina_certbot_email": "ops@example.org", "lumina_certbot_timeout": 120,
                "lumina_acme_webroot": "/var/lib/lumina/acme",
                "lumina_tls_cert_name": NAME,
                "lumina_letsencrypt_dir": str(root),
                "lumina_tls_le_dir": str(live),
                "lumina_tls_le_renewal": str(root / "renewal" / ("%s.conf" % NAME)),
                "lumina_tls_le_cert": str(le_cert),
                "lumina_tls_le_key": str(live / "privkey.pem"),
                "lumina_tls_selfsigned_cert": str(selfsigned),
                "lumina_tls_selfsigned_key": str(tmp / "ss.key"),
                # Through a variable so Ansible templates the role's own {{ ... }} inside it. Inlined
                # into the expression below it stays literal, and every conditional flag reads as
                # absent while the test still passes.
                "lumina_certbot_command": command,
            }
            out = tmp / "decided.json"
            ansible([{
                "hosts": "localhost", "gather_facts": False, "vars": variables,
                "tasks": [
                    {"include_tasks": str(tmp / "tasks.yml")},
                    {"copy": {"dest": str(out), "content":
                        "{{ {'usable': lumina_tls_le_usable,"
                        " 'selfsigned': lumina_tls_le_selfsigned,"
                        " 'staging': lumina_tls_le_staging,"
                        " 'real': lumina_tls_le_real,"
                        " 'loadable': lumina_tls_le_loadable,"
                        " 'obstructed': lumina_tls_le_obstructed,"
                        " 'serving': lumina_tls_cert,"
                        " 'runs': (" + ") and (".join("(%s)" % g for g in guard) + "),"
                        " 'command': lumina_certbot_command} | to_json }}"}},
                ],
            }], tmp)
            got = json.loads(out.read_text())
            got["serving"] = {str(le_cert): "certbot", str(selfsigned): "selfsigned"}.get(
                got["serving"], got["serving"])
            runs = bool(got["runs"])
            flags = {f for f in got["command"].split() if f.startswith("--")}

            wrong = ["%s is %r, should be %r" % (k, got.get(k), v)
                     for k, v in expected.items() if got.get(k) != v]
            if runs != want_runs:
                wrong.append("certbot runs=%s, should be %s" % (runs, want_runs))
            if runs and ("--force-renewal" in flags) != want_force:
                wrong.append("--force-renewal present=%s, should be %s"
                             % ("--force-renewal" in flags, want_force))
            if runs and "--cert-name" not in flags:
                wrong.append("--cert-name missing: certbot faced with a damaged lineage would write "
                             "to a new one beside it and this role would never see the result")
            print("%-22s %s" % (label, "ok" if not wrong else "WRONG: " + "; ".join(wrong)))
            if wrong:
                problems.append("%s: %s" % (label, "; ".join(wrong)))

    if creates is not None:
        problems.append(
            "`creates: %s` is back on the certbot command. Any file at that path then suppresses the "
            "request, including the self-signed certificate the request exists to replace." % creates)
        print("WRONG: " + problems[-1])

    print()
    if problems:
        print("FAILED\n" + "\n".join("  " + p for p in problems))
        return 1
    print("every certificate state decided as specified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
