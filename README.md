# OS_project_usage_exporter

Export the usage information of your running projects inside OpenStack via your
(hopefully) existing Prometheus-Setup. Currently only two metrics are exported, namely
the values of `total_memory_mb_usage` and `total_vcpus_usage` of the
[`os-simple-tenant-usage`-API](https://developer.openstack.org/api-ref/compute/?expanded=list-tenant-usage-statistics-for-all-tenants-detail#usage-reports-os-simple-tenant-usage).
Exported labels per project are:

- `domain_id`
- `domain_name`
- `project_id`
- `project_name`

## Requirements/Installation

All production and development dependencies are managed via
[*pipenv*](https://pipenv.readthedocs.io). Therefore simply go via `pipenv install` or
start directly with one of the modi listed below. You can activate the virtual
environment via `pipenv shell` or simply prefix any command with `pipenv run` to have it
run inside the corresponding environment.

A [docker image](https://hub.docker.com/r/tluettje/os_project_usage_exporter/) is
available as well and all command line options do have corresponding environment
variables.

## Usage

```
usage: project_usage_exporter.py [-h] [-d DUMMY_DATA]
                                 [--domain [DOMAIN [DOMAIN ...]]] [-s START]
                                 [-i UPDATE_INTERVAL] [-p PORT]

Query project usages from an openstack instance and provide it in a prometheus
compatible format. Alternatively develop in local mode and emulate machines
and projects.

optional arguments:
  -h, --help            show this help message and exit
  -d DUMMY_DATA, --dummy-data DUMMY_DATA
                        Use dummy values instead of connecting to an openstack
                        instance. Usage values are calculated base on the
                        configured uptime, take a look at the example file for
                        an explanation resources/dummy_machines.toml. Can also
                        be provided via environment variable
                        USAGE_EXPORTER_DUMMY_FILE (default: None)
  --domain [DOMAIN [DOMAIN ...]]
                        Only export usages of projects belonging to one of the
                        given domains. Separate them via comma if passing via
                        environment variable (USAGE_EXPORTER_PROJECT_DOMAINS).
                        If no domains are specified all readable projects are
                        exported. (default: ['elixir'])
  -s START, --start START
                        Beginning time of stats (YYYY-MM-DD). If set the value
                        of USAGE_EXPORTER_START_DATE is used. Uses maya for
                        parsing. (default: 2018-12-12 13:37:47.437867)
  -i UPDATE_INTERVAL, --update-interval UPDATE_INTERVAL
                        Time to sleep between intervals, in case the calls
                        cause to much load on your openstack instance.
                        Defaults to the value of
                        $USAGE_EXPORTER_UPDATE_INTERVAL or 300 (in seconds)
                        (default: 300)
  -p PORT, --port PORT  Port to provide metrics on (default: 8080)

MIT @ tluettje
```

## Development mode/Preview

If you simply want to take a look at the output, try any modification or do not have any
access to an OpenStack instance you can emulate running projects and machines with
simple `toml` files. A few profiles are available inside the `/resources` folder.

```shell
pipenv run ./project_usage_exporter.py \
            --dummy-data resources/dummy_machines.toml \
            --update-interval 10 --domain
```
or
```
docker run -e USAGE_EXPORTER_DUMMY_FILE=/code/resources/dummy_machines.toml \
           -e USAGE_EXPORTER_UPDATE_INTERVAL=10 \
           -e USAGE_EXPORTER_PROJECT_DOMAINS= \
           -p 8080:8080 tluettje/os_project_usage_exporter:v2
```
This will emulate a few projects with machines without any domain restrictions. The
`resources` folder is also available inside the docker container at `/code/resources`.

## Production Mode

Simply source your `admin-openrc.sh` before starting the exporter. Depending on the size
of your instance you might want test different values for `--update-interval` to
determine how much load (if any) is caused by the queries.

In case of docker you have to insert your password inside the `openrc` file, and remove
any lines other than `key=value` pairs. Surrounding quotes will be considered part of
the values therefore remove them as well.

```
docker run --env-file openrc -p 8080:8080 tluettje/os_project_usage_exporter:v2
```
