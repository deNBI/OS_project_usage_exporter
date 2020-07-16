from datetime import datetime, timedelta

import toml
from munch import Munch
import requests
from typing import (
    Tuple,
    Union,
    cast,
)
from enum import Enum
import json

hour_timedelta = timedelta(hours=1)


class DummyCloud:

    def __init__(self, dummy_file, start=None):
        self.dummy_file = dummy_file
        self.dummy_values = toml.loads(self.dummy_file.read())
        if start is not None:
            script_start = start
        else:
            script_start = datetime.now()
        self.compute = Compute(self.dummy_values, script_start)

    def load_toml(self):
        self.dummy_file.seek(0)
        self.dummy_values = toml.loads(self.dummy_file.read())
        self.compute.reload(self.dummy_values)

    def list_projects(self, domain_id=None):
        self.load_toml()
        projects_return = []
        for domain_name, domain_content in self.dummy_values.items():
            if domain_id is not None and domain_content.get("domain_id", "UNKNOWN_DOMAIN_ID") != domain_id:
                continue
            projects_in_domain = domain_content.get("projects", [])
            for project_in_domain in projects_in_domain:
                project = Munch()
                project.id = project_in_domain.get("project_id", "UNKNOWN_ID")
                project.name = project_in_domain.get("project_name", "UNKNOWN_NAME")
                projects_return.append(project)
        return projects_return

    def get_domain(self, name_or_id):
        self.load_toml()
        for domain_name, domain_content in self.dummy_values.items():
            file_domain_id = domain_content.get("domain_id", "UNKNOWN_DOMAIN_ID")
            if domain_name == name_or_id or file_domain_id == name_or_id:
                return Munch(id=file_domain_id, name=domain_name)
        return None


class ExistenceInformation(Enum):
    NO_EXISTENCE = 0
    SINCE_SCRIPT_START = 1
    SINCE_DATETIME = 2
    BETWEEN_DATETIMES = 3


class Compute:

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

        def __init__(self,
                     cpus: int = 4,
                     ram: int = 8,
                     existence: Union[bool, datetime, Tuple[datetime, datetime]] = True,
                     metadata=None,
                     instance_id="UNKNOWN_ID"):
            self.cpus = cpus
            self.ram = ram
            self.existence = existence
            self.metadata = metadata
            self.instance_id = instance_id
            self.init_existence_information()

        def init_existence_information(self) -> None:
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

        def compute_server_info(self, requested_start_date, script_start) -> Munch:
            requested_start_date = datetime.strptime(requested_start_date, "%Y-%m-%dT%H:%M:%S.%f")
            now = datetime.now()
            return_dict = Munch()
            return_dict.hours = 0.0
            return_dict.vcpus = self.cpus
            return_dict.memory_mb = self.ram_mb
            return_dict.started_at = script_start.strftime("%Y-%m-%dT%H:%M:%S.%f")
            return_dict.instance_id = self.instance_id
            if self.existence_information is ExistenceInformation.SINCE_SCRIPT_START:
                if requested_start_date > script_start:
                    hours_existence = (datetime.now() - requested_start_date) / hour_timedelta
                else:
                    hours_existence = (datetime.now() - script_start) / hour_timedelta
                return_dict.hours = hours_existence
            elif self.existence_information is ExistenceInformation.NO_EXISTENCE:
                return return_dict
            elif self.existence_information is ExistenceInformation.SINCE_DATETIME:
                # to satisfy `mypy` type checker
                boot_datetime = cast(datetime, self.existence)
                if requested_start_date > boot_datetime:
                    hours_existence = (datetime.now() - requested_start_date) / hour_timedelta
                else:
                    hours_existence = (now - boot_datetime.replace(tzinfo=None)) / hour_timedelta

                # do not report negative usage in case the machine is not *booted yet*
                return_dict.started_at = boot_datetime.strftime("%Y-%m-%dT%H:%M:%S.%f")
                if hours_existence > 0:
                    return_dict.hours = hours_existence
            else:
                # to satisfy `mypy` type checker
                runtime_tuple = cast(Tuple[datetime, datetime], self.existence)
                boot_datetime = cast(datetime, runtime_tuple[0].replace(tzinfo=None))
                shutdown_datetime = cast(datetime, runtime_tuple[1].replace(tzinfo=None))
                return_dict.started_at = boot_datetime.strftime("%Y-%m-%dT%H:%M:%S.%f")
                if boot_datetime > now:
                    # machine did not boot yet
                    hours_existence = 0.0
                elif shutdown_datetime < now:
                    # machine did run already and is considered down
                    if requested_start_date > boot_datetime:
                        hours_existence = (shutdown_datetime - requested_start_date) / hour_timedelta
                    else:
                        hours_existence = (shutdown_datetime - boot_datetime) / hour_timedelta
                else:
                    # machine booted in the past but is still existing
                    if requested_start_date > boot_datetime:
                        hours_existence = (now - requested_start_date) / hour_timedelta
                    else:
                        hours_existence = (now - boot_datetime) / hour_timedelta
                return_dict.hours = hours_existence
            return return_dict

        def get_details(self):
            return {"id": self.instance_id, "metadata": self.metadata}

    def __init__(self, dummy_values, script_start):
        self.dummy_values = dummy_values
        self.os_simple_tenant_usage_string = "/os-simple-tenant-usage/"
        self.server_detail_all_tenants_string = "/servers/detail?all_tenants=false&project_id="
        self.script_start = script_start

    def reload(self, dummy_values):
        self.dummy_values = dummy_values

    def get_tenant_usage(self, project, requested_start_date):
        server_usages = []
        start = requested_start_date
        stop = datetime.now()

        for toml_machine in project.get("machines", []):
            machine = self.DummyMachine(toml_machine.get("cpus", 4), toml_machine.get("ram", 8),
                                        toml_machine.get("existence", True), toml_machine.get("metadata", {}),
                                        toml_machine.get("instance_id", "UNKNOWN_ID"))
            usage_temp = machine.compute_server_info(requested_start_date, self.script_start)
            server_usages.append(usage_temp)

        return {
            "tenant_usage": {
                "tenant_id": project.get("project_id", "UNKNOWN_ID"),
                "server_usages": server_usages,
                "start": start,
                "stop": stop
            }
        }

    def get_server_details(self, project):
        servers = []
        for toml_machine in project.get("machines", []):
            machine = self.DummyMachine(toml_machine.get("cpus", 4), toml_machine.get("ram", 8),
                                        toml_machine.get("existence", True), toml_machine.get("metadata", {}),
                                        toml_machine.get("instance_id", "UNKNOWN_ID"))
            temp_dict = machine.get_details()
            servers.append(temp_dict)
        return {"servers": servers}

    def get(self, url):
        if not isinstance(url, str):
            raise TypeError
        if self.os_simple_tenant_usage_string in url:
            request = url.split(self.os_simple_tenant_usage_string, 1)[1]
            requested_project_id = request.split("?", 1)[0]
            requested_start_date = request.split("start=", 1)[1]
            for domain_name, domain_content in self.dummy_values.items():
                projects_in_domain = domain_content.get("projects", [])
                for project_in_domain in projects_in_domain:
                    if project_in_domain.get("project_id", "UNKNOWN_ID") == requested_project_id:
                        tenant_usage = self.get_tenant_usage(project_in_domain, requested_start_date)
                        response = requests.Response()
                        response._content = json.dumps(tenant_usage, default=str).encode("utf-8")
                        return response
        elif self.server_detail_all_tenants_string in url:
            requested_project_id = url.split("=", 2)[2]
            for domain_name, domain_content in self.dummy_values.items():
                projects_in_domain = domain_content.get("projects", [])
                for project_in_domain in projects_in_domain:
                    if project_in_domain.get("project_id", "UNKNOWN_ID") == requested_project_id:
                        server_details = self.get_server_details(project_in_domain)
                        response = requests.Response()
                        response._content = json.dumps(server_details, default=str).encode("utf-8")
                        return response

        response = requests.Response()
        response._content = b'{}'
        return response
