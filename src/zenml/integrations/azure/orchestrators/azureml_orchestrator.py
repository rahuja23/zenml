#  Copyright (c) ZenML GmbH 2024. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.
"""Implementation of the AzureML Orchestrator."""

import json
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Type, cast

from azure.ai.ml import Input, MLClient, Output
from azure.ai.ml.constants import TimeZone
from azure.ai.ml.dsl import pipeline
from azure.ai.ml.entities import (
    CommandComponent,
    CronTrigger,
    Environment,
    JobSchedule,
    RecurrencePattern,
    RecurrenceTrigger,
)
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from azure.identity import DefaultAzureCredential

from zenml.config.base_settings import BaseSettings
from zenml.config.step_configurations import Step
from zenml.enums import StackComponentType
from zenml.integrations.azure.flavors.azureml_orchestrator_flavor import (
    AzureMLComputeTypes,
    AzureMLOrchestratorConfig,
    AzureMLOrchestratorSettings,
)
from zenml.integrations.azure.orchestrators.azureml_orchestrator_entrypoint_config import (
    AzureMLEntrypointConfiguration,
)
from zenml.logger import get_logger
from zenml.orchestrators import ContainerizedOrchestrator
from zenml.orchestrators.utils import get_orchestrator_run_name
from zenml.stack import StackValidator
from zenml.utils.string_utils import b64_encode

if TYPE_CHECKING:
    from zenml.models import PipelineDeploymentResponse
    from zenml.stack import Stack

logger = get_logger(__name__)

ENV_ZENML_AZUREML_RUN_ID = "AZUREML_ROOT_RUN_ID"


class AzureMLOrchestrator(ContainerizedOrchestrator):
    """Orchestrator responsible for running pipelines on AzureML."""

    @property
    def config(self) -> AzureMLOrchestratorConfig:
        """Returns the `AzureMLOrchestratorConfig` config.

        Returns:
            The configuration.
        """
        return cast(AzureMLOrchestratorConfig, self._config)

    @property
    def settings_class(self) -> Optional[Type["BaseSettings"]]:
        """Settings class for the AzureML orchestrator.

        Returns:
            The settings class.
        """
        return AzureMLOrchestratorSettings

    @property
    def validator(self) -> Optional[StackValidator]:
        """Validates the stack.

        In the remote case, checks that the stack contains a container registry,
        image builder and only remote components.

        Returns:
            A `StackValidator` instance.
        """

        def _validate_remote_components(
            stack: "Stack",
        ) -> Tuple[bool, str]:
            for component in stack.components.values():
                if not component.config.is_local:
                    continue

                return False, (
                    f"The AzureML orchestrator runs pipelines remotely, "
                    f"but the '{component.name}' {component.type.value} is "
                    "a local stack component and will not be available in "
                    "the AzureML step.\nPlease ensure that you always "
                    "use non-local stack components with the AzureML "
                    "orchestrator."
                )

            return True, ""

        return StackValidator(
            required_components={
                StackComponentType.CONTAINER_REGISTRY,
                StackComponentType.IMAGE_BUILDER,
            },
            custom_validation_function=_validate_remote_components,
        )

    def get_orchestrator_run_id(self) -> str:
        """Returns the run id of the active orchestrator run.

        Important: This needs to be a unique ID and return the same value for
        all steps of a pipeline run.

        Returns:
            The orchestrator run id.

        Raises:
            RuntimeError: If the run id cannot be read from the environment.
        """
        try:
            return os.environ[ENV_ZENML_AZUREML_RUN_ID]
        except KeyError:
            raise RuntimeError(
                "Unable to read run id from environment variable "
                f"{ENV_ZENML_AZUREML_RUN_ID}."
            )

    @staticmethod
    def _create_command_component(
        step: Step,
        step_name: str,
        image: str,
        command: List[str],
        arguments: List[str],
    ) -> CommandComponent:
        """Creates a CommandComponent to run on AzureML Pipelines.

        Args:
            step: The step definition in ZenML.
            step_name: The name of the step.
            image: The image to use in the environment
            command: The command to execute the entrypoint with.
            arguments: The arguments to pass into the command.

        Returns:
            the generated AzureML CommandComponent.
        """
        env = Environment(image=image)

        outputs = {"completed": Output(type="uri_file")}

        inputs = {}
        if step.spec.upstream_steps:
            inputs = {
                f"{upstream_step}": Input(type="uri_file")
                for upstream_step in step.spec.upstream_steps
            }

        return CommandComponent(
            name=step_name,
            display_name=step_name,
            description=f"AzureML CommandComponent for {step_name}.",
            inputs=inputs,
            outputs=outputs,
            environment=env,
            command=" ".join(command + arguments),
        )

    def _create_or_get_compute(
        self, client: MLClient, settings: AzureMLOrchestratorSettings
    ) -> Optional[str]:
        """Creates or fetches the compute target if defined in the settings.

        Args:
            client: the AzureML client.
            settings: the settings for the orchestrator.

        Returns:
            None, if the orchestrator is using serverless compute or
            str, the name of the compute target (instance or cluster).
        """
        # If the mode is serverless, we can not fetch anything anyhow
        if settings.mode == AzureMLComputeTypes.SERVERLESS:
            return None

        # If a name is not provided, generate one based on the orchestrator id
        compute_name = settings.compute_name or f"compute_{self.id}"

        # Try to fetch the compute target
        try:
            client.compute.get(compute_name)
            logger.info(f"Using existing compute target: '{compute_name}'.")

            # TODO: We need to start the compute again if it is stopped.
            # TODO: We need to check whether extra parameters are set and
            #   throw a warning.
            # TODO: Add the experiment name
            # TODO: Remove the compute target
            return compute_name

        # If the compute target does not exist create it
        except ResourceNotFoundError:
            logger.info(
                "Can not find the compute target with name: "
                f"'{compute_name}':"
            )

            if settings.mode == AzureMLComputeTypes.COMPUTE_INSTANCE:
                logger.info(
                    "Creating a new compute instance. This might take a "
                    "few minutes."
                )

                from azure.ai.ml.entities import ComputeInstance

                compute_instance = ComputeInstance(
                    name=compute_name,
                    size=settings.compute_size,
                    idle_time_before_shutdown_minutes=settings.idle_type_before_shutdown_minutes,
                )
                client.begin_create_or_update(compute_instance).result()
                return compute_name

            elif settings.mode == AzureMLComputeTypes.COMPUTE_CLUSTER:
                logger.info(
                    "Creating a new compute cluster. This might take a "
                    "few minutes."
                )

                from azure.ai.ml.entities import AmlCompute

                compute_cluster = AmlCompute(
                    name=compute_name,
                    size=settings.compute_size,
                    location=settings.location,
                    min_instances=settings.min_instances,
                    max_instances=settings.max_instances,
                    idle_time_before_scale_down=settings.idle_type_before_shutdown_minutes,
                    tier=settings.tier,
                )
                client.begin_create_or_update(compute_cluster).result()
                return compute_name

        return None

    def prepare_or_run_pipeline(
        self,
        deployment: "PipelineDeploymentResponse",
        stack: "Stack",
        environment: Dict[str, str],
    ) -> None:
        """Prepares or runs a pipeline on AzureML.

        Args:
            deployment: The deployment to prepare or run.
            stack: The stack to run on.
            environment: Environment variables to set in the orchestration
                environment.
        """
        # Authentication
        if connector := self.get_connector():
            credentials = connector.connect()
        else:
            credentials = DefaultAzureCredential()

        # Settings
        settings = cast(
            AzureMLOrchestratorSettings,
            self.get_settings(deployment),
        )

        # Client creation
        ml_client = MLClient(
            credential=credentials,
            subscription_id=self.config.subscription_id,
            resource_group_name=self.config.resource_group,
            workspace_name=self.config.workspace,
        )

        # Create components
        components = {}
        for step_name, step in deployment.step_configurations.items():
            # Get the image for each step
            image = self.get_image(deployment=deployment, step_name=step_name)

            # Get the command and arguments
            command = AzureMLEntrypointConfiguration.get_entrypoint_command()
            arguments = (
                AzureMLEntrypointConfiguration.get_entrypoint_arguments(
                    step_name=step_name,
                    deployment_id=deployment.id,
                    zenml_env_variables=b64_encode(json.dumps(environment)),
                )
            )

            # Generate an AzureML CommandComponent
            components[step_name] = self._create_command_component(
                step=step,
                step_name=step_name,
                image=image,
                command=command,
                arguments=arguments,
            )

        # Pipeline definition
        pipeline_args = dict()
        run_name = get_orchestrator_run_name(
            pipeline_name=deployment.pipeline_configuration.name
        )
        pipeline_args["name"] = run_name

        if compute_target := self._create_or_get_compute(ml_client, settings):
            pipeline_args["compute"] = compute_target

        @pipeline(**pipeline_args)
        def azureml_pipeline() -> None:
            """Create an AzureML pipeline."""
            # Here we have to track the inputs and outputs so that we can bind
            # the components to each other to execute them in a specific order.
            component_outputs: Dict[str, Any] = {}
            for component_name, component in components.items():
                # Inputs
                component_inputs = {}
                if component.inputs:
                    component_inputs.update(
                        {i: component_outputs[i] for i in component.inputs}
                    )

                # Job
                component_job = component(**component_inputs)

                # Outputs
                if component_job.outputs:
                    component_outputs[component_name] = (
                        component_job.outputs.completed
                    )

        # Create and execute the pipeline job
        pipeline_job = azureml_pipeline()

        if settings.mode == AzureMLComputeTypes.SERVERLESS:
            pipeline_job.settings.default_compute = "serverless"

        # Scheduling
        if schedule := deployment.schedule:
            try:
                schedule_trigger = None

                if schedule.cron_expression:
                    # If we are working with a cron expression
                    schedule_trigger = CronTrigger(
                        expression=schedule.cron_expression,
                        start_time=schedule.start_time.isoformat(),
                        end_time=schedule.end_time.isoformat(),
                        time_zone=TimeZone.UTC,
                    )

                elif schedule.interval_second:
                    # If we are working with intervals
                    interval = schedule.interval_second.total_seconds()

                    if interval < 3600:
                        frequency = "minute"
                        interval = int(interval // 60)
                    elif interval < 86400:
                        frequency = "hour"
                        interval = int(interval // 3600)
                    else:
                        frequency = "day"
                        interval = int(interval // 86400)

                    recurrence_pattern = None
                    if frequency == "day":
                        recurrence_pattern = RecurrencePattern(
                            hours=schedule.start_time.hour,
                            minutes=schedule.start_time.minute,
                        )

                    schedule_trigger = RecurrenceTrigger(
                        frequency=frequency,
                        interval=interval,
                        schedule=recurrence_pattern,
                        start_time=schedule.start_time.isoformat(),
                        end_time=schedule.end_time.isoformat(),
                        time_zone=TimeZone.UTC,
                    )

                if schedule_trigger:
                    # Create and execute the job schedule
                    job_schedule = JobSchedule(
                        name=run_name,
                        trigger=schedule_trigger,
                        create_job=pipeline_job,
                    )
                    ml_client.schedules.begin_create_or_update(
                        job_schedule
                    ).result()
                    logger.info(
                        f"Scheduled pipeline '{run_name}' with recurrence "
                        f"or cron expression."
                    )
                else:
                    logger.warning(
                        f"No valid scheduling configuration found for pipeline '{run_name}'."
                    )

            except (HttpResponseError, ResourceExistsError) as e:
                logger.error(
                    f"Failed to create schedule for the pipeline '{run_name}': {str(e)}"
                )

        else:
            logger.info(f"Pipeline {run_name} has been executed.")
            ml_client.jobs.create_or_update(pipeline_job)
