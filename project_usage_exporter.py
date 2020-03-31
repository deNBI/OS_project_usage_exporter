#!/usr/bin/env python3
"""
Query project usages from an openstack instance and provide it in a prometheus compatible
format.
Alternatively develop in local mode and emulate machines and projects.
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
    Set,
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
import ast

# enable logging for now
format = "%(asctime)s - %(levelname)s [%(name)s] %(threadName)s %(message)s"
logging.basicConfig(level=logging.INFO, format=format)


project_labels = ["project_id", "project_name", "domain_name", "domain_id"]
project_metrics = {
    # the key is the name of the value inside the API response, therefore do not change
    # it
    "total_vcpus_usage": prometheus_client.Gauge(
        "project_vcpu_usage", "Total vcpu usage", labelnames=project_labels
    ),
    "total_memory_mb_usage": prometheus_client.Gauge(
        "project_mb_usage", "Total MB usage", labelnames=project_labels
    ),
}
HOURS_KEY = "hours"

__author__ = "tluettje"
__license__ = "GNU AGPLv3"

# Environment variables for usage inside docker
start_date_env_var = "USAGE_EXPORTER_START_DATE"
update_interval_env_var = "USAGE_EXPORTER_UPDATE_INTERVAL"
simple_vm_project_id = "USAGE_EXPORTER_SIMPLE_VM_PROJECT_ID"
vcpu_weights = "USAGE_EXPORTER_PROJECT_MB_WEIGHTS"
project_mb_weights = "USAGE_EXPORTER_VCPU_WEIGHTS"

# name of the domain whose projects to monitor
project_domain_env_var = "USAGE_EXPORTER_PROJECT_DOMAINS"
default_project_domains = ["elixir"]

# id of the domain whose projects should be exported
project_domain_id_env_var = "USAGE_EXPORTER_PROJECT_DOMAIN_ID"

dummy_file_env_var = "USAGE_EXPORTER_DUMMY_FILE"

default_dummy_file = "resources/dummy_machines.toml"

UsageTuple = NamedTuple("UsageTuple", [("vcpu_hours", float), ("mb_hours", float)])

hour_timedelta = timedelta(hours=1)

script_start = datetime.now()


def sha256(content: str) -> str:
    s = sha256func()
    s.update(content.encode())
    return s.hexdigest()


@dataclass(frozen=True)
class OpenstackProject:
    id: str
    name: str
    domain_name: str
    domain_id: str


class _ExporterBase:
    def update(self):
        ...


class OpenstackExporter(_ExporterBase):
    def __init__(
        self,
        stats_start: datetime = datetime.today(),
        domains: Iterable[str] = None,
        domain_id: Optional[str] = None,
        vcpu_weights: Dict[int:int] = None,
        mb_weights: Dict[int:int] = None
    ) -> None:
        self.domains = set(domains) if domains else None
        self.domain_id = domain_id
        self.projects: Set[OpenstackProject] = set()
        self.stats_start = stats_start
        self.simple_project_usages = None
        self.vcpu_weights = vcpu_weights
        self.mb_weights = mb_weights
        try:
            self.cloud = openstack.connect()
        except keystoneauth1.exceptions.auth_plugins.MissingRequiredOptions:
            logging.exception(
                "Could not authenticate against OpenStack, Aborting! "
                "See following traceback."
            )
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
                    project_id=project.id,
                    project_name=project.name,
                    domain_name=project.domain_name,
                    domain_id=project.domain_id,
                ).set(usage_values[usage_name])

    def collect_usages(self, **query_args) -> Dict[OpenstackProject, Dict[str, float]]:
        """
        :param query_args: Additional parameters for the `os-simple-tenant-usage`-url
        """
        query_params = "&".join(
            "=".join((key, value)) for key, value in query_args.items()
        )
        project_usages: Dict[OpenstackProject, Dict[str, float]] = {}
        for project in self.projects:
            try:
                json_payload = self.cloud.compute.get(  # type: ignore
                    f"/os-simple-tenant-usage/{project.id}?" + query_params
                ).json()
                project_usage = json_payload["tenant_usage"]  # type: ignore
                if not project_usage:
                    logging.info(
                        "Project %s has no existing projects (in the requested date "
                        "range), skipping",
                        project,
                    )
                    continue
            except KeyError:
                logging.error(
                    "Received following invalid json payload: %s", json_payload
                )
                continue
            except BaseException:
                logging.exception("Received following exception:")
                continue

            project_usages[project] = {}

            for metric in project_metrics:
                instance_metric = "_".join(metric.split("_")[1:len(metric.split("_"))-1])
                total_usage = 0
                for instance in project_usage:
                    instance_hours = instance[HOURS_KEY]
                    if instance_hours > 0:
                        total_usage += (instance_hours * instance[instance_metric]) * 1 # here set weight
                if total_usage != project_usage[metric]:
                    logging.info("Warning the calculated result was un expected.  Metric_usage: %s, Calculates usage: %s", project_usage[metric], total_usage)
                else:
                    logging.info("SUCCESS: the new calculation works! %s = %s", project_usage[metric], total_usage)

#            project_usages[project] = {
#                metric: project_usage[metric] for metric in project_metrics
#            }

        return project_usages

    def get_instance_weight(self, metric_tag, metric_amount):
        metric_weights = {}
        if metric_tag == "vcpu":
            metric_weights = self.vcpu_weights
        elif metric_tag == "memory_mb":
            metric_weights = self.mb_weights
        if metric_weights != None:
            sorted_keys = sorted(metric_weights.keys())
            max_key = max(sorted_keys)
            for key in sorted_keys:
                if metric_amount <= key or max_key == key:
                    return metric_weights[key]
            logging.info("WARNING: The weight was set to one this should not happen though. Metric: %s, Weights: %s, Amount: %s"
                         "", metric_tag, str(metric_weights), str(metric_amount))
            return 1
        return 1

    def collect_projects(self) -> Set[OpenstackProject]:
        projects: Set[OpenstackProject] = set()
        if self.domain_id:
            for project in self.cloud.list_projects(domain_id=self.domain_id):
                projects.add(
                    OpenstackProject(
                        id=project.id,
                        name=project.name,
                        domain_name="UNKNOWN",
                        domain_id=self.domain_id,
                    )
                )
        elif self.domains:
            for domain_name in self.domains:
                domain = self.cloud.get_domain(name_or_id=domain_name)
                if not domain:
                    logging.info(
                        "Could not detect any domain with name %s. Skipping",
                        domain_name,
                    )
                    continue
                for project in self.cloud.list_projects(domain_id=domain.id):
                    projects.add(
                        OpenstackProject(
                            id=project.id,
                            name=project.name,
                            domain_name=domain.name,
                            domain_id=domain.id,
                        )
                    )
        else:
            for project in self.cloud.list_projects():
                projects.add(
                    OpenstackProject(
                        id=project.id,
                        name=project.name,
                        domain_name=self.cloud.get_domain(
                            name_or_id=project.domain_id
                        ).name,
                        domain_id=project.domain_id,
                    )
                )
        return projects


class ExistenceInformation(Enum):
    NO_EXISTENCE = 0
    SINCE_SCRIPT_START = 1
    SINCE_DATETIME = 2
    BETWEEN_DATETIMES = 3


@dataclass
class DummyMachine:
    """
    Representing a dummy machine causing usage to monitor.
    :param name: Currently not used outside but might be in future, therefore leave it
    :param cpus: Number of cpus the dummy machine is using.
    :param ram: Amount of RAM [GiB] the machine is using.
    :param existence: Determines whether the machine is *up* and its usage so far. In case
    of True the machine is considered booted up the instant this script is started. In
    case of False it hasn't been booted ever (no actual use case).
    In case of a single datetime the machine is considered *up* since that moment (for
    simplicity the timezone information are ignored). In case of a list of two datetimes
    the machine is considered *up* the time in between. The first one must be
    older/smaller than the second one and both but relative to the moment the script
    started both may lie in the future or past.
    """

    cpus: int = 4
    ram: int = 8
    existence: Union[bool, datetime, Tuple[datetime, datetime]] = True

    def __post_init__(self) -> None:
        if self.cpus <= 0 or self.ram <= 0:
            raise ValueError("`cpu` and `ram` must be positive")
        if isinstance(self.existence, (list, tuple)):
            if self.existence[0] > self.existence[1]:  # type: ignore
                raise ValueError(
                    "First existence-tuple datetime must be older than second one"
                )
            # remove any timezone information
            self.existence_information = ExistenceInformation.BETWEEN_DATETIMES
        elif isinstance(self.existence, datetime):
            self.existence_information = ExistenceInformation.SINCE_DATETIME
        elif isinstance(self.existence, bool):
            self.existence_information = (
                ExistenceInformation.SINCE_SCRIPT_START
                if self.existence
                else ExistenceInformation.NO_EXISTENCE
            )
        else:
            raise ValueError(
                f"Invalid type for param `existence` (got {type(self.existence)}"
            )

    @property
    def ram_mb(self) -> int:
        return self.ram * 1024

    def usage_value(self) -> UsageTuple:
        """
        Returns the total ram and cpu usage counted in hours of this machine, depending
        on its `existence` configuration`
        """
        now = datetime.now()
        if self.existence_information is ExistenceInformation.NO_EXISTENCE:
            return UsageTuple(0, 0)
        elif self.existence_information is ExistenceInformation.SINCE_SCRIPT_START:
            hours_existence = (datetime.now() - script_start) / hour_timedelta
            return UsageTuple(
                self.cpus * hours_existence, self.ram_mb * hours_existence
            )
        elif self.existence_information is ExistenceInformation.SINCE_DATETIME:
            # to satisfy `mypy` type checker
            boot_datetime = cast(datetime, self.existence)
            hours_existence = (
                now - boot_datetime.replace(tzinfo=None)
            ) / hour_timedelta
            # do not report negative usage in case the machine is not *booted yet*
            if hours_existence > 0:
                return UsageTuple(
                    self.cpus * hours_existence, self.ram_mb * hours_existence
                )
            else:
                return UsageTuple(0, 0)
        else:
            # to satisfy `mypy` type checker
            runtime_tuple = cast(Tuple[datetime, datetime], self.existence)
            boot_datetime = cast(datetime, runtime_tuple[0].replace(tzinfo=None))
            shutdown_datetime = cast(datetime, runtime_tuple[1].replace(tzinfo=None))
            if boot_datetime > now:
                # machine did not boot yet
                return UsageTuple(0, 0)
            elif shutdown_datetime < now:
                # machine did run already and is considered down
                hours_existence = (shutdown_datetime - boot_datetime) / hour_timedelta
            else:
                # machine booted in the past but is still existing
                hours_existence = (now - boot_datetime) / hour_timedelta
            return UsageTuple(
                self.cpus * hours_existence, self.ram_mb * hours_existence
            )


class DummyExporter(_ExporterBase):
    def __init__(
        self,
        dummy_values: TextIO,
        domains: Iterable[str] = None,
        domain_id: Optional[str] = None,
    ) -> None:
        self.dummy_values = toml.loads(dummy_values.read())
        self.domains = set(domains) if domains else None
        self.domain_id = domain_id
        self.projects: List[DummyProject] = []
        for project_name, project_content in self.dummy_values.items():
            machines = [
                DummyMachine(**machine) for machine in project_content["machines"]
            ]
            self.projects.append(
                DummyProject(
                    name=project_name,
                    domain_name=project_content.get("domain", ""),
                    machines=machines,
                )
            )
        self.update()

    def update(self) -> None:
        for project in self.projects:
            if self.domain_id and project.domain_id != self.domain_id:
                logging.info(
                    "Skipping exporting project %s since its domain id "
                    "is not requested",
                    project,
                )
                continue
            if self.domains and project.domain_name not in self.domains:
                logging.info(
                    "Skipping exporting project %s since its domain "
                    "is not requested",
                    project,
                )
                continue
            project_usages = [machine.usage_value() for machine in project.machines]
            vcpu_hours = sum(usage.vcpu_hours for usage in project_usages)
            mb_hours = sum(usage.mb_hours for usage in project_usages)
            project_metrics["total_vcpus_usage"].labels(
                project_id=project.id,
                project_name=project.name,
                domain_name=project.domain_name,
                domain_id=project.domain_id,
            ).set(vcpu_hours)
            project_metrics["total_memory_mb_usage"].labels(
                project_id=project.id,
                project_name=project.name,
                domain_name=project.domain_name,
                domain_id=project.domain_id,
            ).set(mb_hours)


@dataclass
class DummyProject:
    name: str
    machines: List[DummyMachine]
    domain_name: str = ""

    def __post_init__(self):
        self.id = sha256(self.name)[-16:]
        self.domain_id = sha256(self.domain_name)[-16:]


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
        values are calculated base on the configured existence, take a look at the
        example file for an explanation {default_dummy_file}. Can also be provided via
        environment variable ${dummy_file_env_var}""",
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
        ${project_domain_env_var}. If no domains are specified all readable projects
        are exported.""",
    )
    parser.add_argument(
        "--domain-id",
        default=getenv(project_domain_id_env_var, ""),
        help=f"""Only export usages of projects belonging to the domain identified by
        the given ID. Takes precedence over any specified domain and default values. Can
        also be set via ${project_domain_id_env_var}""",
    )
    parser.add_argument(
        "--vcpu-weights",
        default=getenv(vcpu_weights, ""),
        type=str,
        help=f"""Use weights for different numbers of cpus in a vm. Value is given as
         the string representation of a dictionary with ints as keys and as values.
         a weight of 1 means no change. Above 1 its more expensive, under one it is less 
         expensive. Not available with dummy mode. Can also be set via ${vcpu_weights}""",
    )
    parser.add_argument(
        "--mb-weights",
        default=getenv(project_mb_weights, ""),
        type=str,
        help=f"""Use weights for different numbers of mb (of ram) in a vm. Value is given as
         the string representation of a dictionary with ints as keys and as values.
         a weight of 1 means no change. Above 1 its more expensive, under one it is less 
         expensive. Not available with dummy mode. Can also be set via ${project_mb_weights}""",
    )
    parser.add_argument(
        "-s",
        "--start",
        type=valid_date,
        default=getenv(start_date_env_var, datetime.today()),
        help=f"""Beginning time of stats (YYYY-MM-DD). If set the value of environment
        variable ${start_date_env_var} is used. Uses maya for parsing.""",
    )
    parser.add_argument(
        "-i",
        "--update-interval",
        type=int,
        default=int(getenv(update_interval_env_var, 300)),
        help=f"""Time to sleep between intervals, in case the calls cause to much load on
        your openstack instance. Defaults to the value of environment variable
        ${update_interval_env_var} or 300 (in seconds)""",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=8080, help="Port to provide metrics on"
    )
    args = parser.parse_args()

    if args.dummy_data:
        logging.info("Using dummy export with data from %s", args.dummy_data.name)
        exporter = DummyExporter(args.dummy_data, args.domain, args.domain_id)
    elif getenv(dummy_file_env_var):
        logging.info("Using dummy export with data from %s", getenv(dummy_file_env_var))
        # if the default dummy data have been used we need to open them, argparse
        # hasn't done this for us since the default value has not been a string
        with open(getenv(dummy_file_env_var)) as file:
            exporter = DummyExporter(file, args.domain, args.domain_id)
    else:
        try:
            logging.info("Using regular openstack exporter")
            exporter = OpenstackExporter(
                domains=args.domain, stats_start=args.start, domain_id=args.domain_id,
                vcpu_weights=ast.literal_eval(args.vcpu_weights), mb_weights=ast.literal_eval(args.mb_weights)
            )
        except ValueError:
            return 1
    logging.info(f"Beginning to serve metrics on port {args.port}")
    prometheus_client.start_http_server(args.port)
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
