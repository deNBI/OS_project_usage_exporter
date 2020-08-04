#!/usr/bin/env python3
import os
import json

import openstack  # type: ignore
import logging


class OSOutputTester:
    def __init__(
            self,
    ) -> None:
        self.USERNAME = os.environ["OS_USERNAME"]
        self.PASSWORD = os.environ["OS_PASSWORD"]
        self.PROJECT_NAME = os.environ["OS_PROJECT_NAME"]
        self.USER_DOMAIN_NAME = os.environ["OS_USER_DOMAIN_NAME"]
        self.AUTH_URL = os.environ["OS_AUTH_URL"]
        self.PROJECT_DOMAIN_ID = os.environ["OS_PROJECT_DOMAIN_ID"]
        self.REGION_NAME = os.environ["OS_REGION_NAME"]
        self.INTERFACE = os.environ["OS_INTERFACE"]
        self.IDENTITDY = os.environ["OS_IDENTITY_API_VERSION"]
        self.PROJECT_ID = os.environ["OS_PROJECT_ID"]

        try:
            self.cloud = openstack.connection.Connection(
                username=self.USERNAME,
                password=self.PASSWORD,
                auth_url=self.AUTH_URL,
                project_name=self.PROJECT_NAME,
                user_domain_name=self.USER_DOMAIN_NAME,
                project_domain_id=self.PROJECT_DOMAIN_ID,
                region_name=self.REGION_NAME,
                identity_interface = self.INTERFACE
            )
            self.cloud.authorize()
        except Exception as e:
            print("Could not authenticate against OpenStack, Aborting! "
                  "See following traceback.")
            print(e)
            raise ValueError
        print("Connected to Openstack!")

    def list_projects(self):
        try:
            print("------PRINTING ALL PROJECTS---------------------------------------------------------")
            projects = self.cloud.list_projects()
            print(projects)
            print(len(projects))
            print()
        except Exception as e:
            print("Could not load all projects:")
            logging.exception(e)
            print()
        try:
            print("------PRINTING PROJECTS FOR DOMAIN_ID {0}--------------------------------------------------------".format(self.PROJECT_DOMAIN_ID))
            projects_two = self.cloud.list_projects(domain_id=self.PROJECT_DOMAIN_ID)
            print(projects_two)
            print(len(projects_two))
            print()
        except Exception as e:
            print("Could not load projects with domain id")
            logging.exception(e)
            print()

    def get_domain(self):
        try:
            print("------PRINTING DOMAIN------------------------------------------------------------------------------")
            domain = self.cloud.get_domain(name_or_id=self.USER_DOMAIN_NAME)
            print(json.dumps(domain, indent=2))
            print()
        except Exception as e:
            print("Could not get domain")
            logging.exception(e)
            print()

    def compute_get(self):
        try:
            print("------PRINTING COMPUTE GET OS SIMPLE TENANT USAGE--------------------------------------------------")
            payload = self.cloud.compute.get(  # type: ignore
                f"/os-simple-tenant-usage/{self.PROJECT_ID}?start=2020-07-15T15:07:51.211724"
            ).json()
            print(json.dumps(payload, indent=2))
            print()
        except Exception as e:
            print("Could not compute")
            logging.exception(e)
            print()
        try:
            print("------PRINTING COMPUTE GET SERVER DETAILS----------------------------------------------------------")
            payload_two = self.cloud.compute.get(  # type: ignore
                f"/servers/detail?all_tenants=false&project_id=" + self.PROJECT_ID
            ).json()
            print(json.dumps(payload_two, indent=2))
            print(len(payload_two["servers"]))
        except Exception as e:
            print("Could not compute the second")
            logging.exception(e)
            print()


def main():
    dummy_cloud_tester = OSOutputTester()
    dummy_cloud_tester.list_projects()
    dummy_cloud_tester.get_domain()
    dummy_cloud_tester.compute_get()


if __name__ == "__main__":

    exit(main())
