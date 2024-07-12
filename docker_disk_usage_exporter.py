#!/usr/bin/env python3
# coding: utf-8
# pyright: reportMissingImports=false
# pyright: reportGeneralTypeIssues=false
# pyright: reportOptionalMemberAccess=false
# pyright: reportMissingModuleSource=false
# pyright: reportAttributeAccessIssue=false

"""Docker Disk Usage Exporter"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from typing import Callable
from wsgiref.simple_server import make_server

import docker
import pytz
from prometheus_client import PLATFORM_COLLECTOR, PROCESS_COLLECTOR
from prometheus_client.core import REGISTRY, CollectorRegistry, Metric
from prometheus_client.exposition import _bake_output, _SilentHandler, parse_qs

DOCKER_DISK_USAGE_EXPORTER_NAME = os.environ.get(
    "DOCKER_DISK_USAGE_EXPORTER_NAME", "docker-disk-usage-exporter"
)
DOCKER_DISK_USAGE_EXPORTER_LOGLEVEL = os.environ.get(
    "DOCKER_DISK_USAGE_EXPORTER_LOGLEVEL", "INFO"
).upper()
DOCKER_DISK_USAGE_EXPORTER_TZ = os.environ.get("TZ", "Europe/Paris")

def make_wsgi_app(
    registry: CollectorRegistry = REGISTRY, disable_compression: bool = False
) -> Callable:
    """Create a WSGI app which serves the metrics from a registry."""

    def prometheus_app(environ, start_response):
        # Prepare parameters
        accept_header = environ.get("HTTP_ACCEPT")
        accept_encoding_header = environ.get("HTTP_ACCEPT_ENCODING")
        params = parse_qs(environ.get("QUERY_STRING", ""))
        headers = [
            ("Server", ""),
            ("Cache-Control", "no-cache, no-store, must-revalidate, max-age=0"),
            ("Pragma", "no-cache"),
            ("Expires", "0"),
            ("X-Content-Type-Options", "nosniff"),
        ]
        if environ["PATH_INFO"] == "/":
            status = "301 Moved Permanently"
            headers.append(("Location", "/metrics"))
            output = b""
        elif environ["PATH_INFO"] == "/favicon.ico":
            status = "200 OK"
            output = b""
        elif environ["PATH_INFO"] == "/metrics":
            status, tmp_headers, output = _bake_output(
                registry,
                accept_header,
                accept_encoding_header,
                params,
                disable_compression,
            )
            headers += tmp_headers
        else:
            status = "404 Not Found"
            output = b""
        start_response(status, headers)
        return [output]

    return prometheus_app


def start_wsgi_server(
    port: int,
    addr: str = "0.0.0.0",  # nosec B104
    registry: CollectorRegistry = REGISTRY,
) -> None:
    """Starts a WSGI server for prometheus metrics as a daemon thread."""
    app = make_wsgi_app(registry)
    httpd = make_server(addr, port, app, handler_class=_SilentHandler)
    thread = threading.Thread(target=httpd.serve_forever)
    thread.daemon = True
    thread.start()


start_http_server = start_wsgi_server

# Logging Configuration
try:
    pytz.timezone(DOCKER_DISK_USAGE_EXPORTER_TZ)
    logging.Formatter.converter = lambda *args: datetime.now(
        tz=pytz.timezone(DOCKER_DISK_USAGE_EXPORTER_TZ)
    ).timetuple()
    logging.basicConfig(
        stream=sys.stdout,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%d/%m/%Y %H:%M:%S",
        level=DOCKER_DISK_USAGE_EXPORTER_LOGLEVEL,
    )
except pytz.exceptions.UnknownTimeZoneError:
    logging.Formatter.converter = lambda *args: datetime.now(
        tz=pytz.timezone("Europe/Paris")
    ).timetuple()
    logging.basicConfig(
        stream=sys.stdout,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%d/%m/%Y %H:%M:%S",
        level="INFO",
    )
    logging.error("TZ invalid : %s !", DOCKER_DISK_USAGE_EXPORTER_TZ)
    os._exit(1)
except ValueError:
    logging.basicConfig(
        stream=sys.stdout,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%d/%m/%Y %H:%M:%S",
        level="INFO",
    )
    logging.error("DOCKER_DISK_USAGE_EXPORTER_LOGLEVEL invalid !")
    os._exit(1)

# Check DOCKER_DISK_USAGE_EXPORTER_PORT
try:
    DOCKER_DISK_USAGE_EXPORTER_PORT = int(
        os.environ.get("DOCKER_DISK_USAGE_EXPORTER_PORT", "8123")
    )
except ValueError:
    logging.error("DOCKER_DISK_USAGE_EXPORTER_PORT must be int !")
    os._exit(1)

# Check DOCKER_DISK_USAGE_EXPORTER_CONTAINERS_FILTERS
try:
    DOCKER_DISK_USAGE_EXPORTER_CONTAINERS_FILTERS = json.loads(
        os.environ.get("DOCKER_DISK_USAGE_EXPORTER_CONTAINERS_FILTERS", "{}")
    )
except TypeError:
    DOCKER_DISK_USAGE_EXPORTER_CONTAINERS_FILTERS = "{}"
except json.decoder.JSONDecodeError:
    logging.error("DOCKER_DISK_USAGE_EXPORTER_CONTAINERS_FILTERS invalid !")
    os._exit(1)

METRICS = [
    {
        "name": "anonymous_volume_size",
        "description": "Docker Anonymous Volume Size in bytes",
        "type": "gauge",
    },
    {
        "name": "named_volume_size",
        "description": "Docker Named Volume Size in bytes",
        "type": "gauge",
    },
    {
        "name": "rootfs_size",
        "description": "Docker Container Root File System Size in bytes",
        "type": "gauge",
    },
    {
        "name": "rw_size",
        "description": "Docker Container Read Write File System Size in bytes",
        "type": "gauge",
    },
]

# REGISTRY Configuration
REGISTRY.unregister(PROCESS_COLLECTOR)
REGISTRY.unregister(PLATFORM_COLLECTOR)
REGISTRY.unregister(REGISTRY._names_to_collectors["python_gc_objects_collected_total"])


class DockerDiskUsageCollector:
    """Docker Disk Usage Collector Class"""

    def __init__(self):
        try:
            self.client = docker.from_env()
        except docker.errors.DockerException as exception:
            logging.error(str(exception))
            os._exit(1)

    def metric(self, index, system_df, container, mount):
        """Get Metric"""
        metric = {}
        labels = {"container": container.name, "job": DOCKER_DISK_USAGE_EXPORTER_NAME}
        metric["name"] = f"docker_disk_usage_{METRICS[index]['name']}"
        metric["type"] = METRICS[index]["type"]
        metric["description"] = METRICS[index]["description"]
        if index in [0, 1]:
            labels["volume"] = mount["Name"]
            metric["value"] = [
                i["UsageData"]["Size"]
                for i in system_df["Volumes"]
                if i["Name"] == mount["Name"]
            ][0]
        elif index == 2:
            metric["value"] = [
                i["SizeRootFs"]
                for i in system_df["Containers"]
                if i["Names"] == [f"/{container.name}"]
            ][0]
        elif index == 3:
            metric["value"] = [
                i["SizeRw"]
                for i in system_df["Containers"]
                if i["Names"] == [f"/{container.name}"]
            ][0]
        metric["labels"] = labels
        return metric

    def get_metrics(self):
        """Generate Prometheus Metrics"""
        containers = self.client.containers.list(
            filters=DOCKER_DISK_USAGE_EXPORTER_CONTAINERS_FILTERS
        )
        metrics = []
        system_df = self.client.df()
        for container in containers:
            for mount in container.attrs["Mounts"]:
                if mount["Type"] == "volume":
                    try:
                        if (
                            self.client.volumes.get(mount["Name"])
                            .attrs["Labels"]
                            .get("com.docker.volume.anonymous")
                            is None
                        ):
                            metrics.append(self.metric(1, system_df, container, mount))
                        else:
                            metrics.append(self.metric(0, system_df, container, mount))
                    except AttributeError:
                        metrics.append(self.metric(1, system_df, container, mount))
                    except docker.errors.NotFound:
                        continue
            try:
                metrics.append(self.metric(2, system_df, container, None))
            except IndexError:
                continue

            try:
                metrics.append(self.metric(3, system_df, container, None))
            except KeyError:
                continue

        metrics = [i for i in metrics if i["value"] != 0]
        logging.info(metrics)
        return metrics

    def collect(self):
        """Collect Prometheus Metrics"""
        metrics = self.get_metrics()
        for metric in metrics:
            prometheus_metric = Metric(
                metric["name"], metric["description"], metric["type"]
            )
            prometheus_metric.add_sample(
                metric["name"], value=metric["value"], labels=metric["labels"]
            )
            yield prometheus_metric


def main():
    """Main Function"""
    logging.info(
        "Starting Docker Disk Usage Exporter on port %s.",
        DOCKER_DISK_USAGE_EXPORTER_PORT,
    )
    logging.debug(
        "DOCKER_DISK_USAGE_EXPORTER_PORT: %s.", DOCKER_DISK_USAGE_EXPORTER_PORT
    )
    logging.debug(
        "DOCKER_DISK_USAGE_EXPORTER_NAME: %s.", DOCKER_DISK_USAGE_EXPORTER_NAME
    )
    # Start Prometheus HTTP Server
    start_http_server(DOCKER_DISK_USAGE_EXPORTER_PORT)
    # Init BinanceCollector
    REGISTRY.register(DockerDiskUsageCollector())
    # Infinite Loop
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
