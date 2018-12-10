#!/usr/bin/env python3
"""
Query project usage from an openstack instance and provide it in a prometheus compatible
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
from typing import (
    Optional,
    TextIO,
    Tuple,
    Dict,
    List,
    NamedTuple,
    Union,
    cast,
    Iterable,
)
from json import load
from time import sleep
from urllib import parse
from datetime import datetime, timedelta
from os import getenv
from dataclasses import dataclass
from hashlib import sha256 as sha256func
from enum import Enum

import openstack  # type: ignore
import prometheus_client  # type: ignore
import keystoneauth1  # type: ignore
import maya
import toml

# enable logging for now
format = "%(asctime)s - %(levelname)s [%(name)s] %(threadName)s %(message)s"
logging.basicConfig(level=logging.INFO, format=format)


project_labels = ["project_id", "project_name", "domain_name", "domain_id"]
project_metrics = {
    "total_vcpus_usage": prometheus_client.Gauge(
        "project_vcpu_usage", "Total vcpu usage", labelnames=project_labels
    ),
    "total_memory_mb_usage": prometheus_client.Gauge(
        "project_mb_usage", "Total MB usage", labelnames=project_labels
    ),
}

__author__ = "tluettje"
__license__ = "MIT"

# Environment variables for usage inside docker
start_date_env_var = "USAGE_EXPORTER_START_DATE"
update_interval_env_var = "USAGE_EXPORTER_UPDATE_INTERVAL"

# name of the domain whose projects to monitor
project_domain_env_var = "USAGE_EXPORTER_PROJECT_DOMAINS"
default_project_domains = ["elixir"]

dummy_file_env_var = "USAGE_EXPORTER_DUMMY_FILE"

default_dummy_file = "resources/dummy_machines.toml"

UsageTuple = NamedTuple("UsageTuple", [("vcpu_hours", float), ("mb_hours", float)])

hour_timedelta = timedelta(hours=1)

script_start = datetime.now()


def sha256(content: str) -> str:
    s = sha256func()
    s.update(content.encode())
    return s.hexdigest()


class _ExporterBase:
    def update(self):
        ...


class OpenstackExporter(_ExporterBase):
    def __init__(self, stats_start: datetime = datetime.today()) -> None:
        self.projects = {}  # type: Dict[str, str]
        self.stats_start = stats_start
        self.simple_project_usages = None
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
        query_params = "&".join(
            "=".join((key, value)) for key, value in query_args.items()
        )
        try:
            json_payload = self.cloud.compute.get(  # type: ignore
                "/os-simple-tenant-usage?" + query_params
            ).json()
            project_usage = json_payload["tenant_usages"]  # type: ignore
        except KeyError:
            logging.error("Received following invalid json payload: %s", json_payload)

        return {
            project["project_id"]: {
                metric: project[metric] for metric in project_metrics
            }
            for project in project_usage
        }

    def collect_projects(self) -> Dict[str, str]:
        return {
            project["id"]: project["name"]
            for project in self.cloud.list_projects()  # type: ignore
        }


class UptimeInformation(Enum):
    NO_UPTIME = 0
    SINCE_SCRIPT_START = 1
    SINCE_DATETIME = 2
    BETWEEN_DATETIMES = 3


@dataclass
class DummyMachine:
    """
    Representing a dummy machine causing usage to monitor.
    :param name: Currently not used outside but might be in future, therefore leave it
    :param cpus: Number of cpus the dummy machine is using.
    :param ram: Amount of RAM [mb] the machine is using.
    :param uptime: Determines whether the machine is *up* and its usage so far. In case
    of True the machine is considered booted up the instant this script is started. In
    case of False it hasn't been booted ever (no actual use case).
    In case of a single datetime the machine is considered *up* since that moment (for
    simplicity the timezone information are ignored). In case of a list of two datetimes
    the machine is considered *up* the time in between. The first one must be
    older/smaller than the second one and both but relative to the moment the script
    started both may lie in the future or past.
    """

    cpus: int = 4
    ram: int = 8192
    uptime: Union[bool, datetime, Tuple[datetime, datetime]] = True

    def __post_init__(self) -> None:
        if self.cpus <= 0 or self.ram <= 0:
            raise ValueError("`cpu` and `ram` must be positive")
        if isinstance(self.uptime, (list, tuple)):
            if self.uptime[0] > self.uptime[1]:  # type: ignore
                raise ValueError(
                    "First uptime-tuple datetime must be older than second one"
                )
            # remove any timezone information
            self.uptime_information = UptimeInformation.BETWEEN_DATETIMES
        elif isinstance(self.uptime, datetime):
            self.uptime_information = UptimeInformation.SINCE_DATETIME
        elif isinstance(self.uptime, bool):
            self.uptime_information = (
                UptimeInformation.SINCE_SCRIPT_START
                if self.uptime
                else UptimeInformation.NO_UPTIME
            )
        else:
            raise ValueError(
                f"Invalid type for param `uptime` (got {type(self.uptime)}"
            )

    def usage_value(self) -> UsageTuple:
        """
        Returns the total ram and cpu usage counted in hours of this machine, depending
        on its `uptime` configuration`
        """
        now = datetime.now()
        if self.uptime_information is UptimeInformation.NO_UPTIME:
            return UsageTuple(0, 0)
        elif self.uptime_information is UptimeInformation.SINCE_SCRIPT_START:
            hours_uptime = (datetime.now() - script_start) / hour_timedelta
            return UsageTuple(self.cpus * hours_uptime, self.ram * hours_uptime)
        elif self.uptime_information is UptimeInformation.SINCE_DATETIME:
            # to satisfy `mypy` type checker
            boot_datetime = cast(datetime, self.uptime)
            hours_uptime = (now - boot_datetime.replace(tzinfo=None)) / hour_timedelta
            # do not report negative usage in case the machine is not *booted yet*
            if hours_uptime > 0:
                return UsageTuple(self.cpus * hours_uptime, self.ram * hours_uptime)
            else:
                return UsageTuple(0, 0)
        else:
            # to satisfy `mypy` type checker
            runtime_tuple = cast(Tuple[datetime, datetime], self.uptime)
            boot_datetime = cast(datetime, runtime_tuple[0].replace(tzinfo=None))
            shutdown_datetime = cast(datetime, runtime_tuple[1].replace(tzinfo=None))
            if boot_datetime > now:
                # machine did not boot yet
                return UsageTuple(0, 0)
            elif shutdown_datetime < now:
                # machine did run already and is considered down
                hours_uptime = (shutdown_datetime - boot_datetime) / hour_timedelta
            else:
                # machine booted in the past but is still running
                hours_uptime = (now - boot_datetime) / hour_timedelta
            return UsageTuple(self.cpus * hours_uptime, self.ram * hours_uptime)


class DummyExporter(_ExporterBase):
    def __init__(self, dummy_values: TextIO, domains: Iterable[str] = None) -> None:
        self.dummy_values = toml.loads(dummy_values.read())
        self.domains = set(domains) if domains else None
        self.projects: List[DummyProject] = []
        for project_name, project_content in self.dummy_values.items():
            machines = [
                DummyMachine(**machine) for machine in project_content["machines"]
            ]
            self.projects.append(
                DummyProject(
                    name=project_name,
                    domain=project_content.get("domain", ""),
                    machines=machines,
                )
            )

    def update(self) -> None:
        for project in self.projects:
            if self.domains and project.domain not in self.domains:
                continue
            project_usages = [machine.usage_value() for machine in project.machines]
            vcpu_hours = sum(usage.vcpu_hours for usage in project_usages)
            mb_hours = sum(usage.mb_hours for usage in project_usages)
            project_metrics["total_vcpus_usage"].labels(
                project_id=project.id,
                project_name=project.name,
                domain_name=project.domain,
                domain_id=project.domain_id,
            ).set(vcpu_hours)
            project_metrics["total_memory_mb_usage"].labels(
                project_id=project.id,
                project_name=project.name,
                domain_name=project.domain,
                domain_id=project.domain_id,
            ).set(mb_hours)


@dataclass
class DummyProject:
    name: str
    machines: List[DummyMachine]
    domain: str = ""

    def __post_init__(self):
        self.id = sha256(self.name)[-16:]
        self.domain_id = sha256(self.domain)[-16:]


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
        "--dummy-data",
        type=FileType(),
        help=f"""Use dummy values instead of connecting to an openstack instance. Usage
        values are calculated base on the configured uptime, take a look at the example
        file for an explanation {default_dummy_file}. Can also be provided via
        environment variable {dummy_file_env_var}""",
    )
    parser.add_argument(
        "--domain",
        default=[
            domain
            for domain in getenv(
                project_domain_env_var, ",".join(default_project_domains)
            ).split(",")
            if domain
        ],
        type=str,
        nargs="*",
        help=f"""Only export usages of projects belonging to one of the given domains.
        Separate them via comma if passing via environment variable
        ({project_domain_env_var}). If no domains are specified all readable projects
        are exported.""",
    )
    parser.add_argument(
        "-s",
        "--start",
        type=valid_date,
        default=getenv(start_date_env_var, datetime.today()),
        help=f"""Beginning time of stats (YYYY-MM-DD). If set the value of
        {start_date_env_var} is used. Uses maya for parsing.""",
    )
    parser.add_argument(
        "-i",
        "--update-interval",
        type=int,
        default=int(getenv(update_interval_env_var, 300)),
        help=f"""Time to sleep between intervals, in case the calls cause to much load on
        your openstack instance. Defaults to the value of ${update_interval_env_var} or
        300 (in seconds)""",
    )
    args = parser.parse_args()

    if args.dummy_data:
        logging.info("Using dummy export with data from %s", args.dummy_data.name)
        exporter = DummyExporter(args.dummy_data, args.domain)
    elif getenv(dummy_file_env_var):
        logging.info("Using dummy export with data from %s", getenv(dummy_file_env_var))
        # if the default dummy data have been used we need to open them, argparse
        # hasn't done this for us since the default value has not been a string
        with open(getenv(dummy_file_env_var)) as file:
            exporter = DummyExporter(file)
    else:
        logging.info("Using regular openstack exporter")
        exporter = OpenstackExporter(stats_start=args.start)
    logging.info("Beginning to serve metrics on port 8080")
    prometheus_client.start_http_server(8080)
    while True:
        try:
            sleep(args.update_interval)
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
