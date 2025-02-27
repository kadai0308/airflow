# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Various utils to prepare docker and docker compose commands."""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from subprocess import DEVNULL, CalledProcessError, CompletedProcess
from typing import TYPE_CHECKING

from airflow_breeze.params.build_prod_params import BuildProdParams
from airflow_breeze.utils.host_info_utils import get_host_os
from airflow_breeze.utils.image import find_available_ci_image
from airflow_breeze.utils.path_utils import AIRFLOW_SOURCES_ROOT, GENERATED_DOCKER_ENV_FILE

try:
    from packaging import version
except ImportError:
    # We handle the ImportError so that autocomplete works with just click installed
    version = None  # type: ignore[assignment]

from airflow_breeze.global_constants import (
    ALLOWED_CELERY_BROKERS,
    ALLOWED_DEBIAN_VERSIONS,
    APACHE_AIRFLOW_GITHUB_REPOSITORY,
    DEFAULT_PYTHON_MAJOR_MINOR_VERSION,
    MIN_DOCKER_COMPOSE_VERSION,
    MIN_DOCKER_VERSION,
    MOUNT_ALL,
    MOUNT_REMOVE,
    MOUNT_SELECTED,
)
from airflow_breeze.utils.console import Output, get_console
from airflow_breeze.utils.run_utils import (
    RunCommandResult,
    check_if_buildx_plugin_installed,
    run_command,
)

if TYPE_CHECKING:
    from airflow_breeze.params.common_build_params import CommonBuildParams

# Those are volumes that are mounted when MOUNT_SELECTED is chosen (which is the default when
# entering Breeze. MOUNT_SELECTED prevents to mount the files that you can have accidentally added
# in your sources (or they were added automatically by setup.py etc.) to be mounted to container.
# This is important to get a "clean" environment for different python versions and to avoid
# unnecessary slow-downs when you are mounting files on MacOS (which has very slow filesystem)
# Any time you add a top-level folder in airflow that should also be added to container you should
# add it here.
VOLUMES_FOR_SELECTED_MOUNTS = [
    (".bash_aliases", "/root/.bash_aliases"),
    (".bash_history", "/root/.bash_history"),
    (".build", "/opt/airflow/.build"),
    (".dockerignore", "/opt/airflow/.dockerignore"),
    (".github", "/opt/airflow/.github"),
    (".inputrc", "/root/.inputrc"),
    (".rat-excludes", "/opt/airflow/.rat-excludes"),
    ("BREEZE.rst", "/opt/airflow/BREEZE.rst"),
    ("LICENSE", "/opt/airflow/LICENSE"),
    ("MANIFEST.in", "/opt/airflow/MANIFEST.in"),
    ("NOTICE", "/opt/airflow/NOTICE"),
    ("RELEASE_NOTES.rst", "/opt/airflow/RELEASE_NOTES.rst"),
    ("airflow", "/opt/airflow/airflow"),
    ("constraints", "/opt/airflow/constraints"),
    ("dags", "/opt/airflow/dags"),
    ("dev", "/opt/airflow/dev"),
    ("docs", "/opt/airflow/docs"),
    ("generated", "/opt/airflow/generated"),
    ("hooks", "/opt/airflow/hooks"),
    ("images", "/opt/airflow/images"),
    ("logs", "/root/airflow/logs"),
    ("pyproject.toml", "/opt/airflow/pyproject.toml"),
    ("scripts", "/opt/airflow/scripts"),
    ("scripts/docker/entrypoint_ci.sh", "/entrypoint"),
    ("setup.cfg", "/opt/airflow/setup.cfg"),
    ("setup.py", "/opt/airflow/setup.py"),
    ("tests", "/opt/airflow/tests"),
    ("helm_tests", "/opt/airflow/helm_tests"),
    ("kubernetes_tests", "/opt/airflow/kubernetes_tests"),
    ("docker_tests", "/opt/airflow/docker_tests"),
    ("chart", "/opt/airflow/chart"),
]


def get_extra_docker_flags(mount_sources: str, include_mypy_volume: bool = False) -> list[str]:
    """
    Returns extra docker flags based on the type of mounting we want to do for sources.

    :param mount_sources: type of mounting we want to have
    :param env: environment variables to pass to docker
    :param include_mypy_volume: includes mypy_volume
    :return: extra flag as list of strings
    """
    extra_docker_flags = []
    if mount_sources == MOUNT_ALL:
        extra_docker_flags.extend(["--mount", f"type=bind,src={AIRFLOW_SOURCES_ROOT},dst=/opt/airflow/"])
    elif mount_sources == MOUNT_SELECTED:
        for src, dst in VOLUMES_FOR_SELECTED_MOUNTS:
            if (AIRFLOW_SOURCES_ROOT / src).exists():
                extra_docker_flags.extend(
                    ["--mount", f"type=bind,src={AIRFLOW_SOURCES_ROOT / src},dst={dst}"]
                )
        if include_mypy_volume:
            extra_docker_flags.extend(
                ["--mount", "type=volume,src=mypy-cache-volume,dst=/opt/airflow/.mypy_cache"]
            )
    elif mount_sources == MOUNT_REMOVE:
        extra_docker_flags.extend(
            ["--mount", f"type=bind,src={AIRFLOW_SOURCES_ROOT / 'empty'},dst=/opt/airflow/airflow"]
        )
    extra_docker_flags.extend(["--mount", f"type=bind,src={AIRFLOW_SOURCES_ROOT / 'files'},dst=/files"])
    extra_docker_flags.extend(["--mount", f"type=bind,src={AIRFLOW_SOURCES_ROOT / 'dist'},dst=/dist"])
    extra_docker_flags.extend(["--rm"])
    extra_docker_flags.extend(["--env-file", GENERATED_DOCKER_ENV_FILE.as_posix()])
    return extra_docker_flags


def check_docker_resources(airflow_image_name: str) -> RunCommandResult:
    """
    Check if we have enough resources to run docker. This is done via running script embedded in our image.


    :param airflow_image_name: name of the airflow image to use
    """
    return run_command(
        cmd=[
            "docker",
            "run",
            "-t",
            "--entrypoint",
            "/bin/bash",
            "-e",
            "PYTHONDONTWRITEBYTECODE=true",
            airflow_image_name,
            "-c",
            "python /opt/airflow/scripts/in_container/run_resource_check.py",
        ],
        text=True,
    )


def check_docker_permission_denied() -> bool:
    """
    Checks if we have permission to write to docker socket. By default, on Linux you need to add your user
    to docker group and some new users do not realize that. We help those users if we have
    permission to run docker commands.


    :return: True if permission is denied
    """
    permission_denied = False
    docker_permission_command = ["docker", "info"]
    command_result = run_command(
        docker_permission_command,
        no_output_dump_on_exception=True,
        capture_output=True,
        text=True,
        check=False,
    )
    if command_result.returncode != 0:
        permission_denied = True
        if command_result.stdout and "Got permission denied while trying to connect" in command_result.stdout:
            get_console().print(
                "ERROR: You have `permission denied` error when trying to communicate with docker."
            )
            get_console().print(
                "Most likely you need to add your user to `docker` group: \
                https://docs.docker.com/ engine/install/linux-postinstall/ ."
            )
    return permission_denied


def compare_version(current_version: str, min_version: str) -> bool:
    return version.parse(current_version) >= version.parse(min_version)


def check_docker_is_running():
    """
    Checks if docker is running. Suppressed Dockers stdout and stderr output.

    """
    response = run_command(
        ["docker", "info"],
        no_output_dump_on_exception=True,
        text=False,
        capture_output=True,
        check=False,
    )
    if response.returncode != 0:
        get_console().print(
            "[error]Docker is not running.[/]\n"
            "[warning]Please make sure Docker is installed and running.[/]"
        )
        sys.exit(1)


def check_docker_version():
    """
    Checks if the docker compose version is as expected. including some specific modifications done by
    some vendors such as Microsoft. They might have modified version of docker-compose/docker in their
    cloud. In case docker compose version is wrong we continue but print warning for the user.

    """
    permission_denied = check_docker_permission_denied()
    if not permission_denied:
        docker_version_command = ["docker", "version", "--format", "{{.Client.Version}}"]
        docker_version = ""
        docker_version_result = run_command(
            docker_version_command,
            no_output_dump_on_exception=True,
            capture_output=True,
            text=True,
            check=False,
            dry_run_override=False,
        )
        if docker_version_result.returncode == 0:
            docker_version = docker_version_result.stdout.strip()
        if docker_version == "":
            get_console().print(
                f"""
[warning]Your version of docker is unknown. If the scripts fail, please make sure to[/]
[warning]install docker at least: {MIN_DOCKER_VERSION} version.[/]
"""
            )
            sys.exit(1)
        else:
            good_version = compare_version(docker_version, MIN_DOCKER_VERSION)
            if good_version:
                get_console().print(f"[success]Good version of Docker: {docker_version}.[/]")
            else:
                get_console().print(
                    f"""
[error]Your version of docker is too old: {docker_version}.\n[/]
[warning]Please upgrade to at least {MIN_DOCKER_VERSION}.\n[/]
You can find installation instructions here: https://docs.docker.com/engine/install/
"""
                )
                sys.exit(1)


def check_remote_ghcr_io_commands():
    """Checks if you have permissions to pull an empty image from ghcr.io.

    Unfortunately, GitHub packages treat expired login as "no-access" even on
    public repos. We need to detect that situation and suggest user to log-out
    or if they are in CI environment to re-push their PR/close or reopen the PR.
    """
    response = run_command(
        ["docker", "pull", "ghcr.io/apache/airflow-hello-world"],
        no_output_dump_on_exception=True,
        text=False,
        capture_output=True,
        check=False,
    )
    if response.returncode != 0:
        if "no such host" in response.stderr.decode("utf-8"):
            get_console().print(
                "[error]\nYou seem to be offline. This command requires access to network.[/]\n"
            )
            sys.exit(2)
        get_console().print("[error]Response:[/]\n")
        get_console().print(response.stdout.decode("utf-8"))
        get_console().print(response.stderr.decode("utf-8"))
        if os.environ.get("CI"):
            get_console().print(
                "\n[error]We are extremely sorry but you've hit the rare case that the "
                "credentials you got from GitHub Actions to run are expired, and we cannot do much.[/]"
                "\n¯\\_(ツ)_/¯\n\n"
                "[warning]You have the following options now:\n\n"
                "  * Close and reopen the Pull Request of yours\n"
                "  * Rebase or amend your commit and push your branch again\n"
                "  * Ask in the PR to re-run the failed job\n\n"
            )
            sys.exit(1)
        else:
            get_console().print(
                "[error]\nYou seem to have expired permissions on ghcr.io.[/]\n"
                "[warning]Please logout. Run this command:[/]\n\n"
                "   docker logout ghcr.io\n\n"
            )
            sys.exit(1)


def check_docker_compose_version():
    """Checks if the docker compose version is as expected.

    This includes specific modifications done by some vendors such as Microsoft.
    They might have modified version of docker-compose/docker in their cloud. In
    the case the docker compose version is wrong, we continue but print a
    warning for the user.
    """
    version_pattern = re.compile(r"(\d+)\.(\d+)\.(\d+)")
    docker_compose_version_command = ["docker", "compose", "version"]
    try:
        docker_compose_version_result = run_command(
            docker_compose_version_command,
            no_output_dump_on_exception=True,
            capture_output=True,
            text=True,
            dry_run_override=False,
        )
    except Exception:
        get_console().print(
            "[error]You either do not have docker-composer or have docker-compose v1 installed.[/]\n"
            "[warning]Breeze does not support docker-compose v1 any more as it has been replaced by v2.[/]\n"
            "Follow https://docs.docker.com/compose/migrate/ to migrate to v2"
        )
        sys.exit(1)
    if docker_compose_version_result.returncode == 0:
        docker_compose_version = docker_compose_version_result.stdout
        version_extracted = version_pattern.search(docker_compose_version)
        if version_extracted is not None:
            docker_compose_version = ".".join(version_extracted.groups())
            good_version = compare_version(docker_compose_version, MIN_DOCKER_COMPOSE_VERSION)
            if good_version:
                get_console().print(f"[success]Good version of docker-compose: {docker_compose_version}[/]")
            else:
                get_console().print(
                    f"""
[error]You have too old version of docker-compose: {docker_compose_version}!\n[/]
[warning]At least {MIN_DOCKER_COMPOSE_VERSION} needed! Please upgrade!\n[/]
See https://docs.docker.com/compose/install/ for installation instructions.\n
Make sure docker-compose you install is first on the PATH variable of yours.\n
"""
                )
                sys.exit(1)
    else:
        get_console().print(
            f"""
[error]Unknown docker-compose version.[/]
[warning]At least {MIN_DOCKER_COMPOSE_VERSION} needed! Please upgrade!\n[/]
See https://docs.docker.com/compose/install/ for installation instructions.\n
Make sure docker-compose you install is first on the PATH variable of yours.\n
"""
        )
        sys.exit(1)


def prepare_docker_build_cache_command(
    image_params: CommonBuildParams,
) -> list[str]:
    """
    Constructs docker build_cache command based on the parameters passed.
    :param image_params: parameters of the image


    :return: Command to run as list of string
    """
    final_command = []
    final_command.extend(["docker"])
    final_command.extend(
        ["buildx", "build", "--builder", get_and_use_docker_context(image_params.builder), "--progress=auto"]
    )
    final_command.extend(image_params.common_docker_build_flags)
    final_command.extend(["--pull"])
    final_command.extend(image_params.prepare_arguments_for_docker_build_command())
    final_command.extend(["--target", "main", "."])
    final_command.extend(
        ["-f", "Dockerfile" if isinstance(image_params, BuildProdParams) else "Dockerfile.ci"]
    )
    final_command.extend(["--platform", image_params.platform])
    final_command.extend(
        [f"--cache-to=type=registry,ref={image_params.get_cache(image_params.platform)},mode=max"]
    )
    return final_command


def prepare_base_build_command(image_params: CommonBuildParams) -> list[str]:
    """
    Prepare build command for docker build. Depending on whether we have buildx plugin installed or not,
    and whether we run cache preparation, there might be different results:

    * if buildx plugin is installed - `docker buildx` command is returned - using regular or cache builder
      depending on whether we build regular image or cache
    * if no buildx plugin is installed, and we do not prepare cache, regular docker `build` command is used.
    * if no buildx plugin is installed, and we prepare cache - we fail. Cache can only be done with buildx
    :param image_params: parameters of the image

    :return: command to use as docker build command
    """
    build_command_param = []
    is_buildx_available = check_if_buildx_plugin_installed()
    if is_buildx_available:
        build_command_param.extend(
            [
                "buildx",
                "build",
                "--builder",
                get_and_use_docker_context(image_params.builder),
                "--push" if image_params.push else "--load",
            ]
        )
    else:
        build_command_param.append("build")
    return build_command_param


def prepare_docker_build_command(
    image_params: CommonBuildParams,
) -> list[str]:
    """
    Constructs docker build command based on the parameters passed.
    :param image_params: parameters of the image

    :return: Command to run as list of string
    """
    build_command = prepare_base_build_command(
        image_params=image_params,
    )
    final_command = []
    final_command.extend(["docker"])
    final_command.extend(build_command)
    final_command.extend(image_params.common_docker_build_flags)
    final_command.extend(["--pull"])
    final_command.extend(image_params.prepare_arguments_for_docker_build_command())
    final_command.extend(["-t", image_params.airflow_image_name_with_tag, "--target", "main", "."])
    final_command.extend(
        ["-f", "Dockerfile" if isinstance(image_params, BuildProdParams) else "Dockerfile.ci"]
    )
    final_command.extend(["--platform", image_params.platform])
    return final_command


def construct_docker_push_command(
    image_params: CommonBuildParams,
) -> list[str]:
    """
    Constructs docker push command based on the parameters passed.
    :param image_params: parameters of the image
    :return: Command to run as list of string
    """
    return ["docker", "push", image_params.airflow_image_name_with_tag]


def build_cache(image_params: CommonBuildParams, output: Output | None) -> RunCommandResult:
    build_command_result: CompletedProcess | CalledProcessError = CompletedProcess(args=[], returncode=0)
    for platform in image_params.platforms:
        platform_image_params = copy.deepcopy(image_params)
        # override the platform in the copied params to only be single platform per run
        # as a workaround to https://github.com/docker/buildx/issues/1044
        platform_image_params.platform = platform
        cmd = prepare_docker_build_cache_command(image_params=platform_image_params)
        build_command_result = run_command(
            cmd,
            cwd=AIRFLOW_SOURCES_ROOT,
            output=output,
            check=False,
            text=True,
        )
        if build_command_result.returncode != 0:
            break
    return build_command_result


def make_sure_builder_configured(params: CommonBuildParams):
    if params.builder != "autodetect":
        cmd = ["docker", "buildx", "inspect", params.builder]
        buildx_command_result = run_command(cmd, text=True, check=False, dry_run_override=False)
        if buildx_command_result and buildx_command_result.returncode != 0:
            next_cmd = ["docker", "buildx", "create", "--name", params.builder]
            run_command(next_cmd, text=True, check=False, dry_run_override=False)


def set_value_to_default_if_not_set(env: dict[str, str], name: str, default: str):
    """Set value of name parameter to default (indexed by name) if not set.

    :param env: dictionary where to set the parameter
    :param name: name of parameter
    :param default: default value
    """
    if env.get(name) is None:
        env[name] = os.environ.get(name, default)


def prepare_broker_url(params, env_variables):
    """Prepare broker url for celery executor"""
    urls = env_variables["CELERY_BROKER_URLS"].split(",")
    url_map = {
        ALLOWED_CELERY_BROKERS[0]: urls[0],
        ALLOWED_CELERY_BROKERS[1]: urls[1],
    }
    if getattr(params, "celery_broker", None) and params.celery_broker in params.celery_broker in url_map:
        env_variables["AIRFLOW__CELERY__BROKER_URL"] = url_map[params.celery_broker]


def perform_environment_checks():
    check_docker_is_running()
    check_docker_version()
    check_docker_compose_version()


def get_docker_syntax_version() -> str:
    from airflow_breeze.utils.path_utils import AIRFLOW_SOURCES_ROOT

    return (AIRFLOW_SOURCES_ROOT / "Dockerfile").read_text().splitlines()[0]


def warm_up_docker_builder(image_params: CommonBuildParams):
    from airflow_breeze.utils.path_utils import AIRFLOW_SOURCES_ROOT

    docker_context = get_and_use_docker_context(image_params.builder)
    if docker_context == "default":
        return
    docker_syntax = get_docker_syntax_version()
    get_console().print(f"[info]Warming up the {docker_context} builder for syntax: {docker_syntax}")
    warm_up_image_param = copy.deepcopy(image_params)
    warm_up_image_param.image_tag = "warmup"
    warm_up_image_param.push = False
    build_command = prepare_base_build_command(image_params=warm_up_image_param)
    warm_up_command = []
    warm_up_command.extend(["docker"])
    warm_up_command.extend(build_command)
    warm_up_command.extend(["--platform", image_params.platform, "-"])
    warm_up_command_result = run_command(
        warm_up_command,
        input=f"""{docker_syntax}
FROM scratch
LABEL description="test warmup image"
""",
        cwd=AIRFLOW_SOURCES_ROOT,
        text=True,
        check=False,
    )
    if warm_up_command_result.returncode != 0:
        get_console().print(
            f"[warning]Warning {warm_up_command_result.returncode} when warming up builder:"
            f" {warm_up_command_result.stdout} {warm_up_command_result.stderr}"
        )


OWNERSHIP_CLEANUP_DOCKER_TAG = (
    f"python:{DEFAULT_PYTHON_MAJOR_MINOR_VERSION}-slim-{ALLOWED_DEBIAN_VERSIONS[0]}"
)


def fix_ownership_using_docker():
    if get_host_os() != "linux":
        # no need to even attempt fixing ownership on MacOS/Windows
        return
    shell_params = find_available_ci_image(
        github_repository=APACHE_AIRFLOW_GITHUB_REPOSITORY,
    )
    extra_docker_flags = get_extra_docker_flags(mount_sources=MOUNT_ALL)
    cmd = [
        "docker",
        "run",
        "-t",
        *extra_docker_flags,
        OWNERSHIP_CLEANUP_DOCKER_TAG,
        "/opt/airflow/scripts/in_container/run_fix_ownership.py",
    ]
    run_command(cmd, text=True, check=False, env=shell_params.env_variables_for_docker_commands)


def remove_docker_networks(networks: list[str] | None = None) -> None:
    """
    Removes specified docker networks. If no networks are specified, it removes all unused networks.
    Errors are ignored (not even printed in the output), so you can safely call it without checking
    if the networks exist.

    :param networks: list of networks to remove
    """
    if networks is None:
        run_command(
            ["docker", "network", "prune", "-f"],
            check=False,
            stderr=DEVNULL,
        )
    else:
        for network in networks:
            run_command(
                ["docker", "network", "rm", network],
                check=False,
                stderr=DEVNULL,
            )


# When you are using Docker Desktop (specifically on MacOS). the preferred context is "desktop-linux"
# because the docker socket to use is created in the .docker/ directory in the user's home directory
# and it does not require the user to belong to the "docker" group.
# The "default" context is the traditional one that requires "/var/run/docker.sock" to be writeable by the
# user running the docker command.
PREFERRED_CONTEXTS = ["desktop-linux", "default"]


def autodetect_docker_context():
    """
    Auto-detects which docker context to use.

    :return: name of the docker context to use
    """
    result = run_command(
        ["docker", "context", "ls", "--format=json"],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        get_console().print("[warning]Could not detect docker builder. Using default.[/]")
        return "default"
    try:
        context_dicts = json.loads(result.stdout)
        if isinstance(context_dicts, dict):
            context_dicts = [context_dicts]
    except json.decoder.JSONDecodeError:
        context_dicts = (json.loads(line) for line in result.stdout.splitlines() if line.strip())
    known_contexts = {info["Name"]: info for info in context_dicts}
    if not known_contexts:
        get_console().print("[warning]Could not detect docker builder. Using default.[/]")
        return "default"
    for preferred_context_name in PREFERRED_CONTEXTS:
        try:
            context = known_contexts[preferred_context_name]
        except KeyError:
            continue
        # On Windows, some contexts are used for WSL2. We don't want to use those.
        if context["DockerEndpoint"] == "npipe:////./pipe/dockerDesktopLinuxEngine":
            continue
        get_console().print(f"[info]Using {preferred_context_name} as context.[/]")
        return preferred_context_name
    fallback_context = next(iter(known_contexts))
    get_console().print(
        f"[warning]Could not use any of the preferred docker contexts {PREFERRED_CONTEXTS}.\n"
        f"Using {fallback_context} as context.[/]"
    )
    return fallback_context


def get_and_use_docker_context(context: str):
    if context == "autodetect":
        context = autodetect_docker_context()
    output = run_command(["docker", "context", "use", context], check=False)
    if output.returncode != 0:
        get_console().print(
            f"[warning] Could no use the context {context}. Continuing with current context[/]"
        )
    return context
