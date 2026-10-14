"""Exercise the IPAM helper through real Ansible and a local HTTP fixture."""
import json
import os
from pathlib import Path
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "collections/ansible_collections/osac/templates/roles/netris/tasks"


@pytest.mark.parametrize("scenario", ["new", "reuse", "wrong-parent", "wrong-vpc", "missing-parent"])
def test_reserved_prefix_creation_is_idempotent_and_checks_identity(tmp_path: Path, scenario: str) -> None:
    parent = {"id": 5, "name": "test-subnet", "prefix": "192.0.2.0/24", "parentID": 0,
              "vpc": {"id": 3}, "tenant": {"id": 1}}
    child = {"id": 6, "name": "test-subnet-fabric", "prefix": "192.0.2.0/26", "parentID": 5,
             "vpc": {"id": 3}, "tenant": {"id": 1}}
    pools = [parent]
    if scenario in ["reuse", "wrong-parent"]:
        pools.append(child)
    if scenario == "wrong-parent":
        child["parentID"] = 99
    if scenario == "wrong-vpc":
        parent["vpc"] = {"id": 99}
    if scenario == "missing-parent":
        pools.clear()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, body):
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            calls.append(("GET", self.path, None))
            assert self.path == "/api/v2/ipam/subnets?filterByVpc=3"
            self.respond({"data": pools})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(("POST", self.path, body))
            assert self.path == "/api/v2/ipam/subnet"
            pools.append({**body, "id": 6})
            self.respond({"data": {"id": 6}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        playbook = tmp_path / "fabric_ipam.yaml"
        playbook.write_text(yaml.safe_dump([{
            "hosts": "localhost", "gather_facts": False,
            "vars": {"netris_controller_url": f"http://127.0.0.1:{server.server_port}",
                     "netris_session_cookie": "fixture", "netris_site_id": 1, "netris_tenant_id": 1,
                     "_parent_vpc": {"id": 3}, "ipam_subnet_id": 5, "subnet_name": "test-subnet",
                     "subnet_ipv4_cidr": "192.0.2.0/24", "_fabric_reserved_range": "192.0.2.0/26"},
            "tasks": [{"name": "Ensure reserved prefix on first attempt",
                       "ansible.builtin.include_tasks": str(TASKS / "ensure_fabric_ipam.yaml")},
                      {"name": "Repeat the same provisioning request",
                       "ansible.builtin.include_tasks": str(TASKS / "ensure_fabric_ipam.yaml")}],
        }], sort_keys=False))
        result = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(playbook)],
                                cwd=ROOT, env={**os.environ, "ANSIBLE_LOCAL_TEMP": str(tmp_path / "local")},
                                capture_output=True, text=True, timeout=45)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    posts = [c for c in calls if c[0] == "POST"]
    if scenario in ["new", "reuse"]:
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(posts) == (1 if scenario == "new" else 0)
        assert len(pools) == 2
        if posts:
            assert posts[0][2] == {"name": "test-subnet-fabric", "prefix": "192.0.2.0/26", "parentID": 5,
                                   "purpose": "common", "sites": [{"id": 1}], "tenant": {"id": 1},
                                   "vpc": {"id": 3}}
    else:
        assert result.returncode != 0
        assert not posts
        assert "IPAM" in result.stdout


def test_gateway_creation_waits_for_reserved_ipam_and_cleanup_removes_child_first() -> None:
    tasks = yaml.safe_load((TASKS / "create_subnet.yaml").read_text())
    names = [t["name"] for t in tasks]
    helper = tasks[names.index("Ensure the reserved EVPN fabric prefix exists in Netris IPAM")]
    assert helper["when"] == "is_cudn_evpn"
    assert names.index(helper["name"]) < names.index("Create V-Net in Netris controller")
    tasks = yaml.safe_load((TASKS / "delete_subnet.yaml").read_text())
    names = [t["name"] for t in tasks]
    assert names.index("Delete V-Net from Netris controller") < names.index("Delete the reserved EVPN fabric IPAM subnet")
    assert names.index("Delete the reserved EVPN fabric IPAM subnet") < names.index("Delete IPAM subnet from Netris controller")
