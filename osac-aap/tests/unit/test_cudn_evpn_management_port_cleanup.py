import os
import shlex
import subprocess
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined


def _role_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "collections"
        / "ansible_collections"
        / "osac"
        / "templates"
        / "roles"
        / "cudn_evpn"
    )


def _cleanup_tasks() -> list[dict]:
    path = _role_path() / "tasks" / "cleanup_management_ports.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _exec_task() -> dict:
    return next(
        task for task in _cleanup_tasks()
        if task.get("name") == "Remove the deleted CUDN management port and Linux interface from each OVN node"
    )


def _render_command(saved_ports: str = "ovn-k8s-mp9") -> list[str]:
    command_template = Environment(undefined=StrictUndefined).from_string(
        _exec_task()["kubernetes.core.k8s_exec"]["command"]
    )
    command = command_template.render(
        _cudn_evpn_ovn_network_name="cluster_udn_vnet-7",
        _cudn_evpn_management_ports_by_node={"node-a": saved_ports.split(",") if saved_ports else []},
        item={"spec": {"nodeName": "node-a"}},
    )
    return shlex.split(command)


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def _run_cleanup(path: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PATH"] = f"{path}:{environment['PATH']}"
    return subprocess.run(_render_command(), capture_output=True, text=True, env=environment, check=False)


def test_cleanup_exec_is_scoped_to_owned_management_ports_and_retains_vrf() -> None:
    tasks = _cleanup_tasks()
    pod_lookup = next(
        task for task in tasks if task.get("name") == "Find OVN node Pods for stale CUDN management-port cleanup"
    )
    assert pod_lookup["kubernetes.core.k8s_info"]["namespace"] == (
        "{{ cudn_evpn_ovn_namespace | default('openshift-ovn-kubernetes') }}"
    )
    assert pod_lookup["kubernetes.core.k8s_info"]["label_selectors"] == [
        "{{ cudn_evpn_ovn_pod_selector | default('app=ovnkube-node') }}"
    ]

    task_names = [task.get("name") for task in tasks]
    discover_index = task_names.index("Discover network-owned internal management ports before mutating OVS")
    persist_index = task_names.index("Persist CUDN management-port identities before deleting OVS or Linux interfaces")
    cleanup_index = task_names.index("Remove the deleted CUDN management port and Linux interface from each OVN node")
    clear_index = task_names.index(
        "Clear durable CUDN management-port cleanup state after verifying all node interfaces are gone"
    )
    assert discover_index < persist_index < cleanup_index < clear_index
    annotation_key = "osac.openshift.io/cudn-management-ports"
    persist_annotations = tasks[persist_index]["kubernetes.core.k8s"]["definition"]["metadata"]["annotations"]
    clear_annotations = tasks[clear_index]["kubernetes.core.k8s"]["definition"]["metadata"]["annotations"]
    assert annotation_key in persist_annotations
    assert annotation_key in clear_annotations

    exec_task = _exec_task()
    module = exec_task["kubernetes.core.k8s_exec"]
    assert module["container"] == "{{ cudn_evpn_ovn_container | default('ovnkube-controller') }}"
    assert module["namespace"] == "{{ cudn_evpn_ovn_namespace | default('openshift-ovn-kubernetes') }}"
    assert "failed_when" in exec_task
    assert "removed:" in exec_task["changed_when"]

    command = module["command"]
    assert "type=internal" in command
    assert "external_ids:k8s.ovn.org/network=$network" in command
    assert 'find Interface "name=$port"' in command
    assert "ovn-k8s-mp[0-9]*" in command
    assert "port-to-br" in command
    assert "!= br-int" in command
    assert "del-port br-int" in command
    assert "ip link delete dev" in command
    assert "Unrecorded interface appeared during cleanup" in command
    assert "VRF" not in command
    assert "vrf" not in command


def test_cleanup_removes_owned_port_and_link_and_is_idempotent(tmp_path: Path) -> None:
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    state_path = tmp_path / "state"
    state_path.mkdir()
    (state_path / "port").write_text("ovn-k8s-mp9\n", encoding="utf-8")
    (state_path / "link").write_text("present\n", encoding="utf-8")
    (state_path / "bridge").write_text("br-int\n", encoding="utf-8")

    _write_executable(
        bin_path / "ovs-vsctl",
        """#!/bin/sh
set -eu
state="$CUDN_TEST_STATE"
while [ "$#" -gt 0 ] && [ "$1" = --timeout=15 ]; do shift; done
case "$1" in
  --no-headings)
    case "$*" in
      *external_ids:k8s.ovn.org/network=cluster_udn_vnet-7*|*name=ovn-k8s-mp9*)
        if [ -f "$state/port" ]; then cat "$state/port"; fi
        ;;
      *) exit 39 ;;
    esac
    ;;
  port-to-br)
    cat "$state/bridge"
    ;;
  --if-exists)
    printf '%s\\n' ovs-del >> "$state/calls"
    rm -f "$state/port"
    ;;
  *) exit 40 ;;
esac
""",
    )
    _write_executable(
        bin_path / "ip",
        """#!/bin/sh
set -eu
state="$CUDN_TEST_STATE"
if [ "$1" = link ] && [ "$2" = show ]; then
  test -f "$state/link"
elif [ "$1" = link ] && [ "$2" = delete ]; then
  if [ -f "$state/fail-ip-delete" ]; then exit 77; fi
  printf '%s\\n' ip-del >> "$state/calls"
  rm -f "$state/link"
else
  exit 41
fi
""",
    )

    environment = os.environ.copy()
    environment["PATH"] = f"{bin_path}:{environment['PATH']}"
    environment["CUDN_TEST_STATE"] = str(state_path)
    command = _render_command()

    first = subprocess.run(command, capture_output=True, text=True, env=environment, check=False)
    second = subprocess.run(command, capture_output=True, text=True, env=environment, check=False)

    assert first.returncode == 0, first.stderr
    assert "removed:ovn-k8s-mp9" in first.stdout
    assert not (state_path / "port").exists()
    assert not (state_path / "link").exists()
    assert (state_path / "calls").read_text(encoding="utf-8").splitlines() == ["ovs-del", "ip-del"]
    assert second.returncode == 0, second.stderr
    assert "already-absent" in second.stdout


def test_cleanup_retry_finishes_linux_link_deletion_after_ovs_port_was_removed(tmp_path: Path) -> None:
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    state_path = tmp_path / "state"
    state_path.mkdir()
    (state_path / "port").write_text("ovn-k8s-mp9\n", encoding="utf-8")
    (state_path / "link").write_text("present\n", encoding="utf-8")
    (state_path / "bridge").write_text("br-int\n", encoding="utf-8")
    (state_path / "fail-ip-delete").write_text("fail once\n", encoding="utf-8")
    _write_executable(
        bin_path / "ovs-vsctl",
        """#!/bin/sh
set -eu
state="$CUDN_TEST_STATE"
while [ "$#" -gt 0 ] && [ "$1" = --timeout=15 ]; do shift; done
case "$1" in
  --no-headings)
    case "$*" in
      *external_ids:k8s.ovn.org/network=cluster_udn_vnet-7*|*name=ovn-k8s-mp9*)
        if [ -f "$state/port" ]; then cat "$state/port"; fi
        ;;
      *) exit 39 ;;
    esac
    ;;
  port-to-br) cat "$state/bridge" ;;
  --if-exists) rm -f "$state/port" ;;
  *) exit 40 ;;
esac
""",
    )
    _write_executable(
        bin_path / "ip",
        """#!/bin/sh
set -eu
state="$CUDN_TEST_STATE"
if [ "$1" = link ] && [ "$2" = show ]; then
  test -f "$state/link"
elif [ "$1" = link ] && [ "$2" = delete ]; then
  if [ -f "$state/fail-ip-delete" ]; then exit 77; fi
  rm -f "$state/link"
else
  exit 41
fi
""",
    )
    environment = os.environ.copy()
    environment["PATH"] = f"{bin_path}:{environment['PATH']}"
    environment["CUDN_TEST_STATE"] = str(state_path)
    command = _render_command()

    interrupted = subprocess.run(command, capture_output=True, text=True, env=environment, check=False)
    assert interrupted.returncode == 77
    assert not (state_path / "port").exists()
    assert (state_path / "link").exists()

    (state_path / "fail-ip-delete").unlink()
    retried = subprocess.run(command, capture_output=True, text=True, env=environment, check=False)
    assert retried.returncode == 0, retried.stderr
    assert "removed:ovn-k8s-mp9" in retried.stdout
    assert not (state_path / "link").exists()


def test_cleanup_fails_closed_if_owned_interface_is_on_the_wrong_bridge(tmp_path: Path) -> None:
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    state_path = tmp_path / "state"
    state_path.mkdir()
    (state_path / "port").write_text("ovn-k8s-mp9\n", encoding="utf-8")
    (state_path / "link").write_text("present\n", encoding="utf-8")
    (state_path / "bridge").write_text("br-ex\n", encoding="utf-8")
    _write_executable(
        bin_path / "ovs-vsctl",
        """#!/bin/sh
set -eu
state="$CUDN_TEST_STATE"
while [ "$#" -gt 0 ] && [ "$1" = --timeout=15 ]; do shift; done
case "$1" in
  --no-headings)
    case "$*" in
      *external_ids:k8s.ovn.org/network=cluster_udn_vnet-7*|*name=ovn-k8s-mp9*) cat "$state/port" ;;
      *) exit 39 ;;
    esac
    ;;
  port-to-br) cat "$state/bridge" ;;
  --if-exists) exit 42 ;;
  *) exit 43 ;;
esac
""",
    )
    _write_executable(bin_path / "ip", "#!/bin/sh\nexit 44\n")
    environment = os.environ.copy()
    environment["PATH"] = f"{bin_path}:{environment['PATH']}"
    environment["CUDN_TEST_STATE"] = str(state_path)

    result = subprocess.run(_render_command(), capture_output=True, text=True, env=environment, check=False)

    assert result.returncode == 21
    assert "Refusing interface ovn-k8s-mp9 on bridge br-ex" in result.stderr
    assert (state_path / "port").exists()
    assert (state_path / "link").exists()
