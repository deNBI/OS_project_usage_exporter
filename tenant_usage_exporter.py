#!/usr/bin/env python3
"""
Query tenant usage from an openstack instance and provide it in a prometheus compatible 
format.

Read-only access to the openstack-APIs `list_projects` and `/os-simple-tenant-usages` is 
needed.
"""

from argparse import (
    ArgumentParser,
    ArgumentDefaultsHelpFormatter,
    FileType,
    ArgumentTypeError,
)
import logging
from typing import Optional, TextIO, Tuple, Dict, List, NamedTuple
from json import load
from time import sleep
from urllib import parse
from datetime import datetime
from os import getenv

# enable logging for now
format = "%(asctime)s - %(levelname)s [%(name)s] %(threadName)s %(message)s"
logging.basicConfig(level=logging.INFO, format=format)

import openstack  # type: ignore
import prometheus_client  # type: ignore
import keystoneauth1  # type: ignore
import maya

project_labels = ["project_id", "project_name"]
project_metrics = {
    "total_vcpus_usage": prometheus_client.Gauge(
        "tenant_vcpu_usage", "Total vcpu usage", labelnames=project_labels
    ),
    "total_memory_mb_usage": prometheus_client.Gauge(
        "tenant_mb_usage", "Total MB usage", labelnames=project_labels
    ),
}

__author__ = "tluettje"
__license__ = "MIT"

# Environment variables for usage inside docker
start_date_env_var = "USAGE_EXPORTER_START_DATE"
use_dummy_data_env_var = "USAGE_EXPORTER_DUMMY_MODE"
update_interval_env_var = "USAGE_EXPORTER_UPDATE_INTERVAL"

# Location of dummy data relative to this file
dummy_data = ("./resources/dummy_projects.json", "./resources/dummy_tenant_usage.json")


class _Exporter:
    def __init__(
        self,
        dummy_projects: Optional[TextIO] = None,
        dummy_simple_tenant_usages: Optional[TextIO] = None,
        stats_start: datetime = datetime.today(),
    ) -> None:
        self.projects = {}  # type: Dict[str, str]
        self.stats_start = stats_start
        if all((dummy_simple_tenant_usages, dummy_projects)):
            self.dummy = True
            self.cloud = None
            self.dummy_projects = load(dummy_projects)  # type: ignore
            self.simple_tenant_usages = load(dummy_simple_tenant_usages)  # type: ignore
        else:
            self.dummy = False
            self.dummy_projects = None
            self.simple_tenant_usages = None
            try:
                self.cloud = openstack.connect()
            except keystoneauth1.exceptions.auth_plugins.MissingRequiredOptions:
                logging.error("Could not authenticate against OpenStack, Aborting!")
                logging.info("Consider using the dummy mode for testing")
                raise ValueError
        self.update()

    def update(self) -> None:
        self.projects = self.collect_projects()
        self.usages = self.collect_usages(
            start=self.stats_start.strftime("%Y-%m-%dT%H:%M:%S")
        )
        self.set_metrics()

    def set_metrics(self) -> None:
        for project, usage_values in self.usages.items():
            for usage_name, gauge in project_metrics.items():
                gauge.labels(
                    project_id=project, project_name=self.projects[project]
                ).set(usage_values[usage_name])

    def collect_usages(self, **query_args) -> Dict[str, Dict[str, float]]:
        """
        :param query_args: Additional parameters for the `os-simple-tenant-usage`-url
        """
        if self.dummy:
            return {
                project["tenant_id"]: {
                    metric: project[metric] for metric in project_metrics
                }
                for project in self.simple_tenant_usages[  # type: ignore
                    "tenant_usages"
                ]
            }
        else:
            query_params = "&".join(
                "=".join((key, value)) for key, value in query_args.items()
            )
            try:
                json_payload = self.cloud.compute.get(  # type: ignore
                    "/os-simple-tenant-usage?" + query_params
                ).json()
                tenant_usage = json_payload["tenant_usages"]  # type: ignore
            except KeyError:
                logging.error("Received following invalid json payload:", json_payload)

            return {
                project["tenant_id"]: {
                    metric: project[metric] for metric in project_metrics
                }
                for project in tenant_usage
            }

    def collect_projects(self) -> Dict[str, str]:
        if self.dummy:
            return {project["id"]: project["name"] for project in self.dummy_projects}
        else:
            return {
                project["id"]: project["name"]
                for project in self.cloud.list_projects()  # type: ignore
            }


def valid_date(s):
    try:
        return maya.when(s).datetime()
    except ValueError:
        msg = f"Unrecognized date: '{s}'."
        raise ArgumentTypeError(msg)


def main():
    parser = ArgumentParser(
        epilog=f"{__license__} @ {__author__}",
        formatter_class=ArgumentDefaultsHelpFormatter,
        description=__doc__,
    )
    parser.add_argument(
        "-d",
        "--data",
        nargs=2,
        type=FileType(),
        metavar=("projects", "tenant_usages"),
        default=dummy_data,
        help="""Use dummy values instead of connecting to an openstack instance.""",
    )
    parser.add_argument(
        "-s",
        "--start",
        type=valid_date,
        default=getenv(start_date_env_var, datetime.today()),
        help=f"""Beginning time of stats (YYYY-MM-DD). If set the value of
        {start_date_env_var} is used. Uses maya for parsing.""",
    )
    args = parser.parse_args()
    if getenv(use_dummy_data_env_var) or False:
        # if the default dummy data have been used we need to open them, argparse
        # hasn't done this for us since the default value has not been a string
        if type(args.data[0]) is str:
            args.data = map(open, args.data)
        exporter = _Exporter(*args.data)
    else:
        exporter = _Exporter(stats_start=args.start)
    prometheus_client.start_http_server(8080)
    try:
        update_interval = int(getenv(update_interval_env_var, ""))
    except ValueError:
        update_interval = 300
    while True:
        try:
            sleep(update_interval)
            exporter.update()
        except KeyboardInterrupt:
            logging.info("Received Ctrl-c, exiting.")
            return 0
        except Exception as e:
            logging.exception(
                f"Received unexpected exception {e}. Traceback following."
            )
            return 1


if __name__ == "__main__":

    exit(main())
