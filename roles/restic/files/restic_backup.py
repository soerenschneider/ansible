#!/bin/env python3

import argparse
import io
import json
import logging
import os
import re
import sys
import shutil
import subprocess

from datetime import datetime
from pathlib import Path
from typing import List, Optional

# time to wait for the backup process to finish until cancelling it
BACKUP_TIMEOUT_SECONDS = 7200

# prefix for all the metrics we're writing
METRIC_PREFIX = "restic_backup"

# skeleton of the backup cmd we're invoking
RESTIC_BACKUP_CMD = ["restic", "-q", "--json", "backup", "--one-file-system", "-r"]

# the json fields output of the restic 'backup' cmd as keys with a nice suffix and a help text as tuple values
RESTIC_METRICS = {
    "files_new": ("_total", "New files created with this snapshot"),
    "files_changed": ("_total", "Files changed with this snapshot"),
    "files_unmodified": ("_total", "Amount of unmodified files with this snapshot"),
    "dirs_new": ("_total", "Newly created directories with this snapshot"),
    "dirs_changed": ("_total", "Changed directories with this snapshot"),
    "dirs_unmodified": ("_total", "Unmodified directories with this snapshot"),
    "data_blobs": ("_total", "Data blobs of the snapshot"),
    "tree_blobs": ("_total", "Tree blobs of the snapshot"),
    "data_added": ("_bytes_total", "Total bytes added during this snapshot"),
    "total_files_processed": ("", "Files processed with this snapshot"),
    "total_bytes_processed": ("", "Bytes processed with this snapshot"),
    "total_duration": ("_seconds", "Total duration of this snapshot"),
}

# additional metrics of this wrapper
INTERNAL_METRICS = {
    "start_time": ("_seconds", "Start time of the backup process"),
    "success": ("_bool", "Boolean indicating the success of the backup"),
    "exporter_errors": ("_bool", "Exporter errors unrelated to restic"),
}


class ResticError(Exception):
    pass


def run_backup(repo: str, dirs: List[str]) -> Optional[str]:
    """ Performs the backup operation. Returns the JSONified stdout of the restic backup call. """
    if isinstance(dirs, str):
        dirs = [dirs]

    command = RESTIC_BACKUP_CMD + [repo] + dirs
    logging.info("Starting backup using command: %s", command)
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        stdout, stderr = proc.communicate()
        proc.wait(BACKUP_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            logging.error("Backup was not successful: %s", stderr)
            raise ResticError(stderr)

        logging.info("Backup was successful!")
        return stdout.splitlines()[-1]


def write_metrics(metrics_data: io.StringIO, target_dir: Path, backup_id: str) -> None:
    """ Writes the metrics file to the target directory. """
    target_file = f"restic_backup_{backup_id}.prom"
    tmp_file = f"{target_file}.{os.getpid()}"
    try:
        # we're kind of defeating the purpose of the stream here
        with open(tmp_file, mode="w", encoding="utf-8") as fd:
            print(metrics_data.getvalue(), file=fd)
        logging.info("Moving temporary metric file '%s' to '%s'", tmp_file, target_dir / target_file)
        shutil.move(tmp_file, target_dir / target_file)
    finally:
        metrics_data.close()


def format_data(output: dict, identifier: str) -> io.StringIO:
    """ Poor man's Open Metrics formatting of the JSON output. """
    buffer = io.StringIO()
    for metric in RESTIC_METRICS:
        if metric not in output:
            logging.error("Excepted metric to be around but wasn't: %s", metric)
            output["exporter_errors"] = 1
        else:
            value = output[metric]
            buffer.write(f"# HELP {METRIC_PREFIX}_{metric}{RESTIC_METRICS[metric][0]} {RESTIC_METRICS[metric][1]}\n")
            buffer.write(f"# TYPE {METRIC_PREFIX}_{metric}{RESTIC_METRICS[metric][0]} gauge\n")
            buffer.write(f'{METRIC_PREFIX}_{metric}{RESTIC_METRICS[metric][0]}{{repo="{identifier}"}} {value}\n')

    for metric in INTERNAL_METRICS:
        value = output[metric]
        buffer.write(f"# HELP {METRIC_PREFIX}_{metric}{INTERNAL_METRICS[metric][0]} {INTERNAL_METRICS[metric][1]}\n")
        buffer.write(f"# TYPE {METRIC_PREFIX}_{metric}{INTERNAL_METRICS[metric][0]} gauge\n")
        buffer.write(f'{METRIC_PREFIX}_{metric}{INTERNAL_METRICS[metric][0]}{{repo="{identifier}"}} {value}\n')

    return buffer


def validate_args(args: argparse.Namespace) -> None:
    """ Validates the parsed arguments. As we're relying heavily on env vars, we can't use
        argparse functionality directly for this. """
    # check repo parameter
    if not args.repo:
        raise ValueError("No repository defined")
    args.repo = os.path.expanduser(args.repo)

    # check targets
    if not args.targets:
        raise ValueError("No targets to backup defined")

    args.targets = [os.path.expanduser(repo) for repo in args.targets.split(",")]
    for target in args.targets:
        if not Path(target).exists():
            raise ValueError(f"One of the targets does not exist: {target}")

    # check backup id
    if not args.backup_id:
        raise ValueError("No backup_id given")
    args.backup_id = re.sub(r"[^\w\s]", "", args.backup_id)

    # check metric dir
    if not Path(args.metric_dir).exists():
        raise ValueError(f"Dir to write metrics to does not exist: '{args.metric_dir}' ")


def parse_args() -> argparse.Namespace:
    """ Parses the arguments and returns the parsed namespace. """
    parser = argparse.ArgumentParser()
    parser.add_argument("-r", "--repo", default=os.environ.get("RESTIC_REPOSITORY"), help="The restic repository")
    parser.add_argument("-t", "--targets", default=os.environ.get("RESTIC_TARGETS"), help="The targets to include in the snapshot")
    parser.add_argument("-i", "--id", dest="backup_id", default=os.environ.get("RESTIC_BACKUP_ID"), help="An identifier for this backup")
    parser.add_argument("-m", "--metric-dir", default="/var/lib/node_exporter", help="Dir to write metrics to")
    return parser.parse_args()


def main() -> None:
    """ Main does mainly main things. """
    start_time = datetime.utcnow().timestamp()
    args = parse_args()
    try:
        validate_args(args)
    except ValueError as err:
        logging.error("Can not start the backup: %s", err.args[0])

    # initialize dict signaling failure
    json_output = {"success": 0}
    try:
        stdout = run_backup(args.repo, args.targets)
        json_output = json.loads(stdout)
        json_output["success"] = 1
    except ResticError as err:
        logging.error("Failed to run backup: %s", err)

    json_output["exporter_errors"] = 0
    json_output["start_time"] = start_time

    metrics_data = format_data(json_output, args.backup_id)
    target_dir = Path(args.metric_dir)
    write_metrics(metrics_data, target_dir, args.backup_id)

    if json_output["success"] != 1:
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(format="%(levelname)s:%(message)s", level=logging.INFO)
    main()
