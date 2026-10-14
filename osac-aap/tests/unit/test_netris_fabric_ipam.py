"""Exercise the IPAM helper through real Ansible and a local HTTP fixture."""
import json
import os
from pathlib import Path
import socket
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "collections/ansible_collections/osac/templates/roles/netris/tasks"


@pytest.mark.parametrize("scenario", ["new", "reuse", "lost-response", "wrong-parent", "wrong-vpc", "missing-parent"])
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
    drop_create_response = scenario == "lost-response"

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
            nonlocal drop_create_response
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(("POST", self.path, body))
            assert self.path == "/api/v2/ipam/subnet"
            pools.append({**body, "id": 6})
            if drop_create_response:
                drop_create_response = False
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            self.respond({"data": {"id": 6}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def run_helper(attempt: int, repeats: int = 1) -> subprocess.CompletedProcess:
            playbook = tmp_path / f"fabric_ipam_{attempt}.yaml"
            helper_tasks = [
                {
                    "name": f"Ensure reserved prefix on attempt {attempt}.{repeat}",
                    "ansible.builtin.include_tasks": str(TASKS / "ensure_fabric_ipam.yaml"),
                }
                for repeat in range(repeats)
            ]
            playbook.write_text(yaml.safe_dump([{
                "hosts": "localhost", "gather_facts": False,
                "vars": {"netris_controller_url": f"http://127.0.0.1:{server.server_port}",
                         "netris_session_cookie": "fixture", "netris_site_id": 1, "netris_tenant_id": 1,
                         "_parent_vpc": {"id": 3}, "ipam_subnet_id": 5, "subnet_name": "test-subnet",
                         "subnet_ipv4_cidr": "192.0.2.0/24", "_fabric_reserved_range": "192.0.2.0/26"},
                "tasks": helper_tasks,
            }], sort_keys=False))
            return subprocess.run(
                ["ansible-playbook", "-i", "localhost,", "-c", "local", str(playbook)],
                cwd=ROOT,
                env={**os.environ, "ANSIBLE_LOCAL_TEMP": str(tmp_path / f"local-{attempt}")},
                capture_output=True,
                text=True,
                timeout=45,
            )

        if scenario == "lost-response":
            first_attempt = run_helper(1)
            assert first_attempt.returncode != 0, first_attempt.stdout + first_attempt.stderr
            assert len([call for call in calls if call[0] == "POST"]) == 1
            assert len(pools) == 2

            retry_attempt = run_helper(2)
            assert retry_attempt.returncode == 0, retry_attempt.stdout + retry_attempt.stderr
            assert "changed=0" in retry_attempt.stdout

            repeated_attempt = run_helper(3)
            assert repeated_attempt.returncode == 0, repeated_attempt.stdout + repeated_attempt.stderr
            assert "changed=0" in repeated_attempt.stdout
            result = repeated_attempt
        else:
            result = run_helper(1, repeats=2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    posts = [c for c in calls if c[0] == "POST"]
    if scenario in ["new", "reuse", "lost-response"]:
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(posts) == (1 if scenario in ["new", "lost-response"] else 0)
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
