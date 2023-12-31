import asyncio
import os
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Response, UploadFile
from fastapi.responses import FileResponse
from hypercorn.asyncio import serve
from hypercorn.config import Config
from prometheus_fastapi_instrumentator import Instrumentator

from .db import AgentDB
from .errors import NotFoundError
from .forge_log import CustomLogger
from .middlewares import AgentMiddleware
from .routes.agent_protocol import base_router
from .schema import *
from .tracing import setup_tracing
from .utils import run
from .workspace import Workspace

LOG = CustomLogger(__name__)


class Agent:
    def __init__(self, database: AgentDB, workspace: Workspace):
        self.db = database
        self.workspace = workspace

    def start(self, port: int = 8000, router: APIRouter = base_router):
        """
        Start the agent server.
        """
        config = Config()
        config.bind = [f"localhost:{port}"]
        app = FastAPI(
            title="Auto-GPT Forge",
            description="Modified version of The Agent Protocol.",
            version="v0.4",
        )

        # Add Prometheus metrics to the agent
        # https://github.com/trallnag/prometheus-fastapi-instrumentator
        instrumentator = Instrumentator().instrument(app)

        @app.on_event("startup")
        async def _startup():
            instrumentator.expose(app)

        app.include_router(router)
        app.add_middleware(AgentMiddleware, agent=self)
        setup_tracing(app)
        config.loglevel = "ERROR"
        config.bind = [f"0.0.0.0:{port}"]

        LOG.info(f"Agent server starting on http://{config.bind[0]}")
        asyncio.run(serve(app, config))

    async def create_task(self, task_request: TaskRequestBody) -> Task:
        """
        Create a task for the agent.
        """
        try:
            task = await self.db.create_task(
                input=task_request.input,
                additional_input=task_request.additional_input,
            )
            return task
        except Exception as e:
            raise

    async def list_tasks(self, page: int = 1, pageSize: int = 10) -> TaskListResponse:
        """
        List all tasks that the agent has created.
        """
        try:
            tasks, pagination = await self.db.list_tasks(page, pageSize)
            response = TaskListResponse(tasks=tasks, pagination=pagination)
            return response
        except Exception as e:
            raise

    async def get_task(self, task_id: str) -> Task:
        """
        Get a task by ID.
        """
        try:
            task = await self.db.get_task(task_id)
        except Exception as e:
            raise
        return task

    async def list_steps(
        self, task_id: str, page: int = 1, pageSize: int = 10
    ) -> TaskStepsListResponse:
        """
        List the IDs of all steps that the task has created.
        """
        try:
            steps, pagination = await self.db.list_steps(task_id, page, pageSize)
            response = TaskStepsListResponse(steps=steps, pagination=pagination)
            return response
        except Exception as e:
            raise

    async def create_and_execute_step(
        self, task_id: str, step_request: StepRequestBody
    ) -> Step:
        """
        Create a step for the task.
        """
        if step_request.input != "y":
            step = await self.db.create_step(
                task_id=task_id,
                input=step_request,
                additional_input=step_request.additional_input,
            )
            # utils.run
            artifacts = run(step.input)
            for artifact in artifacts:
                art = await self.db.create_artifact(
                    task_id=step.task_id,
                    file_name=artifact["file_name"],
                    uri=artifact["uri"],
                    agent_created=True,
                    step_id=step.step_id,
                )
                assert isinstance(
                    art, Artifact
                ), f"Artifact not instance of Artifact {type(art)}"
                step.artifacts.append(art)
            step.status = "completed"
        else:
            steps, steps_pagination = await self.db.list_steps(
                task_id, page=1, per_page=100
            )
            # Find the latest step that has not been completed
            step = next((s for s in reversed(steps) if s.status != "completed"), None)
            if step is None:
                # If all steps have been completed, create a new placeholder step
                step = await self.db.create_step(
                    task_id=task_id,
                    input="y",
                    additional_input={},
                )
                step.status = "completed"
                step.is_last = True
                step.output = "No more steps to run."
                step = await self.db.update_step(step)
        if isinstance(step.status, Status):
            step.status = step.status.value
        step.output = "Done some work"
        return step

    async def get_step(self, task_id: str, step_id: str) -> Step:
        """
        Get a step by ID.
        """
        try:
            step = await self.db.get_step(task_id, step_id)
            return step
        except Exception as e:
            raise

    async def list_artifacts(
        self, task_id: str, page: int = 1, pageSize: int = 10
    ) -> TaskArtifactsListResponse:
        """
        List the artifacts that the task has created.
        """
        try:
            artifacts, pagination = await self.db.list_artifacts(
                task_id, page, pageSize
            )
            response = TaskArtifactsListResponse(
                artifacts=artifacts, pagination=pagination
            )
            return Response(content=response.json(), media_type="application/json")
        except Exception as e:
            raise

    async def create_artifact(
        self, task_id: str, file: UploadFile, relative_path: str
    ) -> Artifact:
        """
        Create an artifact for the task.
        """
        data = None
        file_name = file.filename or str(uuid4())
        try:
            data = b""
            while contents := file.file.read(1024 * 1024):
                data += contents
            # Check if relative path ends with filename
            if relative_path.endswith(file_name):
                file_path = relative_path
            else:
                file_path = os.path.join(relative_path, file_name)

            self.workspace.write(task_id, file_path, data)

            artifact = await self.db.create_artifact(
                task_id=task_id,
                file_name=file_name,
                relative_path=relative_path,
                agent_created=False,
            )
        except Exception as e:
            raise
        return artifact

    async def get_artifact(self, task_id: str, artifact_id: str) -> Artifact:
        """
        Get an artifact by ID.
        """
        try:
            artifact = await self.db.get_artifact(artifact_id)
            file_path = os.path.join(artifact.relative_path, artifact.file_name)
            retrieved_artifact = self.workspace.read(task_id=task_id, path=file_path)
            path = artifact.file_name
            with open(path, "wb") as f:
                f.write(retrieved_artifact)
        except NotFoundError as e:
            raise
        except FileNotFoundError as e:
            raise
        except Exception as e:
            raise
        return FileResponse(
            # Note: mimetype is guessed in the FileResponse constructor
            path=path,
            filename=artifact.file_name,
        )
