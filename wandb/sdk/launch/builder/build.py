import json
import logging
import os
import shlex
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple


from dockerpycreds.utils import find_executable  # type: ignore
import pkg_resources
from six.moves import shlex_quote
import wandb
from wandb.apis.internal import Api
import wandb.docker as docker
from wandb.errors import DockerError, ExecutionError, LaunchError

from .._project_spec import compute_command_args, EntryPoint, LaunchProject
from ..utils import _is_wandb_dev_uri, _is_wandb_local_uri
from ...lib.git import GitRepo

_logger = logging.getLogger(__name__)

_GENERATED_DOCKERFILE_NAME = "Dockerfile.wandb-autogenerated"
DEFAULT_ENTRYPOINT = "_wandb_default_entrypoint.py"

DEFAULT_CUDA_VERSION = "10.0"


def validate_docker_installation() -> None:
    """Verify if Docker is installed on host machine."""
    if not find_executable("docker"):
        raise ExecutionError(
            "Could not find Docker executable. "
            "Ensure Docker is installed as per the instructions "
            "at https://docs.docker.com/install/overview/."
        )


def get_docker_user(launch_project: LaunchProject, runner_type: str) -> Tuple[str, int]:
    import getpass

    username = getpass.getuser()

    if runner_type == "sagemaker" and not launch_project.docker_image:
        # unless user has provided their own image, sagemaker must run as root but keep the name for workdir etc
        return username, 0

    userid = launch_project.docker_user_id or os.geteuid()
    return username, userid


DOCKERFILE_TEMPLATE = """
# ----- stage 1: build -----
FROM {py_build_image} as build

# requirements section depends on pip vs conda, and presence of buildx
{requirements_section}

# ----- stage 2: base -----
{base_setup}

COPY --from=build /env /env
ENV PATH="/env/bin:$PATH"

ENV SHELL /bin/bash

# some resources (eg sagemaker) must run on root
{user_setup}

WORKDIR {workdir}
RUN chown -R {uid} {workdir}

# make artifacts cache dir unrelated to build
RUN mkdir -p {workdir}/.cache && chown -R {uid} {workdir}/.cache

# copy code/etc
COPY --chown={uid} src/ {workdir}

ENV PYTHONUNBUFFERED=1

# some resources (eg sagemaker) have unique entrypoint requirements
{entrypoint_setup}
"""

# this goes into base_setup in TEMPLATE
PYTHON_SETUP_TEMPLATE = """
FROM {py_base_image} as base
"""

# this goes into base_setup in TEMPLATE
CUDA_SETUP_TEMPLATE = """
FROM {cuda_base_image} as base
# TODO: once NVIDIA their linux repository keys for all docker images
RUN apt-key adv --fetch-keys https://developer.download.nvidia.com/compute/cuda/repos/$(cat /etc/os-release | grep ^ID= |  cut -d "=" -f2 )$(cat /etc/os-release | grep ^VERSION_ID= |  cut -d "=" -f2 | sed -e 's/[\".]//g' )/$(uname -i)/3bf863cc.pub
RUN apt-key adv --fetch-keys https://developer.download.nvidia.com/compute/machine-learning/repos/$(cat /etc/os-release | grep ^ID= |  cut -d "=" -f2 )$(cat /etc/os-release | grep ^VERSION_ID= |  cut -d "=" -f2 | sed -e 's/[\".]//g' )/$(uname -i)/7fa2af80.pub
RUN apt-get update -qq && apt-get install -y software-properties-common && add-apt-repository -y ppa:deadsnakes/ppa

# install python
RUN apt-get update -qq && apt-get install --no-install-recommends -y \
    {python_packages} \
    && apt-get -qq purge && apt-get -qq clean \
    && rm -rf /var/lib/apt/lists/*

# make sure `python` points at the right version
RUN update-alternatives --install /usr/bin/python python /usr/bin/python{py_version} 1 \
    && update-alternatives --install /usr/local/bin/python python /usr/bin/python{py_version} 1
"""

# this goes into requirements_section in TEMPLATE
PIP_TEMPLATE = """
RUN python -m venv /env
# make sure we install into the env
ENV PATH="/env/bin:$PATH"
COPY {requirements_files} ./
{buildx_optional_prefix} {pip_install}
"""

# this goes into requirements_section in TEMPLATE
CONDA_TEMPLATE = """
COPY src/environment.yml .
{buildx_optional_prefix} conda env create -f environment.yml -n env

# pack the environment so that we can transfer to the base image
RUN conda install -c conda-forge conda-pack
RUN conda pack -n env -o /tmp/env.tar && \
    mkdir /env && cd /env && tar xf /tmp/env.tar && \
    rm /tmp/env.tar
RUN /env/bin/conda-unpack
"""

USER_CREATE_TEMPLATE = """
RUN useradd \
    --create-home \
    --no-log-init \
    --shell /bin/bash \
    --gid 0 \
    --uid {uid} \
    {user} || echo ""
"""

ENTRYPOINT_TEMPLATE = """
COPY ./src/{default_entrypoint} {workdir}
ENTRYPOINT ["python", "{default_entrypoint}", "{entrypoint}"]
"""

PYTHON_ENTRYPOINT = """
import subprocess
import os
import sys

def _run_entry_point(command: str):
    work_dir = os.getcwd()
    env = os.environ.copy()
    if os.name == "nt":
        # we are running on windows
        process = subprocess.Popen(
            ["cmd", "/c", command], close_fds=True, cwd=work_dir, env=env
        )
    else:
        process = subprocess.Popen(
            ["bash", "-c", command],
            close_fds=True,
            cwd=os.getcwd(),
            env=env,
        )
    return process.wait()


if __name__ == "__main__":
    print("CALLED MAIN FILE")
    print(sys.argv[:])
    cmd = ""
    if os.environ.get("WANDB_ENTRYPOINT_COMMAND"):
        cmd += os.environ.get("WANDB_ENTRYPOINT_COMMAND")
    else:
        cmd += sys.argv[1]

    if os.environ.get("WANDB_ARGS"):
        cmd += " " + os.environ.get("WANDB_ARGS")
    print("RUNNING", cmd)

    sys.exit(_run_entry_point(cmd))
"""


def get_current_python_version() -> Tuple[str, str]:
    full_version = sys.version.split()[0].split(".")
    major = full_version[0]
    version = ".".join(full_version[:2]) if len(full_version) >= 2 else major + ".0"
    return version, major


def get_base_setup(
    launch_project: LaunchProject, py_version: str, py_major: str
) -> str:
    """Fill in the Dockerfile templates for stage 2 of build. CPU version is built on python, GPU
    version is built on nvidia:cuda"""

    python_base_image = f"python:{py_version}-buster"
    if launch_project.cuda:
        cuda_version = launch_project.cuda_version or DEFAULT_CUDA_VERSION
        # cuda image doesn't come with python tooling
        if py_major == "2":
            python_packages = [
                f"python{py_version}",
                f"libpython{py_version}",
                "python-pip",
                "python-setuptools",
            ]
        else:
            python_packages = [
                f"python{py_version}",
                f"libpython{py_version}",
                "python3-pip",
                "python3-setuptools",
            ]
        base_setup = CUDA_SETUP_TEMPLATE.format(
            cuda_base_image=f"nvidia/cuda:{cuda_version}-runtime",
            python_packages=" \\\n".join(python_packages),
            py_version=py_version,
        )
    else:
        python_packages = [
            "python3-dev" if py_major == "3" else "python-dev",
            "gcc",
        ]  # gcc required for python < 3.7 for some reason
        base_setup = PYTHON_SETUP_TEMPLATE.format(py_base_image=python_base_image)
    return base_setup


def get_env_vars_dict(
    launch_project: LaunchProject, entry_point: Optional[EntryPoint], api: Api
) -> Dict[str, str]:
    """Generates environment variables for the project.

    Arguments:
    launch_project: LaunchProject to generate environment variables for.

    Returns:
        Dictionary of environment variables.
    """
    env_vars = {}
    env_vars["WANDB_BASE_URL"] = api.settings("base_url")
    env_vars["WANDB_API_KEY"] = api.api_key
    env_vars["WANDB_PROJECT"] = launch_project.target_project
    env_vars["WANDB_ENTITY"] = launch_project.target_entity
    env_vars["WANDB_LAUNCH"] = "True"
    env_vars["WANDB_RUN_ID"] = launch_project.run_id
    if launch_project.docker_image:
        env_vars["WANDB_DOCKER"] = launch_project.docker_image

    # TODO: handle env vars > 32760 characters
    env_vars["WANDB_CONFIG"] = json.dumps(launch_project.override_config)
    env_vars["WANDB_ARTIFACTS"] = json.dumps(launch_project.override_artifacts)
    #  check if the user provided an override entrypoint, otherwise use the default
    if launch_project.override_args.get("entry_point") and entry_point:
        env_vars["WANDB_ENTRYPOINT_COMMAND"] = join(entry_point.command)
    if launch_project.override_args:
        env_vars["WANDB_ARGS"] = " ".join(
            compute_command_args(launch_project.override_args)
        )
    return env_vars


def get_requirements_section(launch_project: LaunchProject, builder_type: str) -> str:
    if builder_type == "docker":
        buildx_installed = docker.is_buildx_installed()
        if not buildx_installed:
            wandb.termwarn(
                "Docker BuildX is not installed, for faster builds upgrade docker: https://github.com/docker/buildx#installing"
            )
            prefix = "RUN WANDB_DISABLE_CACHE=true"
    elif builder_type == "kaniko":
        prefix = "RUN WANDB_DISABLE_CACHE=true"
        buildx_installed = False
    if launch_project.deps_type == "pip":
        requirements_files = []
        if launch_project.project_dir is not None and os.path.exists(
            os.path.join(launch_project.project_dir, "requirements.txt")
        ):
            requirements_files += ["src/requirements.txt"]
            pip_install_line = "pip install -r requirements.txt"
        if launch_project.project_dir is not None and os.path.exists(
            os.path.join(launch_project.project_dir, "requirements.frozen.txt")
        ):
            # if we have frozen requirements stored, copy those over and have them take precedence
            requirements_files += ["src/requirements.frozen.txt", "_wandb_bootstrap.py"]
            pip_install_line = (
                _parse_existing_requirements(launch_project)
                + "python _wandb_bootstrap.py"
            )
        if buildx_installed:
            prefix = "RUN --mount=type=cache,mode=0777,target=/root/.cache/pip"

        requirements_line = PIP_TEMPLATE.format(
            buildx_optional_prefix=prefix,
            requirements_files=" ".join(requirements_files),
            pip_install=pip_install_line,
        )
    elif launch_project.deps_type == "conda":
        if buildx_installed:
            prefix = "RUN --mount=type=cache,mode=0777,target=/opt/conda/pkgs"
        requirements_line = CONDA_TEMPLATE.format(buildx_optional_prefix=prefix)
    else:
        # this means no deps file was found
        requirements_line = ""

    return requirements_line


def get_user_setup(username: str, userid: int, runner_type: str) -> str:
    if runner_type == "sagemaker":
        # sagemaker must run as root
        return "USER root"
    user_create = USER_CREATE_TEMPLATE.format(uid=userid, user=username)
    user_create += f"\nUSER {username}"
    return user_create


def get_entrypoint_setup(
    launch_project: LaunchProject,
    entry_point: EntryPoint,
    workdir: str,
) -> str:
    # if runner_type == "sagemaker":
    # this check will always pass, since this is only called in the build case where
    # the project_dir is set
    assert launch_project.project_dir is not None
    # sagemaker automatically appends train after the entrypoint
    # by redirecting to running a train script we can avoid issues
    # with argparse, and hopefully if the user intends for the train
    # argument to be present it is captured in the original jobs
    # command arguments
    with open(os.path.join(launch_project.project_dir, DEFAULT_ENTRYPOINT), "w") as fp:
        fp.write(PYTHON_ENTRYPOINT)
    return ENTRYPOINT_TEMPLATE.format(
        workdir=workdir,
        entrypoint=join(entry_point.command),
        default_entrypoint=DEFAULT_ENTRYPOINT,
    )


def generate_dockerfile(
    launch_project: LaunchProject,
    entry_point: EntryPoint,
    runner_type: str,
    builder_type: str,
) -> str:
    # get python versions truncated to major.minor to ensure image availability
    if launch_project.python_version:
        spl = launch_project.python_version.split(".")[:2]
        py_version, py_major = (".".join(spl), spl[0])
    else:
        py_version, py_major = get_current_python_version()

    # ----- stage 1: build -----
    if launch_project.deps_type == "pip" or launch_project.deps_type is None:
        python_build_image = "python:{}".format(
            py_version
        )  # use full python image for package installation
    elif launch_project.deps_type == "conda":
        # neither of these images are receiving regular updates, latest should be pretty stable
        python_build_image = (
            "continuumio/miniconda3:latest"
            if py_major == "3"
            else "continuumio/miniconda:latest"
        )
    requirements_section = get_requirements_section(launch_project, builder_type)

    # ----- stage 2: base -----
    python_base_setup = get_base_setup(launch_project, py_version, py_major)

    # set up user info
    username, userid = get_docker_user(launch_project, runner_type)
    user_setup = get_user_setup(username, userid, runner_type)
    workdir = f"/home/{username}"

    entrypoint_section = get_entrypoint_setup(launch_project, entry_point, workdir)

    dockerfile_contents = DOCKERFILE_TEMPLATE.format(
        py_build_image=python_build_image,
        requirements_section=requirements_section,
        base_setup=python_base_setup,
        uid=userid,
        user_setup=user_setup,
        workdir=workdir,
        entrypoint_setup=entrypoint_section,
    )
    return dockerfile_contents


_inspected_images = {}


def docker_image_exists(docker_image: str, should_raise: bool = False) -> bool:
    """Checks if a specific image is already available,
    optionally raising an exception"""
    _logger.info("Checking if base image exists...")
    try:
        data = docker.run(["docker", "image", "inspect", docker_image])
        # always true, since return stderr defaults to false
        assert isinstance(data, str)
        parsed = json.loads(data)[0]
        _inspected_images[docker_image] = parsed
        return True
    except (DockerError, ValueError) as e:
        if should_raise:
            raise e
        _logger.info("Base image not found. Generating new base image")
        return False


def docker_image_inspect(docker_image: str) -> Dict[str, Any]:
    """Get the parsed json result of docker inspect image_name"""
    if _inspected_images.get(docker_image) is None:
        docker_image_exists(docker_image, True)
    return _inspected_images.get(docker_image, {})


def pull_docker_image(docker_image: str) -> None:
    """Pulls the requested docker image"""
    if docker_image_exists(docker_image):
        # don't pull images if they exist already, eg if they are local images
        return
    try:
        docker.run(["docker", "pull", docker_image])
    except DockerError as e:
        raise LaunchError(f"Docker server returned error: {e}")


def construct_local_image_uri(launch_project: LaunchProject) -> str:
    assert launch_project.project_dir is not None
    image_uri = _get_docker_image_uri(
        name=launch_project.image_name,
        work_dir=launch_project.project_dir,
        image_id=launch_project.run_id,
    )
    return image_uri


def construct_gcp_image_uri(
    launch_project: LaunchProject,
    gcp_repo: str,
    gcp_project: str,
    gcp_registry: str,
) -> str:
    base_uri = construct_local_image_uri(launch_project)
    return "/".join([gcp_registry, gcp_project, gcp_repo, base_uri])


def construct_gcp_registry_uri(
    gcp_repo: str, gcp_project: str, gcp_registry: str
) -> str:
    return "/".join([gcp_registry, gcp_project, gcp_repo])


def _parse_existing_requirements(launch_project: LaunchProject) -> str:
    requirements_line = ""
    assert launch_project.project_dir is not None
    base_requirements = os.path.join(launch_project.project_dir, "requirements.txt")
    if os.path.exists(base_requirements):
        include_only = set()
        with open(base_requirements) as f:
            iter = pkg_resources.parse_requirements(f)
            while True:
                try:
                    pkg = next(iter)
                    if hasattr(pkg, "name"):
                        name = pkg.name.lower()  # type: ignore
                    else:
                        name = str(pkg)
                    include_only.add(shlex_quote(name))
                except StopIteration:
                    break
                # Different versions of pkg_resources throw different errors
                # just catch them all and ignore packages we can't parse
                except Exception as e:
                    _logger.warn(f"Unable to parse requirements.txt: {e}")
                    continue
        requirements_line += "WANDB_ONLY_INCLUDE={} ".format(",".join(include_only))
    return requirements_line


def _get_docker_image_uri(name: Optional[str], work_dir: str, image_id: str) -> str:
    """
    Returns an appropriate Docker image URI for a project based on the git hash of the specified
    working directory.
    :param name: The URI of the Docker repository with which to tag the image. The
                           repository URI is used as the prefix of the image URI.
    :param work_dir: Path to the working directory in which to search for a git commit hash
    """
    name = name.replace(" ", "-") if name else "wandb-launch"
    # Optionally include first 7 digits of git SHA in tag name, if available.

    git_commit = GitRepo(work_dir).last_commit
    version_string = (
        ":" + str(git_commit[:7]) + image_id if git_commit else ":" + image_id
    )
    return name + version_string


def _create_docker_build_ctx(
    launch_project: LaunchProject,
    dockerfile_contents: str,
) -> str:
    """Creates build context temp dir containing Dockerfile and project code, returning path to temp dir."""
    directory = tempfile.mkdtemp()
    dst_path = os.path.join(directory, "src")
    assert launch_project.project_dir is not None
    shutil.copytree(
        src=launch_project.project_dir,
        dst=dst_path,
        symlinks=True,
    )
    shutil.copy(
        os.path.join(os.path.dirname(__file__), "templates", "_wandb_bootstrap.py"),
        os.path.join(directory),
    )
    if launch_project.python_version:
        runtime_path = os.path.join(dst_path, "runtime.txt")
        with open(runtime_path, "w") as fp:
            fp.write(f"python-{launch_project.python_version}")
    # TODO: we likely don't need to pass the whole git repo into the container
    # with open(os.path.join(directory, ".dockerignore"), "w") as f:
    #    f.write("**/.git")
    with open(os.path.join(directory, _GENERATED_DOCKERFILE_NAME), "w") as handle:
        handle.write(dockerfile_contents)
    return directory


def join(split_command: List[str]) -> str:
    """Return a shell-escaped string from *split_command*."""
    return " ".join(shlex.quote(arg) for arg in split_command)
