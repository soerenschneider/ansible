#!/usr/bin/env python3

import argparse
import subprocess
import sys

from pathlib import Path
from typing import Dict, List, Any, Tuple

import yaml


class FileIo:
    def __init__(self, f: Path):
        if not f or not f.exists():
            raise Exception(f"The specified path {f} does not exist")
        self._f = f

    def read_yaml(self) -> Dict[Any, Any]:
        with open(self._f, 'r') as fc:
            return yaml.safe_load(fc)

    def write_yaml(self, content: Dict[Any, Any]) -> None:
        with open(self._f, 'w') as fc:
            yaml.dump(content, fc, default_flow_style=False)


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


def gen_keypair() -> Tuple[str, str]:
    proc = subprocess.run(["wg", "genkey"], check=True, capture_output=True)
    private_key = proc.stdout.decode('utf-8').strip()

    proc = subprocess.run(["wg", "pubkey"], input=proc.stdout, check=True, capture_output=True)
    pub_key = proc.stdout.decode('utf-8').strip()

    return private_key, pub_key


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Rotate a wireguard private key')
    parser.add_argument('-i', '--inventory', default="/home/soeren/src/gitlab/playbooks/inventory/prod")
    parser.add_argument('--file-priv', dest="file_private_key", default="wireguard.sops.yaml")
    parser.add_argument('--file-pub', dest="file_pub_keys", default="wireguard-pubkeys.yaml")
    parser.add_argument('-wh', '--hosts', nargs="+", dest='hosts', required=True, action='store')
    parser.add_argument('-wn', '--net-name', default="mesh", dest='net_name', action='store')
    parser.add_argument('--private-key-path', dest='path_priv_key', action='store', default="wg_private_keys.$wireguard_wg_net_name.$host_name")
    parser.add_argument('--pub-key-path', dest='path_pub_key', action='store', default="wg_pub_keys.$wireguard_wg_net_name.$host_name")

    return parser.parse_args()


def get_xpath(path: str, net_name: str, host_name: str) -> List[str]:
    chain = path.split(".")
    for index in range(len(chain)):
        elem = chain[index]
        if elem == "$wireguard_wg_net_name":
            chain[index] = net_name
        elif elem == "$host_name":
            chain[index] = host_name

    return chain


def replace_private_key(con: Dict, xpath: List, priv_key: str) -> Dict:
    con[xpath[0]][xpath[1]][xpath[2]] = priv_key


def replace_pub_key(con: Dict, xpath: List, pub_key: str) -> Dict:
    con[xpath[0]][xpath[1]][xpath[2]] = pub_key


if __name__ == "__main__":
    args = parse_args()
    for host in args.hosts:
        print(f"Rotating private key for host '{host}'...")
        priv_key_file = Path(args.inventory) / "host_vars" / host / args.file_private_key
        pub_key_file = Path(args.inventory) / "group_vars" / f"wireguard-{args.net_name}" / args.file_pub_keys

        priv_key_io = SopsIo(priv_key_file)
        priv_key_dict = priv_key_io.read_yaml()

        pub_key_io = FileIo(pub_key_file)
        pub_keys = pub_key_io.read_yaml()
        
        new_priv_key, new_pub_key = gen_keypair()
        print(f"New public key is '{new_pub_key}'")

        jpath_priv_key = get_xpath(args.path_priv_key, args.net_name, host)
        jpath_pub_key = get_xpath(args.path_pub_key, args.net_name, host)
        
        replace_private_key(priv_key_dict, jpath_priv_key, new_priv_key)
        replace_pub_key(pub_keys, jpath_pub_key, new_pub_key)
        priv_key_io.write_yaml(priv_key_dict)
        pub_key_io.write_yaml(pub_keys)
        print(f"Finished rotating keys for host '{host}'")

