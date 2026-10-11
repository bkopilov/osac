import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_OUTPUT_KEYS = ("l2_vni", "l3_vni", "fabric_reserved_range")


def _create_subnet_tasks() -> list[dict]:
    path = (
        Path(__file__).resolve().parents[2]
        / "collections"
        / "ansible_collections"
        / "osac"
        / "templates"
        / "roles"
        / "netris"
        / "tasks"
        / "create_subnet.yaml"
    )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_vnet_read_by_id_uses_fact_from_create_or_find_step() -> None:
    tasks = _create_subnet_tasks()
    task_names = [task.get("name") for task in tasks]
    create_index = task_names.index("Create V-Net in Netris controller")
    read_index = task_names.index("Read V-Net fabric outputs")

    assert create_index < read_index
    create_task = tasks[create_index]
    assert create_task["ansible.builtin.include_role"]["name"] == "netris.controller.vnet"
    assert create_task["ansible.builtin.include_role"]["tasks_from"] == "create"

    read_task = tasks[read_index]
    assert read_task["ansible.builtin.include_role"]["name"] == "netris.controller.vnet"
    assert read_task["ansible.builtin.include_role"]["tasks_from"] == "read_by_id"
    assert "vnet_id" not in read_task.get("vars", {})


def _fabric_output_tasks() -> list[dict]:
    names = {
        "Set the Subnet CIDR as the fabric reserved range",
        "Validate Netris fabric outputs",
        "Publish fabric outputs for the k8s manager",
    }
    return [task for task in _create_subnet_tasks() if task.get("name") in names]


def _run_fabric_output_validation(
    tmp_path: Path,
    *,
    vnet: object,
    vpc: object,
    subnet_ipv4_cidr: str = "192.0.2.0/24",
) -> subprocess.CompletedProcess[str]:
    ansible_playbook = shutil.which("ansible-playbook")
    assert ansible_playbook is not None, "ansible-playbook must be installed in the AAP dev environment"

    playbook = tmp_path / "validate_fabric_outputs.yaml"
    ansible_home = tmp_path / "ansible-home"
    ansible_local_temp = tmp_path / "ansible-local"
    ansible_home.mkdir()
    ansible_local_temp.mkdir()
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "netris_vnet_detail": vnet,
                        "_parent_vpc": vpc,
                        "subnet_name": "test-subnet",
                        "subnet_ipv4_cidr": subnet_ipv4_cidr,
                    },
                    "tasks": [
                        *_fabric_output_tasks()[:2],
                        {"name": "Confirm outputs passed validation", "ansible.builtin.set_fact": {"validated": True}},
                    ],
                }
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [ansible_playbook, "-v", "-i", "localhost,", "-c", "local", str(playbook)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "ANSIBLE_HOME": str(ansible_home), "ANSIBLE_LOCAL_TEMP": str(ansible_local_temp)},
    )


def test_fabric_outputs_are_validated_before_configmap_publication() -> None:
    tasks = _create_subnet_tasks()
    names = [task.get("name") for task in tasks]
    validation_index = names.index("Validate Netris fabric outputs")
    publication_index = names.index("Publish fabric outputs for the k8s manager")
    assert validation_index < publication_index

    configmap = tasks[publication_index]["kubernetes.core.k8s"]["definition"]["data"]
    assert tuple(configmap) == _OUTPUT_KEYS
    assert configmap["l2_vni"] == "{{ netris_vnet_detail.vxlanID | string | trim }}"
    assert configmap["l3_vni"] == "{{ subnet_l3_vni | string }}"
    assert configmap["fabric_reserved_range"] == "{{ _fabric_reserved_range }}"


@pytest.mark.parametrize(
    ("vnet", "vpc", "subnet_ipv4_cidr"),
    [
        (None, {"vni": 20007}, "192.0.2.0/24"),
        ({"vxlanID": None}, {"vni": 20007}, "192.0.2.0/24"),
        ({"vxlanID": ""}, {"vni": 20007}, "192.0.2.0/24"),
        ({"vxlanID": 0}, {"vni": 20007}, "192.0.2.0/24"),
        ({"vxlanID": "16777216"}, {"vni": 20007}, "192.0.2.0/24"),
        ({"vxlanID": 10007}, None, "192.0.2.0/24"),
        ({"vxlanID": 10007}, {"vni": None}, "192.0.2.0/24"),
        ({"vxlanID": 10007}, {"vni": "16777216"}, "192.0.2.0/24"),
        ({"vxlanID": 10007}, {"vni": 20007}, " "),
    ],
)
def test_invalid_fabric_output_fails_before_publication(
    tmp_path: Path, vnet: object, vpc: object, subnet_ipv4_cidr: str
) -> None:
    result = _run_fabric_output_validation(
        tmp_path, vnet=vnet, vpc=vpc, subnet_ipv4_cidr=subnet_ipv4_cidr
    )

    assert result.returncode != 0
    assert "Refusing to publish incomplete fabric outputs" in result.stdout
    assert "Confirm outputs passed validation" not in result.stdout


def test_valid_fabric_outputs_pass_validation(tmp_path: Path) -> None:
    result = _run_fabric_output_validation(
        tmp_path,
        vnet={"vxlanID": " 10007 "},
        vpc={"vni": 20007},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Confirm outputs passed validation" in result.stdout
