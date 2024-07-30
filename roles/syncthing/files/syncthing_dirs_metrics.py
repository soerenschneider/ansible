#!/usr/bin/env python3

import io
import os
import re
import sys
import shutil
import xml.etree.ElementTree as ET

from datetime import datetime
from pathlib import Path
from typing import Tuple, List

regex_deletion = r"^.+~\d{8}-\d{6}$"
regex_conflict = r"^.+\.sync-conflict-\d{8}-\d{6}-.+$"

def check_dir(dir_path: Path) -> Tuple[int]:
    num_deleted_files = 0
    bytes_deleted_files = 0
    conflicting_files = 0
    bytes_conflicting_files = 0
    checked_files = 0

    deleted_files = dir_path / ".stversions"
    if deleted_files.exists() and deleted_files.is_dir():
        for f in deleted_files.rglob("*"):
            if re.match(regex_deletion, f.name):
                num_deleted_files += 1
                bytes_deleted_files += f.stat().st_size

    for f in dir_path.rglob("*"):
        checked_files += 1
        if re.match(regex_conflict, f.name):
            conflicting_files += 1
            bytes_conflicting_files += f.stat().st_size


    return num_deleted_files, bytes_deleted_files, conflicting_files, bytes_conflicting_files, checked_files


def print_metric_header(buffer: io.StringIO) -> None:
    buffer.write(f'# HELP syncthing_files_checked_total Total checked files\n')
    buffer.write(f'# TYPE syncthing_files_checked_total gauge\n')
    
    buffer.write(f'# HELP syncthing_dirs_conflicts_total Total sync files\n')
    buffer.write(f'# TYPE syncthing_dirs_conflicts_total gauge\n')

    buffer.write(f'# HELP syncthing_dirs_conflicts_files_size_bytes Size of the conflicting files by another syncthing device\n')
    buffer.write(f'# TYPE syncthing_dirs_conflicts_files_size_bytes gauge\n')

    buffer.write(f'# HELP syncthing_dirs_deleted_files_total Total files deleted by another syncthing device\n')
    buffer.write(f'# TYPE syncthing_dirs_deleted_files_total gauge\n')

    buffer.write(f'# HELP syncthing_dirs_deleted_files_size_bytes Size of the deleted files by another syncthing device\n')
    buffer.write(f'# TYPE syncthing_dirs_deleted_files_size_bytes gauge\n')

    buffer.write(f'# HELP syncthing_dirs_paths_timestamp_seconds Timestamp of the run\n')
    buffer.write(f'# TYPE syncthing_dirs_paths_timestamp_seconds gauge\n')


def print_metric(buffer: io.StringIO, path: Path, dir_info: Tuple[int]) -> None:
    buffer.write(f'syncthing_dirs_deleted_files_total{{dir="{path}"}} {dir_info[0]}\n')
    buffer.write(f'syncthing_dirs_deleted_files_size_bytes{{dir="{path}"}} {dir_info[1]}\n')
    buffer.write(f'syncthing_dirs_conflicts_total{{dir="{path}"}} {dir_info[2]}\n')
    buffer.write(f'syncthing_dirs_conflicts_files_size_bytes{{dir="{path}"}} {dir_info[3]}\n')
    buffer.write(f'syncthing_dirs_files_checked_total{{dir="{path}"}} {dir_info[4]}\n')
    buffer.write(f'syncthing_dirs_paths_timestamp_seconds{{dir="{path}"}} {datetime.now().timestamp()}\n')


def iterate_dirs(syncthing_dirs: List[Path]) -> io.StringIO:
    buffer = io.StringIO()
    print_metric_header(buffer)
    for syncthing_dir in syncthing_dirs:
        dir_info = check_dir(syncthing_dir)
        print_metric(buffer, syncthing_dir, dir_info)

    return buffer


def get_paths_from_config(config_file: str) -> List[Path]:
    tree = ET.parse(config_file)
    root = tree.getroot()
    return [Path(node.attrib["path"]) for node in root.findall("./folder")]


def write_metrics(metrics_data: io.StringIO, target_dir: Path, config: str) -> None:
    target_file = f"syncthing_dirs_paths_{config}.prom"
    tmp_file = f"{target_file}.{os.getpid()}"
    try:
        with open(tmp_file, mode="w", encoding="utf-8") as fd:
            print(metrics_data.getvalue(), file=fd)
        shutil.move(tmp_file, target_dir / target_file)
    finally:
        metrics_data.close()


def get_config() -> Tuple[str]:
    config_file = os.environ.get("SYNCTHING_DIRS_METRICS_CONFIG_FILE", "")
    if not config_file or not Path(config_file).exists():
        raise ValueError(f"Config file '{config_file}' does not exist")

    metrics_dir = os.environ.get("SYNCTHING_DIRS_METRICS_METRICS_DIR", "/var/lib/node_exporter")
    if not metrics_dir or not Path(metrics_dir).exists():
        raise ValueError(f"Metrics dir '{metrics_dir}' does not exist")

    identifier = os.environ.get("SYNCTHING_DIRS_METRICS_IDENTIFIER", "default")
    if not identifier:
        raise ValueError("Empty identifier given")

    return config_file, metrics_dir, identifier


def main() -> None:
    try:
        config_file, metrics_dir, identifier = get_config()
        paths = get_paths_from_config(config_file)
        buffer = iterate_dirs(paths)
        write_metrics(buffer, Path(metrics_dir), identifier)
    except ET.ParseError:
        print(f"Config file {config_file} is not a valid xml file")
        sys.exit(1)
    except ValueError as err:
        print(err.args[0])
        sys.exit(1)


if __name__ == '__main__':
    main()

