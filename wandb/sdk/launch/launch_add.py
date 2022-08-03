import json
import pprint
from typing import Any, Dict, List, Optional, Union

import wandb
from wandb.apis.internal import Api
import wandb.apis.public as public
from wandb.sdk.launch.utils import (
    construct_launch_spec,
    validate_launch_spec_source,
)
from wandb.sdk.launch._project_spec import (
    create_project_from_spec,
    build_image_from_project,
)
from wandb.sdk.data_types._dtypes import TypeRegistry


def push_to_queue(api: Api, queue: str, launch_spec: Dict[str, Any]) -> Any:
    try:
        res = api.push_to_run_queue(queue, launch_spec)
    except Exception as e:
        print("Exception:", e)
        return None
    return res


def launch_add(
    uri: Optional[str] = None,
    job: Optional[str] = None,
    config: Optional[Union[str, Dict[str, Any]]] = None,
    project: Optional[str] = None,
    entity: Optional[str] = None,
    queue: Optional[str] = None,
    resource: Optional[str] = None,
    entry_point: Optional[List[str]] = None,
    name: Optional[str] = None,
    version: Optional[str] = None,
    docker_image: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    resource_args: Optional[Dict[str, Any]] = None,
    cuda: Optional[bool] = None,
    run_id: Optional[str] = None,
    build: Optional[bool] = False,
) -> "public.QueuedRun":
    """Enqueue a W&B launch experiment. With either a source uri, job or docker_image.

    Arguments:
    uri: URI of experiment to run. A wandb run uri or a Git repository URI.
    job: string reference to a wandb.Job eg: wandb/test/my-job:latest
    config: A dictionary containing the configuration for the run. May also contain
        resource specific arguments under the key "resource_args"
    project: Target project to send launched run to
    entity: Target entity to send launched run to
    queue: the name of the queue to enqueue the run to
    resource: Execution backend for the run: W&B provides built-in support for "local" backend
    entry_point: Entry point to run within the project. Defaults to using the entry point used
        in the original run for wandb URIs, or main.py for git repository URIs.
    name: Name run under which to launch the run.
    version: For Git-based projects, either a commit hash or a branch name.
    docker_image: The name of the docker image to use for the run.
    params: Parameters (dictionary) for the entry point command. Defaults to using the
        the parameters used to run the original run.
    resource_args: Resource related arguments for launching runs onto a remote backend.
        Will be stored on the constructed launch config under ``resource_args``.
    cuda: Whether to build a CUDA-enabled docker image or not
    run_id: optional string indicating the id of the launched run
    build: optional flag defaulting to false, requires queue to be set
        if build, an image is created, creates a job artifact, pushes a reference
            to that job artifact to queue


    Example:
        import wandb
        project_uri = "https://github.com/wandb/examples"
        params = {"alpha": 0.5, "l1_ratio": 0.01}
        # Run W&B project and create a reproducible docker environment
        # on a local host
        api = wandb.apis.internal.Api()
        wandb.launch_add(uri=project_uri, parameters=params)


    Returns:
        an instance of`wandb.api.public.QueuedRun` which gives information about the
        queued run, or if `wait_until_started` or `wait_until_finished` are called, gives access
        to the underlying Run information.

    Raises:
        `wandb.exceptions.LaunchError` if unsuccessful
    """
    api = Api()

    return _launch_add(
        api,
        uri,
        job,
        config,
        project,
        entity,
        queue,
        resource,
        entry_point,
        name,
        version,
        docker_image,
        params,
        resource_args,
        cuda,
        run_id=run_id,
    )


def _launch_add(
    api: Api,
    uri: Optional[str],
    job: Optional[str],
    config: Optional[Union[str, Dict[str, Any]]],
    project: Optional[str],
    entity: Optional[str],
    queue: Optional[str],
    resource: Optional[str],
    entry_point: Optional[List[str]],
    name: Optional[str],
    version: Optional[str],
    docker_image: Optional[str],
    params: Optional[Dict[str, Any]],
    resource_args: Optional[Dict[str, Any]] = None,
    cuda: Optional[bool] = None,
    run_id: Optional[str] = None,
    build: Optional[bool] = False,
) -> "public.QueuedRun":

    resource = resource or "local"
    if config is not None:
        if isinstance(config, str):
            with open(config) as fp:
                launch_config = json.load(fp)
        elif isinstance(config, dict):
            launch_config = config
    else:
        launch_config = {}

    if queue is None:
        queue = "default"

    launch_spec = construct_launch_spec(
        uri,
        job,
        api,
        name,
        project,
        entity,
        docker_image,
        resource,
        entry_point,
        version,
        params,
        resource_args,
        launch_config,
        cuda,
        run_id,
    )

    if build:
        if launch_spec.get("job") is not None:
            wandb.termwarn("Build doesn't support setting a job. Overwriting job.")
            launch_spec["job"] = None

        launch_project = create_project_from_spec(launch_spec, api)
        docker_image_uri = build_image_from_project(launch_project)

        # Remove passed in URI, using job artifact abstraction instead
        launch_spec["uri"] = None
        JOB_BUILD = "launch_build"  # constant, TODO: #2 find better home

        if wandb.run is not None:  # can this ever be true?
            run = wandb.run
        else:
            run = wandb.init(project=project, job_type=JOB_BUILD)

        _id = docker_image_uri.split(":")[-1]
        name = f"{launch_spec.get('entity')}-{launch_spec.get('project')}-{_id}"

        # TODO: #3 @Kyle about this whole block!
        input_types = TypeRegistry.type_of(dict).to_json()
        output_types = TypeRegistry.type_of(dict).to_json()
        python_runtime = None
        installed_packages_list = []

        source_info = {
            "_version": "v0",
            "source_type": "image",
            "source": {"image": docker_image_uri},
            "input_types": input_types,
            "output_types": output_types,
            "runtime": python_runtime,
        }
        job_artifact = run._construct_job_artifact(
            name=name,
            source_dict=source_info,
            installed_packages_list=installed_packages_list,
        )
        run.log_artifact(job_artifact)

        job_name = job_artifact.wait().name
        launch_spec["job"] = job_name
        job = job_name

    validate_launch_spec_source(launch_spec)
    res = push_to_queue(api, queue, launch_spec)

    if res is None or "runQueueItemId" not in res:
        raise Exception("Error adding run to queue")
    wandb.termlog(f"Added run to queue {queue}.")
    wandb.termlog(f"Launch spec:\n{pprint.pformat(launch_spec)}\n")
    public_api = public.Api()
    queued_run_entity = launch_spec.get("entity")
    queued_run_project = launch_spec.get("project")

    container_job = False
    if job:
        job_artifact = public_api.job(job)
        if job_artifact._source_info.get("source_type") == "image":
            container_job = True

    queued_run = public_api.queued_run(
        queued_run_entity,
        queued_run_project,
        queue,
        res["runQueueItemId"],
        container_job,
    )

    return queued_run  # type: ignore
