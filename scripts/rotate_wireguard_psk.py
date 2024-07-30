#!/usr/bin/env python3

import argparse
import subprocess
import sys

from pathlib import Path
from typing import Dict, Any, List

import yaml


class SopsIo:
    def __init__(self, f: Path):
        if not f or not f.exists():
            raise Exception(f"The specified path {f} does not exist")

        self._f = f

    def read_yaml(self) -> Dict[Any, Any]:
        proc = subprocess.run(["sops", "-d", self._f], check=True, capture_output=True)
        return yaml.safe_load(proc.stdout)

    def write_yaml(self, content: Dict[Any, Any]) -> None:
        encoded = yaml.dump(content).encode('utf-8')
        subprocess.run(["sops", "--input-type", "yaml", "--output", str(self._f), "-e", "/dev/stdin"], input=encoded, check=True, capture_output=True, cwd=self._f.parent)

def list_hosts(psks: Dict[str, str], xpath: str) -> None:
    for coupling in psks[xpath[0]][xpath[1]]:
        split = coupling.split("_")
        print(f"{split[0]} <--> {split[1]}")

def rotate_hosts(psks: Dict[str, str], xpath: str, hosts: List[str]) -> None:
    if hosts:
        ordered = _get_ordered_hosts(hosts)
        if ordered not in psks[xpath[0]][xpath[1]]:
            print(f"Hosts {hosts} are not present in psk map, can not rotate them")
            sys.exit(1)
        psk_new = gen_psk()
        psks[xpath[0]][xpath[1]][ordered] = psk_new
        return

    for coupling in psks[xpath[0]][xpath[1]]:
        psk_new = gen_psk()
        psks[xpath[0]][xpath[1]][coupling] = psk_new


def add_hosts(psks: Dict[str, str], xpath: str, hosts: List[str]) -> None:
    ordered = _get_ordered_hosts(hosts)
    if ordered in psks[xpath[0]][xpath[1]]:
        print(f"Hosts {hosts} are already present in psk map")
        sys.exit(1)

    psk = gen_psk()
    psks[xpath[0]][xpath[1]][ordered] = psk

def _get_ordered_hosts(hosts: List[str]) -> str:
    return f"{hosts[0]}_{hosts[1]}" if hosts[0] < hosts[1] else f"{hosts[1]}_{hosts[0]}"

def del_hosts(psks: Dict[str, str], xpath: str, hosts: List[str]) -> None:
    ordered = _get_ordered_hosts(hosts)
    if ordered not in psks[xpath[0]][xpath[1]]:
        print(f"Hosts {hosts} not in psks")
        sys.exit(1)

    del psks[xpath[0]][xpath[1]][ordered]

def gen_psk() -> str:
    proc = subprocess.run(["wg", "genpsk"], check=True, capture_output=True)
    return proc.stdout.decode('utf-8').strip()

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Rotate a wireguard private key')
    parser.add_argument('-i', '--inventory', default="/home/soeren/src/gitlab/playbooks/inventory/prod")
    parser.add_argument('-f', '-file', dest="file_psks", default="wireguard-psks.sops.yaml")
    parser.add_argument('-wn', '--net-name', dest='net_name', default="mesh", action='store')
    parser.add_argument('--pub-key-path', dest='path_psks', action='store', default="wg_psks.$wireguard_wg_net_name")

    subparsers = parser.add_subparsers(dest="cmd")
    add_hosts = subparsers.add_parser('add')
    add_hosts.add_argument(dest='hosts', nargs="+", action='store')

    rotate = subparsers.add_parser('rotate')
    rotate.add_argument(dest='hosts', nargs="*", action='store')
    
    ls = subparsers.add_parser('list')

    del_hosts = subparsers.add_parser('del')
    del_hosts.add_argument(dest='hosts', nargs="+", action='store')

    return parser.parse_args()


def get_jpath(path: str, net_name: str) -> List[str]:
    chain = path.split(".")
    for index in range(len(chain)):
        elem = chain[index]
        if elem == "$wireguard_wg_net_name":
            chain[index] = net_name

    return chain


if __name__ == "__main__":
    args = parse_args()
    if args.cmd == "rotate" and len(args.hosts) not in [0, 2]:
        print(f"You supplied {len(args.hosts)} hosts, but you must supply either 0 or exactly 2 hosts to rotate")
        sys.exit(1)

    if args.cmd not in ["list", "rotate"] and len(args.hosts) != 2:
        print(f"You supplied {len(args.hosts)} hosts, but you must supply an even number of hosts")
        sys.exit(1)

    psks_file = Path(args.inventory) / "group_vars" / f"wireguard-{args.net_name}" / args.file_psks
    file_io = SopsIo(psks_file)
    psks = file_io.read_yaml()
    jpath = get_jpath(args.path_psks, args.net_name)

    match args.cmd:
        case "list":
            list_hosts(psks, jpath)
        case "rotate":
            rotate_hosts(psks, jpath, args.hosts)
        case "add":
            add_hosts(psks, jpath, args.hosts)
        case "del":
            del_hosts(psks, jpath, args.hosts)
            
    file_io.write_yaml(psks)
