import logging
from dataclasses import dataclass
import os
import pprint
import queue
import socket
import threading
import time
from typing import Any, List, Optional

import wandb
from wandb import wandb_lib  # type: ignore
from wandb.errors import SweepError
from wandb.wandb_agent import Agent as LegacySweepAgent

from .scheduler import Scheduler, SchedulerState, SimpleRunState, SweepRun

logger = logging.getLogger(__name__)


@dataclass
class HeartbeatAgent:
    agent: dict
    id: str
    thread: threading.Thread


class SweepScheduler(Scheduler):
    """A SweepScheduler is a controller/agent that will populate a Launch RunQueue with
    launch jobs it creates from run suggestions it pulls from an internal sweeps RunQueue.
    """

    def __init__(
        self,
        *args: Any,
        sweep_id: Optional[str] = None,
        heartbeat_thread_sleep: int = 3,
        heartbeat_queue_timeout: int = 3,
        main_thread_sleep: int = 3,
        num_workers: int = 1,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        # Make sure the provided sweep_id corresponds to a valid sweep
        found = self._api.sweep(
            sweep_id, "{}", entity=self._entity, project=self._project
        )
        if not found:
            raise SweepError(
                f"Could not find sweep {self._entity}/{self._project}/{sweep_id}"
            )
        self._sweep_id = sweep_id
        # TODO(hupo) validate sleeps integers here, throw errors if needed
        self._heartbeat_thread_sleep: int = heartbeat_thread_sleep
        self._heartbeat_queue_timeout: int = heartbeat_queue_timeout
        self._main_thread_sleep: int = main_thread_sleep
        self._num_workers: int = num_workers

    def _start(self) -> None:
        # Thread will pop items off the Sweeps RunQueue using AgentHeartbeat
        # and put them in this internal queue, which will be used to populate
        # the Launch RunQueue
        self._heartbeat_queue: "queue.Queue[SweepRun]" = queue.Queue()
        # Emulation of N agents in a classic sweeps setup
        self._heartbeat_agents: List[HeartbeatAgent] = []
        for worker_idx in range(self._num_workers):
            _msg = f"Starting AgentHeartbeat worker {worker_idx}\n"
            logger.debug(_msg)
            _agent = self._api.register_agent(
                f"{socket.gethostname()}-{worker_idx}",  # host
                sweep_id=self._sweep_id,
                project_name=self._project,
                entity=self._entity,
            )
            _thread = threading.Thread(target=self._heartbeat)
            _thread.daemon = True
            self._heartbeat_agents.append(
                HeartbeatAgent(
                    agent=_agent,
                    id=_agent["id"],
                    thread=_thread,
                )
            )
            _thread.start()

    def _heartbeat(self, worker_idx: int) -> None:
        while True:
            if not self.is_alive():
                return
            # AgentHeartbeat wants dict of runs which are running or queued
            _run_states = {}
            for run_id, run in self._runs.items():
                if run.state == SimpleRunState.ALIVE:
                    _run_states[run_id] = True
            _msg = f"AgentHeartbeat sending: \n{pprint.pformat(_run_states)}\n"
            logger.debug(_msg)
            commands = self._api.agent_heartbeat(
                self._heartbeat_agents[worker_idx].id, {}, _run_states
            )
            if commands:
                _msg = f"AgentHeartbeat received {len(commands)} commands: \n{pprint.pformat(commands)}\n"
                logger.debug(_msg)
                for command in commands:
                    _type = command.get("type")
                    # type can be one of "run", "resume", "stop", "exit"
                    if _type == "exit":
                        self.state = SchedulerState.COMPLETED
                        self.exit()
                        return
                    run = SweepRun(
                        id=command.get("run_id"),
                        args=command.get("args"),
                        logs=command.get("logs"),
                        program=command.get("program"),
                    )
                    self._runs[run.id] = run
                    if _type in ["run", "resume"]:
                        self._heartbeat_queue.put(run)
                    elif _type == "stop":
                        self._stop_run(run.id)
                        continue
            time.sleep(self._heartbeat_thread_sleep)

    def _run(self) -> None:
        try:
            run: SweepRun = self._heartbeat_queue.get(
                timeout=self._heartbeat_queue_timeout
            )
        except queue.Empty:
            _msg = "No jobs in Sweeps RunQueue, waiting..."
            logger.debug(_msg)
            wandb.termlog(_msg)
            time.sleep(self._main_thread_sleep)
            return
        # If run is already stopped just ignore the request
        if run.state in [
            SimpleRunState.DEAD,
            SimpleRunState.UNKNOWN,
        ]:
            return
        _msg = f"Converting Sweep Run (RunID:{run.id}) to Launch Job"
        logger.debug(_msg)
        wandb.termlog(_msg)
        # This is actually what populates the wandb config
        # since it is used in wandb.init()
        sweep_param_path = os.path.join(
            os.environ.get(wandb.env.DIR, os.getcwd()),
            "wandb",
            f"sweep-{self._sweep_id}",
            f"config-{run.id}.yaml",
        )
        wandb.termlog(f"Saving params to {sweep_param_path}")
        wandb_lib.config_util.save_config_file_from_dict(sweep_param_path, run.args)
        # Construct entry point using legacy sweeps utilities
        command_args = LegacySweepAgent._create_command_args({"args": run.args})["args"]
        entry_point = ["python", run.program] + command_args
        _ = self._add_to_launch_queue(
            # uri=os.environ.get(wandb.env.DIR, os.getcwd()),
            resource="local-process",
            entry_point=entry_point,
            run_id=run.id,
            params=run.args,
        )

    def _exit(self) -> None:
        self.state = SchedulerState.COMPLETED
