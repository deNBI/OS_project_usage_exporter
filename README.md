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
All dependencies are managed with a requirements.txt. Create a virtual environment with a tool of your choosing 
(e.g. [*pyenv*](https://github.com/pyenv/pyenv) with [*pyenv-virtualenv*](https://github.com/pyenv/pyenv-virtualenv) or [*venv*](https://docs.python.org/3/library/venv.html)) and
install with `pip install -r requirements.txt`.

A [docker image](https://hub.docker.com/r/denbicloud/os_project_usage_exporter/) is
available as well and all command line options do have corresponding environment
variables.

## Usage

```
usage: project_usage_exporter.py [-h] [-d DUMMY_DATA] [-w DUMMY_WEIGHTS]
                                 [--domain [DOMAIN [DOMAIN ...]]]
                                 [--domain-id DOMAIN_ID]
                                 [--simple-vm-id SIMPLE_VM_ID]
                                 [--simple-vm-tag SIMPLE_VM_TAG]
                                 [--weight-update-frequency WEIGHT_UPDATE_FREQUENCY]
                                 [--weight-update-endpoint WEIGHT_UPDATE_ENDPOINT]
                                 [--start-date-endpoint START_DATE_ENDPOINT]
                                 [-s START] [-i UPDATE_INTERVAL] [-p PORT]
                                 [-v]

Query project usages from an openstack instance and provide it in a prometheus
compatible format. Alternatively develop in local mode and emulate machines
and projects.

optional arguments:
  -h, --help            show this help message and exit
  -d DUMMY_DATA, --dummy-data DUMMY_DATA
                        Use dummy values instead of connecting to an openstack
                        instance. Usage values are calculated based on the
                        configured existence. Toml files can be updated on the
                        fly as they are read every time a dummy-cloud function
                        is called (functions of nested classes excluded). Take
                        a look at the example file for an explanation
                        resources/dummy_cc.toml. Can also be provided via
                        environment variable $USAGE_EXPORTER_DUMMY_FILE
                        (default: None)
  -w DUMMY_WEIGHTS, --dummy-weights DUMMY_WEIGHTS
                        Use dummy weight endpoint instead of connecting to the
                        api. Take a look at the example file for an
                        explanation resources/dummy_weights.toml. Can also be
                        provided via environment variable
                        $USAGE_EXPORTER_DUMMY_WEIGHTS_FILE (default: None)
  --domain [DOMAIN [DOMAIN ...]]
                        Only export usages of projects belonging to one of the
                        given domains. Separate them via comma if passing via
                        environment variable $USAGE_EXPORTER_PROJECT_DOMAINS.
                        If no domains are specified all readable projects are
                        exported. (default: ['elixir'])
  --domain-id DOMAIN_ID
                        Only export usages of projects belonging to the domain
                        identified by the given ID. Takes precedence over any
                        specified domain and default values. Can also be set
                        via $USAGE_EXPORTER_PROJECT_DOMAIN_ID (default: )
  --simple-vm-id SIMPLE_VM_ID
                        The ID of the Openstack project, that hosts the
                        SimpleVm projects. Can also be set vis
                        $USAGE_EXPORTER_SIMPLE_VM_PROJECT_ID (default: )
  --simple-vm-tag SIMPLE_VM_TAG
                        The metadata of the Openstack project, that hosts the
                        SimpleVm projects. It is used to differentiate the
                        simple vm projects, default: project_name Can also be
                        set vis $USAGE_EXPORTER_SIMPLE_VM_PROJECT_TAG
                        (default: project_name)
  --weight-update-frequency WEIGHT_UPDATE_FREQUENCY
                        The frequency of checking if there is a weight update.
                        Is a multiple of the update interval length . Defaults
                        to the value of environment variable
                        $USAGE_EXPORTER_WEIGHT_UPDATE_FREQUENCY or 10
                        (default: 10)
  --weight-update-endpoint WEIGHT_UPDATE_ENDPOINT
                        The endpoint url where the current weights can be
                        updated . Defaults to the value of environment
                        variable $USAGE_EXPORTER_WEIGHTS_UPDATE_ENDPOINT or
                        will be left blank (default: )
  --start-date-endpoint START_DATE_ENDPOINT
                        The endpoint url where the start date can be
                        requested. If defined, requested date takes precedence
                        over all other start date arguments. Defaults to the
                        value of environment variable
                        $USAGE_EXPORTER_START_DATE_ENDPOINT or will be left
                        blank (default: )
  -s START, --start START
                        Beginning time of stats (YYYY-MM-DD). If set the value
                        of environment variable $USAGE_EXPORTER_START_DATE is
                        used. Uses maya for parsing. (default: 2021-07-20
                        18:04:41.399703)
  -i UPDATE_INTERVAL, --update-interval UPDATE_INTERVAL
                        Time to sleep between intervals, in case the calls
                        cause to much load on your openstack instance.
                        Defaults to the value of environment variable
                        $USAGE_EXPORTER_UPDATE_INTERVAL or 300 (in seconds)
                        (default: 30)
  -p PORT, --port PORT  Port to provide metrics on (default: 8080)
  -v, --verbose         Activate logging debug level (default: 0)

GNU AGPLv3 @ tluettje
```

## Development mode/Preview

If you simply want to take a look at the output, try any modification or do not have any
access to an OpenStack instance you can emulate running projects and machines with
simple `toml` files. A few profiles are available inside the `/resources` folder.

```shell
./project_usage_exporter.py \
 -d resources/dummy_cc.toml -w resources/dummy_weights.toml \
 --domain --simple-vm-id 123realsimplevm
```
or
```
docker run -e USAGE_EXPORTER_DUMMY_FILE=/code/resources/dummy_cc.toml \
           -e USAGE_EXPORTER_DUMMY_WEIGHTS_FILE=/code/resources/dummy_weigths.toml \
           -e USAGE_EXPORTER_PROJECT_DOMAINS= \
           -p 8080:8080 denbicloud/os_project_usage_exporter:latest
```
This will emulate a few projects with machines without any domain restrictions. The
`resources` folder is also available inside the docker container at `/code/resources`.

**Note**: If you want to fetch mb and vcpu weights from an active endpoint, you need to omit the 
`-w DUMMY_WEIGHTS, --dummy-weights DUMMY_WEIGHTS` argument or respectively the `USAGE_EXPORTER_DUMMY_WEIGHTS_FILE`
environment as providing a dummy weights file deactivates fetching weights from and active endpoint.
## Production Mode

Simply source your `admin-openrc.sh` before starting the exporter. Depending on the size
of your instance you might want test different values for `--update-interval` to
determine how much load (if any) is caused by the queries.

In case of docker you have to insert your password inside the `openrc` file, and remove
any lines other than `key=value` pairs. Surrounding quotes will be considered part of
the values therefore remove them as well.

```
docker run --env-file openrc -p 8080:8080 denbicloud/os_project_usage_exporter:latest
```
