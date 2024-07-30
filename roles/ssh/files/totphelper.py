#!/usr/bin/env python3

import base64
import binascii
import sys
import socket
import os
import getpass
import subprocess
import argparse

from pathlib import Path


class TotpHelper:
    def __init__(self, args: argparse.Namespace):
        self._args = args

    def cmd(self):
        seed_hex = self.get_seed()
        seed_b32 = self.convert(seed_hex)
        user = self.get_username()
        issuer = self.get_issuer()
        uri = self.get_uri(seed_b32, user, issuer)
        print(uri)
        if not self._args.no_qr:
            self.print_qr(uri)

    def get_seed(self) -> str:
        seed_hex = None
        seed_file = self._args.file

        try:
            with open(seed_file, "r") as seed_file:
                seed_hex = seed_file.read().replace('\n', '')
        except Exception as e:
            print(f"Could not open file {seed_file}: {e}")
            if self._args.yes:
                print("Can not continue")
                sys.exit(1)

        if not seed_hex:
            seed_hex = input("Please supply hex seed manually: ")

        return seed_hex


    def convert(self, seed_hex: str) -> str:
        binary_string = binascii.unhexlify(seed_hex)
        seed_b32 = base64.b32encode(binary_string).decode('utf-8')
        return seed_b32


    def get_username(self) -> str:
        username = getpass.getuser()

        print(f"Auto-guessed username is: '{username}'")
        if not self._args.yes and not _read_consent():
            username = _read_str("Please supply an username")

        return username

    def get_issuer(self) -> str:
        issuer = "SSH " + socket.getfqdn()
        print(f"Auto-guessed issuer is: '{issuer}'")
        if not self._args.yes and not _read_consent():
            issuer = _read_str("Please supply an issuer name")

        return issuer


    def get_uri(self, seed_b32: str, username: str, issuer: str) -> str:
        uri = f"otpauth://totp/{username}?secret={seed_b32}&issuer={issuer}"
        return uri


    def print_qr(self, data: str) -> None:
        try:
            qrencode = subprocess.run(["qrencode", "-t", "ANSIUTF8"], stdout=subprocess.PIPE, text=True, input=data)
            print(qrencode.stdout)
        except Exception as e:
            print(f"Can not print qrcode: {e}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="boing")
    parser.add_argument('-f', '--file', dest="file", default=os.path.join(Path.home(), ".totp-key"))
    parser.add_argument('-y', '--yes', dest="yes", help="Assume 'yes' to all questions", action="store_true")
    parser.add_argument('--no-qr', dest="no_qr", help="Do not print QR code", action="store_true")

    return parser.parse_args()


def _read_consent() -> bool:
    valid_answer = False
    answer = None
    while not valid_answer:
        answer = input("Is this ok for you? y/n ")
        valid_answer = answer.strip().upper() in ["Y", "YES", "N", "NO"]

    return answer.strip().upper() in ["Y", "YES"]


def _read_str(prompt: str) -> str:
    valid_answer = False
    answer = None
    while not valid_answer:
        answer = input(prompt+ ": ")
        answer = answer.replace(" ", "")
        valid_answer = len(answer) >= 3

    return answer

if __name__ == "__main__":
    args = parse_args()
    helper = TotpHelper(args)
    helper.cmd()
