#!/usr/bin/env python3
"""
Query project usages from an openstack instance and provide it in a prometheus compatible
format.
Alternatively develop in local mode and emulate machines and projects.
"""
import json
from distutils.util import strtobool
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
    Dict,
    Iterable,
)
from time import sleep
from datetime import datetime, timedelta
from os import getenv
from dataclasses import dataclass
from hashlib import sha256 as sha256func

import toml

from dummy_cloud import DummyCloud

import openstack  # type: ignore
import prometheus_client  # type: ignore
import keystoneauth1  # type: ignore
import maya
import ast
import requests

# enable logging for now
format = "%(asctime)s - %(levelname)s [%(name)s] %(threadName)s %(message)s"
logging.basicConfig(level=logging.INFO, format=format)
logger = logging.getLogger()

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
weights_update_frequency_env_var = "USAGE_EXPORTER_WEIGHT_UPDATE_FREQUENCY"
weights_update_endpoint_env_var = "USAGE_EXPORTER_WEIGHTS_UPDATE_ENDPOINT"
simple_vm_project_id_env_var = "USAGE_EXPORTER_SIMPLE_VM_PROJECT_ID"
simple_vm_project_name_tag_env_var = "USAGE_EXPORTER_SIMPLE_VM_PROJECT_TAG"
verbosity_env_var = "USAGE_EXPORTER_VERBOSE_MODE"
start_date_endpoint_env_var = "USAGE_EXPORTER_START_DATE_ENDPOINT"

# name of the domain whose projects to monitor
project_domain_env_var = "USAGE_EXPORTER_PROJECT_DOMAINS"
default_project_domains = ["elixir"]

# id of the domain whose projects should be exported
project_domain_id_env_var = "USAGE_EXPORTER_PROJECT_DOMAIN_ID"

dummy_file_env_var = "USAGE_EXPORTER_DUMMY_FILE"

dummy_weights_file_env_var = "USAGE_EXPORTER_DUMMY_WEIGHTS_FILE"

default_dummy_file = "resources/dummy_cc.toml"

default_dummy_weights_file = "resources/dummy_weights.toml"


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
    is_simple_vm_project: bool


class _ExporterBase:
    def update(self):
        ...


class OpenstackExporter(_ExporterBase):
    def __init__(
        self,
        stats_start: datetime = datetime.today(),
        domains: Iterable[str] = None,
        domain_id: Optional[str] = None,
        simple_vm_project="",
        simple_vm_tag=None,
        dummy_file: TextIO = None
    ) -> None:
        self.domains = set(domains) if domains else None
        self.domain_id = domain_id
        self.projects: Set[OpenstackProject] = set()
        self.stats_start = stats_start
        self.simple_project_usages = None
        self.simple_vm_project = simple_vm_project
        self.simple_vm_tag = simple_vm_tag
        self.dummy_file = dummy_file
        self.weights = None
        if self.dummy_file is not None:
            self.cloud = DummyCloud(self.dummy_file, self.stats_start)
        else:
            try:
                self.cloud = openstack.connect()
            except keystoneauth1.exceptions.auth_plugins.MissingRequiredOptions:
                logger.exception(
                    "Could not authenticate against OpenStack, Aborting! "
                    "See following traceback."
                )
                logger.info("Consider using the dummy mode for testing")
                raise ValueError

    def update(self) -> None:
        self.projects = self.collect_projects()
        logger.debug(f"Collected projects: {self.projects}")
        self.usages = self.collect_usages(
            start=self.stats_start.strftime("%Y-%m-%dT%H:%M:%S.%f")
        )
        logger.debug(f"Collected usages: {self.usages}")
        self.set_metrics()

    def update_weights(self, new_weights) -> None:
        if self.weights != new_weights:
            logger.info(f"Updating weights: Old: {self.weights}. New: {new_weights}")
        if len(new_weights) == 0:
            logger.warning(f"Updated weights are empty, which should not happen. "
                           f"Please check configuration or activate debug mode.")
        self.weights = new_weights

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
                    logger.info(
                        "Project %s has no existing projects (in the requested date "
                        "range), skipping",
                        project,
                    )
                    continue
            except KeyError:
                logger.error(
                    "Received following invalid json payload: %s", json_payload
                )
                continue
            except BaseException as e:
                logger.exception(f"Received following exception:\n{e}")
                continue
            if project.is_simple_vm_project:
                if self.simple_vm_tag is None:
                    logger.error("The simple vm tag is not set, please set the simple vm metadata "
                                 "tag for simple vm tracking")
                else:
                    json_payload_metadata = self.cloud.compute.get(  # type: ignore
                        f"/servers/detail?all_tenants=True&project_id=" + project.id
                    ).json()
                    instance_id_to_project_dict = {}
                    for instance in json_payload_metadata['servers']:
                        try:
                            instance_id_to_project_dict[instance['id']] = instance['metadata'][self.simple_vm_tag]
                        except KeyError as e:
                            logger.debug(f"Could not get metadata for instance with id {instance['id']}"
                                         f"Error message. {e}")
                            continue
                    simple_vm_project_names = list(set(instance_id_to_project_dict.values()))
                    for simple_vm_project_name in simple_vm_project_names:
                        svm_project = OpenstackProject(
                            id=project.id,
                            name=simple_vm_project_name,
                            domain_name=project.domain_name,
                            domain_id=project.domain_id,
                            is_simple_vm_project=True,
                        )
                        project_usages[svm_project] = {}
                        for metric in project_metrics:
                            instance_metric = "_".join(metric.split("_")[1:len(metric.split("_")) - 1])
                            total_usage = 0
                            for instance in project_usage["server_usages"]:
                                try:
                                    if instance_id_to_project_dict[instance["instance_id"]] == simple_vm_project_name:
                                        instance_hours = instance[HOURS_KEY]
                                        if instance_hours > 0:
                                            metric_amount = instance[instance_metric]
                                            if metric == "total_memory_mb_usage":
                                                metric_amount = int(metric_amount / 1024)
                                            total_usage += (instance_hours * metric_amount) * self.get_instance_weight(
                                                instance_metric, metric_amount, instance["started_at"])
                                except KeyError as e:
                                    logger.debug(f"Catching key error: {e}")
                                    continue
                            project_usages[svm_project][metric] = total_usage
            else:
                project_usages[project] = {}
                for metric in project_metrics:
                    instance_metric = "_".join(metric.split("_")[1:len(metric.split("_"))-1])
                    total_usage = 0
                    for instance in project_usage["server_usages"]:
                        instance_hours = instance[HOURS_KEY]
                        if instance_hours > 0:
                            metric_amount = instance[instance_metric]
                            if metric == "total_memory_mb_usage":
                                metric_amount = int(metric_amount / 1024)
                            total_usage += (instance_hours * metric_amount) * self.get_instance_weight(instance_metric, metric_amount, instance["started_at"])
                    project_usages[project][metric] = total_usage
        return project_usages

    def get_instance_weight(self, metric_tag, metric_amount, started_date):
        instance_started_timestamp = datetime.strptime(started_date, "%Y-%m-%dT%H:%M:%S.%f").timestamp()
        if self.weights is not None and len(self.weights) != 0:
            sorted_timestamps = sorted(self.weights.keys())
            min_timestamp = min(sorted_timestamps)
            associated_weights = None
            for timestamp in sorted_timestamps[::-1]:
                if instance_started_timestamp >= timestamp or min_timestamp == timestamp:
                    associated_weights = self.weights[timestamp]
                    break
            if associated_weights is not None:
                metric_weights = associated_weights[metric_tag]
                if len(metric_weights) == 0:
                    logger.debug(f"No weights for {metric_tag}. Using 1.")
                    return 1
                sorted_keys = sorted(metric_weights.keys())
                max_key = max(sorted_keys)
                for key in sorted_keys:
                    if metric_amount <= key or max_key == key:
                        return metric_weights[key]
                logger.debug(
                    "Warning: The weight was set to 1 this should not happen though. Metric: %s, Weights: %s, Amount: %s"
                    "", metric_tag, str(metric_weights), str(metric_amount))
                return 1
            else:
                logger.debug("Warning: could not determine metric: %s for timestamp %s", self.weights,
                             instance_started_timestamp)
                return 1
        logger.debug("Warning: no weights set!")
        return 1

    def collect_projects(self) -> Set[OpenstackProject]:
        projects: Set[OpenstackProject] = set()
        if self.domain_id:
            for project in self.cloud.list_projects(domain_id=self.domain_id):
                add_project(project.id, project.name, self.domain_id, "UNKNOWN", self.simple_vm_project, projects)
        elif self.domains:
            for domain_name in self.domains:
                domain = self.cloud.get_domain(name_or_id=domain_name)
                if not domain:
                    logger.info(
                        "Could not detect any domain with name %s. Skipping",
                        domain_name,
                    )
                    continue
                for project in self.cloud.list_projects(domain_id=domain.id):
                    add_project(project.id, project.name, self.domain_id, domain.name, self.simple_vm_project, projects)
        else:
            for project in self.cloud.list_projects():
                domain_name = self.cloud.get_domain(name_or_id=project.domain_id).name
                add_project(project.id, project.name, project.domain_id, domain_name, self.simple_vm_project, projects)
        return projects


def add_project(id, name, domain_id, domain_name, simple_vm_id, projects):
    is_simple_vm_project = False
    if id == simple_vm_id:
        is_simple_vm_project = True
    projects.add(
        OpenstackProject(
            id=id,
            name=name,
            domain_name=domain_name,
            domain_id=domain_id,
            is_simple_vm_project=is_simple_vm_project,
        )
    )


def valid_date(s):
    try:
        return maya.when(s).datetime()
    except ValueError:
        msg = f"Unrecognized date: '{s}'."
        raise ArgumentTypeError(msg)


def get_dummy_weights(file):
    file.seek(0)
    dummy_weights = toml.loads(file.read())
    response = requests.Response()
    response._content = json.dumps(dummy_weights["weights"], default=str).encode("utf-8")
    return response


def convert_verbose():
    try:
        returned_bool = strtobool(getenv(verbosity_env_var, "False").strip())
        return returned_bool
    except Exception as e:
        return False


def nullable_string(val):
    if not val:
        return None
    return val


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
        values are calculated based on the configured existence. Toml files can be updated on the fly as they are read 
        every time a dummy-cloud function is called (functions of nested classes excluded).
        Take a look at the example file for an explanation {default_dummy_file}. Can also be provided via
        environment variable ${dummy_file_env_var}""",
    )
    parser.add_argument(
        "-w",
        "--dummy-weights",
        type=FileType(),
        help=f"""Use dummy weight endpoint instead of connecting to the api. Take a look at the
        example file for an explanation {default_dummy_weights_file}. Can also be provided via
        environment variable ${dummy_weights_file_env_var}""",
    )
    parser.add_argument(
        "--domain",
        default=[
            domain.strip()
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
        default=getenv(project_domain_id_env_var, "").strip(),
        help=f"""Only export usages of projects belonging to the domain identified by
        the given ID. Takes precedence over any specified domain and default values. Can
        also be set via ${project_domain_id_env_var}""",
    )
    parser.add_argument(
        "--simple-vm-id",
        default=getenv(simple_vm_project_id_env_var, "").strip(),
        type=str,
        help=f"""The ID of the Openstack project, that hosts the SimpleVm projects. 
        Can also be set vis ${simple_vm_project_id_env_var}""",
    )
    parser.add_argument(
        "--simple-vm-tag",
        default=getenv(simple_vm_project_name_tag_env_var, "project_name").strip(),
        type=str,
        help=f"""The metadata of the Openstack project, that hosts the SimpleVm projects. It is used
        to differentiate the simple vm projects, default: project_name
        Can also be set vis ${simple_vm_project_name_tag_env_var}""",
    )
    parser.add_argument(
        "--weight-update-frequency",
        type=int,
        default=int(getenv(weights_update_frequency_env_var, 10)),
        help=f"""The frequency of checking if there is a weight update. Is  a multiple of the update interval length
        . Defaults to the value of environment variable ${weights_update_frequency_env_var} or 10""",
    )
    parser.add_argument(
        "--weight-update-endpoint",
        type=str,
        default=getenv(weights_update_endpoint_env_var, "").strip(),
        help=f"""The endpoint url where the current weights can be updated
        . Defaults to the value of environment variable ${weights_update_endpoint_env_var} or will be left blank""",
    )
    parser.add_argument(
        "--start-date-endpoint",
        type=nullable_string,
        default=getenv(start_date_endpoint_env_var, "").strip(),
        help=f"""The endpoint url where the start date can be requested.
        If defined, requested date takes precedence over all other start date arguments.
        Defaults to the value of environment variable ${start_date_endpoint_env_var} or will be left blank""",
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
        default=int(getenv(update_interval_env_var, 30)),
        help=f"""Time to sleep between intervals, in case the calls cause to much load on
        your openstack instance. Defaults to the value of environment variable
        ${update_interval_env_var} or 300 (in seconds)""",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=8080, help="Port to provide metrics on"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=convert_verbose(),
        help="Activate logging debug level"
    )
    args = parser.parse_args()
    if args.verbose:
        logger.setLevel(logging.DEBUG)
        logger.debug("Debug mode activated.")
    if args.start_date_endpoint:
        try:
            start_date_response = requests.get(args.start_date_endpoint)
            start_date_response = start_date_response.json()
            args.start = maya.when(start_date_response[0]["start_date"]).datetime()
        except Exception as e:
            logger.exception(f"Exception when getting start date from endpoint. Exception message: {e}. "
                              f"Traceback following:\n")
            return 1
    if args.dummy_data:
        logger.info("Using dummy export with data from %s", args.dummy_data.name)
        try:
            exporter = OpenstackExporter(
                domains=args.domain, stats_start=args.start, domain_id=args.domain_id,
                simple_vm_project=args.simple_vm_id, simple_vm_tag=args.simple_vm_tag, dummy_file=args.dummy_data
            )
        except ValueError as e:
            return 1
    elif getenv(dummy_file_env_var):
        logger.info("Using dummy export with data from %s", getenv(dummy_file_env_var))
        # if the default dummy data have been used we need to open them, argparse
        # hasn't done this for us since the default value has not been a string
        try:
            with open(getenv(dummy_file_env_var)) as file:
                exporter = OpenstackExporter(
                    domains=args.domain, stats_start=args.start, domain_id=args.domain_id,
                    simple_vm_project=args.simple_vm_id, simple_vm_tag=args.simple_vm_tag, dummy_file=file
                )
        except ValueError as e:
            return 1
    else:
        try:
            logger.info("Using regular openstack exporter")
            exporter = OpenstackExporter(
                domains=args.domain, stats_start=args.start, domain_id=args.domain_id,
                simple_vm_project=args.simple_vm_id, simple_vm_tag=args.simple_vm_tag
            )
        except ValueError as e:
            return 1
    logger.info(f"Beginning to serve metrics on port {args.port}")
    prometheus_client.start_http_server(args.port)
    laps = args.weight_update_frequency
    if args.dummy_weights or getenv(dummy_weights_file_env_var):
        args.weight_update_endpoint = "dummy-endpoint"
    while True:
        if args.weight_update_endpoint != "":
            if laps >= args.weight_update_frequency:
                try:
                    if args.dummy_weights:
                        weight_response = get_dummy_weights(args.dummy_weights)
                    elif getenv(dummy_weights_file_env_var):
                        with open(getenv(dummy_weights_file_env_var)) as file:
                            weight_response = get_dummy_weights(file)
                    else:
                        weight_response = requests.get(args.weight_update_endpoint)
                    current_weights = {
                        x['resource_set_timestamp']: {'memory_mb': {y['value']: y['weight'] for y in x['memory_mb']},
                                                      'vcpus': {y['value']: y['weight'] for y in x['vcpus']}} for
                        x in weight_response.json()}
                    logger.debug("Updated credits weights, new weights: " + str(current_weights))
                    exporter.update_weights(current_weights)
                except Exception as e:
                    logger.exception(
                        f"Received exception {e} while trying to update the credit weights, check if credit endpoint {args.weight_update_endpoint}"
                        f" is accessible or contact the denbi team to check if the weights are set correctly. Traceback following."
                    )
                finally:
                    laps = 0
            else:
                laps += 1
        try:
            sleep(args.update_interval)
            exporter.update()
        except KeyboardInterrupt:
            logger.info("Received Ctrl-c, exiting.")
            return 0
        except Exception as e:
            logger.exception(
                f"Received unexpected exception {e}. Traceback following."
            )
            return 1


if __name__ == "__main__":

    exit(main())
