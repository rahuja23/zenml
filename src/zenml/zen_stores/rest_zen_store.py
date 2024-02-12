#  Copyright (c) ZenML GmbH 2022. All Rights Reserved.
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
"""REST Zen Store implementation."""
import os
import re
from pathlib import Path, PurePath
from typing import (
    Any,
    ClassVar,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from urllib.parse import urlparse
from uuid import UUID

import requests
import urllib3
from pydantic import BaseModel, root_validator, validator
from requests.adapters import HTTPAdapter, Retry

import zenml
from zenml.analytics import source_context
from zenml.config.global_config import GlobalConfiguration
from zenml.config.store_config import StoreConfiguration
from zenml.constants import (
    API,
    API_KEY_ROTATE,
    API_KEYS,
    API_TOKEN,
    ARTIFACT_VERSIONS,
    ARTIFACT_VISUALIZATIONS,
    ARTIFACTS,
    CODE_REFERENCES,
    CODE_REPOSITORIES,
    CURRENT_USER,
    DEFAULT_HTTP_TIMEOUT,
    DEVICES,
    DISABLE_CLIENT_SERVER_MISMATCH_WARNING,
    ENV_ZENML_DISABLE_CLIENT_SERVER_MISMATCH_WARNING,
    EVENT_SOURCES,
    FLAVORS,
    GET_OR_CREATE,
    INFO,
    LOGIN,
    LOGS,
    MODEL_VERSION_ARTIFACTS,
    MODEL_VERSION_PIPELINE_RUNS,
    MODEL_VERSIONS,
    MODELS,
    PIPELINE_BUILDS,
    PIPELINE_DEPLOYMENTS,
    PIPELINES,
    RUN_METADATA,
    RUNS,
    SCHEDULES,
    SECRETS,
    SECRETS_BACKUP,
    SECRETS_OPERATIONS,
    SECRETS_RESTORE,
    SERVICE_ACCOUNTS,
    SERVICE_CONNECTOR_CLIENT,
    SERVICE_CONNECTOR_RESOURCES,
    SERVICE_CONNECTOR_TYPES,
    SERVICE_CONNECTOR_VERIFY,
    SERVICE_CONNECTORS,
    STACK_COMPONENTS,
    STACKS,
    STEPS,
    TAGS,
    TRIGGER_EXECUTIONS,
    TRIGGERS,
    USERS,
    VERSION_1,
    WORKSPACES,
)
from zenml.enums import (
    OAuthGrantTypes,
    StoreType,
)
from zenml.exceptions import (
    AuthorizationException,
)
from zenml.io import fileio
from zenml.logger import get_logger
from zenml.models import (
    APIKeyFilter,
    APIKeyRequest,
    APIKeyResponse,
    APIKeyRotateRequest,
    APIKeyUpdate,
    ArtifactFilter,
    ArtifactRequest,
    ArtifactResponse,
    ArtifactUpdate,
    ArtifactVersionFilter,
    ArtifactVersionRequest,
    ArtifactVersionResponse,
    ArtifactVersionUpdate,
    ArtifactVisualizationResponse,
    BaseFilter,
    BaseRequest,
    BaseResponse,
    CodeReferenceResponse,
    CodeRepositoryFilter,
    CodeRepositoryRequest,
    CodeRepositoryResponse,
    CodeRepositoryUpdate,
    ComponentFilter,
    ComponentRequest,
    ComponentResponse,
    ComponentUpdate,
    EventSourceFilter,
    EventSourceRequest,
    EventSourceResponse,
    EventSourceUpdate,
    FlavorFilter,
    FlavorRequest,
    FlavorResponse,
    FlavorUpdate,
    LogsResponse,
    ModelFilter,
    ModelRequest,
    ModelResponse,
    ModelUpdate,
    ModelVersionArtifactFilter,
    ModelVersionArtifactRequest,
    ModelVersionArtifactResponse,
    ModelVersionFilter,
    ModelVersionPipelineRunFilter,
    ModelVersionPipelineRunRequest,
    ModelVersionPipelineRunResponse,
    ModelVersionRequest,
    ModelVersionResponse,
    ModelVersionUpdate,
    OAuthDeviceFilter,
    OAuthDeviceResponse,
    OAuthDeviceUpdate,
    Page,
    PipelineBuildFilter,
    PipelineBuildRequest,
    PipelineBuildResponse,
    PipelineDeploymentFilter,
    PipelineDeploymentRequest,
    PipelineDeploymentResponse,
    PipelineFilter,
    PipelineRequest,
    PipelineResponse,
    PipelineRunFilter,
    PipelineRunRequest,
    PipelineRunResponse,
    PipelineRunUpdate,
    PipelineUpdate,
    RunMetadataFilter,
    RunMetadataRequest,
    RunMetadataResponse,
    ScheduleFilter,
    ScheduleRequest,
    ScheduleResponse,
    ScheduleUpdate,
    SecretFilter,
    SecretRequest,
    SecretResponse,
    SecretUpdate,
    ServerModel,
    ServiceAccountFilter,
    ServiceAccountRequest,
    ServiceAccountResponse,
    ServiceAccountUpdate,
    ServiceConnectorFilter,
    ServiceConnectorRequest,
    ServiceConnectorResourcesModel,
    ServiceConnectorResponse,
    ServiceConnectorTypeModel,
    ServiceConnectorUpdate,
    StackFilter,
    StackRequest,
    StackResponse,
    StackUpdate,
    StepRunFilter,
    StepRunRequest,
    StepRunResponse,
    StepRunUpdate,
    TagFilter,
    TagRequest,
    TagResponse,
    TagUpdate,
    TriggerExecutionFilter,
    TriggerExecutionResponse,
    TriggerFilter,
    TriggerRequest,
    TriggerResponse,
    TriggerUpdate,
    UserFilter,
    UserRequest,
    UserResponse,
    UserUpdate,
    WorkspaceFilter,
    WorkspaceRequest,
    WorkspaceResponse,
    WorkspaceScopedRequest,
    WorkspaceUpdate,
)
from zenml.service_connectors.service_connector_registry import (
    service_connector_registry,
)
from zenml.utils.networking_utils import (
    replace_localhost_with_internal_hostname,
)
from zenml.zen_server.exceptions import exception_from_response
from zenml.zen_stores.base_zen_store import BaseZenStore

logger = get_logger(__name__)

# type alias for possible json payloads (the Anys are recursive Json instances)
Json = Union[Dict[str, Any], List[Any], str, int, float, bool, None]


AnyRequest = TypeVar("AnyRequest", bound=BaseRequest)
AnyResponse = TypeVar("AnyResponse", bound=BaseResponse)  # type: ignore[type-arg]
AnyWorkspaceScopedRequest = TypeVar(
    "AnyWorkspaceScopedRequest",
    bound=WorkspaceScopedRequest,
)


class RestZenStoreConfiguration(StoreConfiguration):
    """REST ZenML store configuration.

    Attributes:
        type: The type of the store.
        username: The username to use to connect to the Zen server.
        password: The password to use to connect to the Zen server.
        api_key: The service account API key to use to connect to the Zen
            server.
        api_token: The API token to use to connect to the Zen server. Generated
            by the client and stored in the configuration file on the first
            login and every time the API key is refreshed.
        verify_ssl: Either a boolean, in which case it controls whether we
            verify the server's TLS certificate, or a string, in which case it
            must be a path to a CA bundle to use or the CA bundle value itself.
        http_timeout: The timeout to use for all requests.

    """

    type: StoreType = StoreType.REST

    username: Optional[str] = None
    password: Optional[str] = None
    api_key: Optional[str] = None
    api_token: Optional[str] = None
    verify_ssl: Union[bool, str] = True
    http_timeout: int = DEFAULT_HTTP_TIMEOUT

    @root_validator
    def validate_credentials(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        """Validates the credentials provided in the values dictionary.

        Args:
            values: A dictionary containing the values to be validated.

        Raises:
            ValueError: If neither api_token nor username nor api_key is set.

        Returns:
            The values dictionary.
        """
        # Check if the values dictionary contains either an API token, an API
        # key or a username as non-empty strings.
        if (
            values.get("api_token")
            or values.get("username")
            or values.get("api_key")
        ):
            return values
        raise ValueError(
            "Neither api_token nor username nor api_key is set in the "
            "store config."
        )

    @validator("url")
    def validate_url(cls, url: str) -> str:
        """Validates that the URL is a well-formed REST store URL.

        Args:
            url: The URL to be validated.

        Returns:
            The validated URL without trailing slashes.

        Raises:
            ValueError: If the URL is not a well-formed REST store URL.
        """
        url = url.rstrip("/")
        scheme = re.search("^([a-z0-9]+://)", url)
        if scheme is None or scheme.group() not in ("https://", "http://"):
            raise ValueError(
                "Invalid URL for REST store: {url}. Should be in the form "
                "https://hostname[:port] or http://hostname[:port]."
            )

        # When running inside a container, if the URL uses localhost, the
        # target service will not be available. We try to replace localhost
        # with one of the special Docker or K3D internal hostnames.
        url = replace_localhost_with_internal_hostname(url)

        return url

    @validator("verify_ssl")
    def validate_verify_ssl(
        cls, verify_ssl: Union[bool, str]
    ) -> Union[bool, str]:
        """Validates that the verify_ssl either points to a file or is a bool.

        Args:
            verify_ssl: The verify_ssl value to be validated.

        Returns:
            The validated verify_ssl value.
        """
        secret_folder = Path(
            GlobalConfiguration().local_stores_path,
            "certificates",
        )
        if isinstance(verify_ssl, bool) or verify_ssl.startswith(
            str(secret_folder)
        ):
            return verify_ssl

        if os.path.isfile(verify_ssl):
            with open(verify_ssl, "r") as f:
                verify_ssl = f.read()

        fileio.makedirs(str(secret_folder))
        file_path = Path(secret_folder, "ca_bundle.pem")
        with open(file_path, "w") as f:
            f.write(verify_ssl)
        file_path.chmod(0o600)
        verify_ssl = str(file_path)

        return verify_ssl

    @classmethod
    def supports_url_scheme(cls, url: str) -> bool:
        """Check if a URL scheme is supported by this store.

        Args:
            url: The URL to check.

        Returns:
            True if the URL scheme is supported, False otherwise.
        """
        return urlparse(url).scheme in ("http", "https")

    def expand_certificates(self) -> None:
        """Expands the certificates in the verify_ssl field."""
        # Load the certificate values back into the configuration
        if isinstance(self.verify_ssl, str) and os.path.isfile(
            self.verify_ssl
        ):
            with open(self.verify_ssl, "r") as f:
                self.verify_ssl = f.read()

    @classmethod
    def copy_configuration(
        cls,
        config: "StoreConfiguration",
        config_path: str,
        load_config_path: Optional[PurePath] = None,
    ) -> "StoreConfiguration":
        """Create a copy of the store config using a different path.

        This method is used to create a copy of the store configuration that can
        be loaded using a different configuration path or in the context of a
        new environment, such as a container image.

        The configuration files accompanying the store configuration are also
        copied to the new configuration path (e.g. certificates etc.).

        Args:
            config: The store configuration to copy.
            config_path: new path where the configuration copy will be loaded
                from.
            load_config_path: absolute path that will be used to load the copied
                configuration. This can be set to a value different from
                `config_path` if the configuration copy will be loaded from
                a different environment, e.g. when the configuration is copied
                to a container image and loaded using a different absolute path.
                This will be reflected in the paths and URLs encoded in the
                copied configuration.

        Returns:
            A new store configuration object that reflects the new configuration
            path.
        """
        assert isinstance(config, RestZenStoreConfiguration)
        assert config.api_token is not None or config.api_key is not None
        config = config.copy(exclude={"username", "password"}, deep=True)
        # Load the certificate values back into the configuration
        config.expand_certificates()
        return config

    class Config:
        """Pydantic configuration class."""

        # Don't validate attributes when assigning them. This is necessary
        # because the `verify_ssl` attribute can be expanded to the contents
        # of the certificate file.
        validate_assignment = False
        # Forbid extra attributes set in the class.
        extra = "forbid"


class RestZenStore(BaseZenStore):
    """Store implementation for accessing data from a REST API."""

    config: RestZenStoreConfiguration
    TYPE: ClassVar[StoreType] = StoreType.REST
    CONFIG_TYPE: ClassVar[Type[StoreConfiguration]] = RestZenStoreConfiguration
    _api_token: Optional[str] = None
    _session: Optional[requests.Session] = None

    # ====================================
    # ZenML Store interface implementation
    # ====================================

    # --------------------------------
    # Initialization and configuration
    # --------------------------------

    def _initialize(self) -> None:
        """Initialize the REST store."""
        client_version = zenml.__version__
        server_version = self.get_store_info().version

        if not DISABLE_CLIENT_SERVER_MISMATCH_WARNING and (
            server_version != client_version
        ):
            logger.warning(
                "Your ZenML client version (%s) does not match the server "
                "version (%s). This version mismatch might lead to errors or "
                "unexpected behavior. \nTo disable this warning message, set "
                "the environment variable `%s=True`",
                client_version,
                server_version,
                ENV_ZENML_DISABLE_CLIENT_SERVER_MISMATCH_WARNING,
            )

    def get_store_info(self) -> ServerModel:
        """Get information about the server.

        Returns:
            Information about the server.
        """
        body = self.get(INFO)
        return ServerModel.parse_obj(body)

    def get_deployment_id(self) -> UUID:
        """Get the ID of the deployment.

        Returns:
            The ID of the deployment.
        """
        return self.get_store_info().id

    # ----------------------------- API Keys -----------------------------

    def create_api_key(
        self, service_account_id: UUID, api_key: APIKeyRequest
    ) -> APIKeyResponse:
        """Create a new API key for a service account.

        Args:
            service_account_id: The ID of the service account for which to
                create the API key.
            api_key: The API key to create.

        Returns:
            The created API key.
        """
        return self._create_resource(
            resource=api_key,
            route=f"{SERVICE_ACCOUNTS}/{str(service_account_id)}{API_KEYS}",
            response_model=APIKeyResponse,
        )

    def get_api_key(
        self,
        service_account_id: UUID,
        api_key_name_or_id: Union[str, UUID],
        hydrate: bool = True,
    ) -> APIKeyResponse:
        """Get an API key for a service account.

        Args:
            service_account_id: The ID of the service account for which to fetch
                the API key.
            api_key_name_or_id: The name or ID of the API key to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The API key with the given ID.
        """
        return self._get_resource(
            resource_id=api_key_name_or_id,
            route=f"{SERVICE_ACCOUNTS}/{str(service_account_id)}{API_KEYS}",
            response_model=APIKeyResponse,
            params={"hydrate": hydrate},
        )

    def set_api_key(self, api_key: str) -> None:
        """Set the API key to use for authentication.

        Args:
            api_key: The API key to use for authentication.
        """
        self.config.api_key = api_key
        self.clear_session()
        GlobalConfiguration()._write_config()

    def list_api_keys(
        self,
        service_account_id: UUID,
        filter_model: APIKeyFilter,
        hydrate: bool = False,
    ) -> Page[APIKeyResponse]:
        """List all API keys for a service account matching the given filter criteria.

        Args:
            service_account_id: The ID of the service account for which to list
                the API keys.
            filter_model: All filter parameters including pagination
                params
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A list of all API keys matching the filter criteria.
        """
        return self._list_paginated_resources(
            route=f"{SERVICE_ACCOUNTS}/{str(service_account_id)}{API_KEYS}",
            response_model=APIKeyResponse,
            filter_model=filter_model,
            params={"hydrate": hydrate},
        )

    def update_api_key(
        self,
        service_account_id: UUID,
        api_key_name_or_id: Union[str, UUID],
        api_key_update: APIKeyUpdate,
    ) -> APIKeyResponse:
        """Update an API key for a service account.

        Args:
            service_account_id: The ID of the service account for which to
                update the API key.
            api_key_name_or_id: The name or ID of the API key to update.
            api_key_update: The update request on the API key.

        Returns:
            The updated API key.
        """
        return self._update_resource(
            resource_id=api_key_name_or_id,
            resource_update=api_key_update,
            route=f"{SERVICE_ACCOUNTS}/{str(service_account_id)}{API_KEYS}",
            response_model=APIKeyResponse,
        )

    def rotate_api_key(
        self,
        service_account_id: UUID,
        api_key_name_or_id: Union[str, UUID],
        rotate_request: APIKeyRotateRequest,
    ) -> APIKeyResponse:
        """Rotate an API key for a service account.

        Args:
            service_account_id: The ID of the service account for which to
                rotate the API key.
            api_key_name_or_id: The name or ID of the API key to rotate.
            rotate_request: The rotate request on the API key.

        Returns:
            The updated API key.
        """
        response_body = self.put(
            f"{SERVICE_ACCOUNTS}/{str(service_account_id)}{API_KEYS}/{str(api_key_name_or_id)}{API_KEY_ROTATE}",
            body=rotate_request,
        )
        return APIKeyResponse.parse_obj(response_body)

    def delete_api_key(
        self,
        service_account_id: UUID,
        api_key_name_or_id: Union[str, UUID],
    ) -> None:
        """Delete an API key for a service account.

        Args:
            service_account_id: The ID of the service account for which to
                delete the API key.
            api_key_name_or_id: The name or ID of the API key to delete.
        """
        self._delete_resource(
            resource_id=api_key_name_or_id,
            route=f"{SERVICE_ACCOUNTS}/{str(service_account_id)}{API_KEYS}",
        )

    # ----------------------------- Artifacts -----------------------------

    def create_artifact(self, artifact: ArtifactRequest) -> ArtifactResponse:
        """Creates a new artifact.

        Args:
            artifact: The artifact to create.

        Returns:
            The newly created artifact.
        """
        return self._create_resource(
            resource=artifact,
            response_model=ArtifactResponse,
            route=ARTIFACTS,
        )

    def get_artifact(
        self, artifact_id: UUID, hydrate: bool = True
    ) -> ArtifactResponse:
        """Gets an artifact.

        Args:
            artifact_id: The ID of the artifact to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The artifact.
        """
        return self._get_resource(
            resource_id=artifact_id,
            route=ARTIFACTS,
            response_model=ArtifactResponse,
            params={"hydrate": hydrate},
        )

    def list_artifacts(
        self, filter_model: ArtifactFilter, hydrate: bool = False
    ) -> Page[ArtifactResponse]:
        """List all artifacts matching the given filter criteria.

        Args:
            filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A list of all artifacts matching the filter criteria.
        """
        return self._list_paginated_resources(
            route=ARTIFACTS,
            response_model=ArtifactResponse,
            filter_model=filter_model,
            params={"hydrate": hydrate},
        )

    def update_artifact(
        self, artifact_id: UUID, artifact_update: ArtifactUpdate
    ) -> ArtifactResponse:
        """Updates an artifact.

        Args:
            artifact_id: The ID of the artifact to update.
            artifact_update: The update to be applied to the artifact.

        Returns:
            The updated artifact.
        """
        return self._update_resource(
            resource_id=artifact_id,
            resource_update=artifact_update,
            response_model=ArtifactResponse,
            route=ARTIFACTS,
        )

    def delete_artifact(self, artifact_id: UUID) -> None:
        """Deletes an artifact.

        Args:
            artifact_id: The ID of the artifact to delete.
        """
        self._delete_resource(resource_id=artifact_id, route=ARTIFACTS)

    # -------------------- Artifact Versions --------------------

    def create_artifact_version(
        self, artifact_version: ArtifactVersionRequest
    ) -> ArtifactVersionResponse:
        """Creates an artifact version.

        Args:
            artifact_version: The artifact version to create.

        Returns:
            The created artifact version.
        """
        return self._create_resource(
            resource=artifact_version,
            response_model=ArtifactVersionResponse,
            route=ARTIFACT_VERSIONS,
        )

    def get_artifact_version(
        self, artifact_version_id: UUID, hydrate: bool = True
    ) -> ArtifactVersionResponse:
        """Gets an artifact.

        Args:
            artifact_version_id: The ID of the artifact version to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The artifact version.
        """
        return self._get_resource(
            resource_id=artifact_version_id,
            route=ARTIFACT_VERSIONS,
            response_model=ArtifactVersionResponse,
            params={"hydrate": hydrate},
        )

    def list_artifact_versions(
        self,
        artifact_version_filter_model: ArtifactVersionFilter,
        hydrate: bool = False,
    ) -> Page[ArtifactVersionResponse]:
        """List all artifact versions matching the given filter criteria.

        Args:
            artifact_version_filter_model: All filter parameters including
                pagination params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A list of all artifact versions matching the filter criteria.
        """
        return self._list_paginated_resources(
            route=ARTIFACT_VERSIONS,
            response_model=ArtifactVersionResponse,
            filter_model=artifact_version_filter_model,
            params={"hydrate": hydrate},
        )

    def update_artifact_version(
        self,
        artifact_version_id: UUID,
        artifact_version_update: ArtifactVersionUpdate,
    ) -> ArtifactVersionResponse:
        """Updates an artifact version.

        Args:
            artifact_version_id: The ID of the artifact version to update.
            artifact_version_update: The update to be applied to the artifact
                version.

        Returns:
            The updated artifact version.
        """
        return self._update_resource(
            resource_id=artifact_version_id,
            resource_update=artifact_version_update,
            response_model=ArtifactVersionResponse,
            route=ARTIFACT_VERSIONS,
        )

    def delete_artifact_version(self, artifact_version_id: UUID) -> None:
        """Deletes an artifact version.

        Args:
            artifact_version_id: The ID of the artifact version to delete.
        """
        self._delete_resource(
            resource_id=artifact_version_id, route=ARTIFACT_VERSIONS
        )

    def prune_artifact_versions(
        self,
        only_versions: bool = True,
    ) -> None:
        """Prunes unused artifact versions and their artifacts.

        Args:
            only_versions: Only delete artifact versions, keeping artifacts
        """
        self.delete(
            path=ARTIFACT_VERSIONS, params={"only_versions": only_versions}
        )

    # ------------------------ Artifact Visualizations ------------------------

    def get_artifact_visualization(
        self, artifact_visualization_id: UUID, hydrate: bool = True
    ) -> ArtifactVisualizationResponse:
        """Gets an artifact visualization.

        Args:
            artifact_visualization_id: The ID of the artifact visualization to
                get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The artifact visualization.
        """
        return self._get_resource(
            resource_id=artifact_visualization_id,
            route=ARTIFACT_VISUALIZATIONS,
            response_model=ArtifactVisualizationResponse,
            params={"hydrate": hydrate},
        )

    # ------------------------ Code References ------------------------

    def get_code_reference(
        self, code_reference_id: UUID, hydrate: bool = True
    ) -> CodeReferenceResponse:
        """Gets a code reference.

        Args:
            code_reference_id: The ID of the code reference to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The code reference.
        """
        return self._get_resource(
            resource_id=code_reference_id,
            route=CODE_REFERENCES,
            response_model=CodeReferenceResponse,
            params={"hydrate": hydrate},
        )

    # --------------------------- Code Repositories ---------------------------

    def create_code_repository(
        self, code_repository: CodeRepositoryRequest
    ) -> CodeRepositoryResponse:
        """Creates a new code repository.

        Args:
            code_repository: Code repository to be created.

        Returns:
            The newly created code repository.
        """
        return self._create_workspace_scoped_resource(
            resource=code_repository,
            response_model=CodeRepositoryResponse,
            route=CODE_REPOSITORIES,
        )

    def get_code_repository(
        self, code_repository_id: UUID, hydrate: bool = True
    ) -> CodeRepositoryResponse:
        """Gets a specific code repository.

        Args:
            code_repository_id: The ID of the code repository to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The requested code repository, if it was found.
        """
        return self._get_resource(
            resource_id=code_repository_id,
            route=CODE_REPOSITORIES,
            response_model=CodeRepositoryResponse,
            params={"hydrate": hydrate},
        )

    def list_code_repositories(
        self,
        filter_model: CodeRepositoryFilter,
        hydrate: bool = False,
    ) -> Page[CodeRepositoryResponse]:
        """List all code repositories.

        Args:
            filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A page of all code repositories.
        """
        return self._list_paginated_resources(
            route=CODE_REPOSITORIES,
            response_model=CodeRepositoryResponse,
            filter_model=filter_model,
            params={"hydrate": hydrate},
        )

    def update_code_repository(
        self, code_repository_id: UUID, update: CodeRepositoryUpdate
    ) -> CodeRepositoryResponse:
        """Updates an existing code repository.

        Args:
            code_repository_id: The ID of the code repository to update.
            update: The update to be applied to the code repository.

        Returns:
            The updated code repository.
        """
        return self._update_resource(
            resource_id=code_repository_id,
            resource_update=update,
            response_model=CodeRepositoryResponse,
            route=CODE_REPOSITORIES,
        )

    def delete_code_repository(self, code_repository_id: UUID) -> None:
        """Deletes a code repository.

        Args:
            code_repository_id: The ID of the code repository to delete.
        """
        self._delete_resource(
            resource_id=code_repository_id, route=CODE_REPOSITORIES
        )

    # ----------------------------- Components -----------------------------

    def create_stack_component(
        self,
        component: ComponentRequest,
    ) -> ComponentResponse:
        """Create a stack component.

        Args:
            component: The stack component to create.

        Returns:
            The created stack component.
        """
        return self._create_workspace_scoped_resource(
            resource=component,
            route=STACK_COMPONENTS,
            response_model=ComponentResponse,
        )

    def get_stack_component(
        self, component_id: UUID, hydrate: bool = True
    ) -> ComponentResponse:
        """Get a stack component by ID.

        Args:
            component_id: The ID of the stack component to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The stack component.
        """
        return self._get_resource(
            resource_id=component_id,
            route=STACK_COMPONENTS,
            response_model=ComponentResponse,
            params={"hydrate": hydrate},
        )

    def list_stack_components(
        self,
        component_filter_model: ComponentFilter,
        hydrate: bool = False,
    ) -> Page[ComponentResponse]:
        """List all stack components matching the given filter criteria.

        Args:
            component_filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A list of all stack components matching the filter criteria.
        """
        return self._list_paginated_resources(
            route=STACK_COMPONENTS,
            response_model=ComponentResponse,
            filter_model=component_filter_model,
            params={"hydrate": hydrate},
        )

    def update_stack_component(
        self,
        component_id: UUID,
        component_update: ComponentUpdate,
    ) -> ComponentResponse:
        """Update an existing stack component.

        Args:
            component_id: The ID of the stack component to update.
            component_update: The update to be applied to the stack component.

        Returns:
            The updated stack component.
        """
        return self._update_resource(
            resource_id=component_id,
            resource_update=component_update,
            route=STACK_COMPONENTS,
            response_model=ComponentResponse,
        )

    def delete_stack_component(self, component_id: UUID) -> None:
        """Delete a stack component.

        Args:
            component_id: The ID of the stack component to delete.
        """
        self._delete_resource(
            resource_id=component_id,
            route=STACK_COMPONENTS,
        )

    #  ----------------------------- Flavors -----------------------------

    def create_flavor(self, flavor: FlavorRequest) -> FlavorResponse:
        """Creates a new stack component flavor.

        Args:
            flavor: The stack component flavor to create.

        Returns:
            The newly created flavor.
        """
        return self._create_resource(
            resource=flavor,
            route=FLAVORS,
            response_model=FlavorResponse,
        )

    def get_flavor(
        self, flavor_id: UUID, hydrate: bool = True
    ) -> FlavorResponse:
        """Get a stack component flavor by ID.

        Args:
            flavor_id: The ID of the stack component flavor to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The stack component flavor.
        """
        return self._get_resource(
            resource_id=flavor_id,
            route=FLAVORS,
            response_model=FlavorResponse,
            params={"hydrate": hydrate},
        )

    def list_flavors(
        self,
        flavor_filter_model: FlavorFilter,
        hydrate: bool = False,
    ) -> Page[FlavorResponse]:
        """List all stack component flavors matching the given filter criteria.

        Args:
            flavor_filter_model: All filter parameters including pagination
                params
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            List of all the stack component flavors matching the given criteria.
        """
        return self._list_paginated_resources(
            route=FLAVORS,
            response_model=FlavorResponse,
            filter_model=flavor_filter_model,
            params={"hydrate": hydrate},
        )

    def update_flavor(
        self, flavor_id: UUID, flavor_update: FlavorUpdate
    ) -> FlavorResponse:
        """Updates an existing user.

        Args:
            flavor_id: The id of the flavor to update.
            flavor_update: The update to be applied to the flavor.

        Returns:
            The updated flavor.
        """
        return self._update_resource(
            resource_id=flavor_id,
            resource_update=flavor_update,
            route=FLAVORS,
            response_model=FlavorResponse,
        )

    def delete_flavor(self, flavor_id: UUID) -> None:
        """Delete a stack component flavor.

        Args:
            flavor_id: The ID of the stack component flavor to delete.
        """
        self._delete_resource(
            resource_id=flavor_id,
            route=FLAVORS,
        )

    # ------------------------ Logs ------------------------

    def get_logs(self, logs_id: UUID, hydrate: bool = True) -> LogsResponse:
        """Gets logs with the given ID.

        Args:
            logs_id: The ID of the logs to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The logs.
        """
        return self._get_resource(
            resource_id=logs_id,
            route=LOGS,
            response_model=LogsResponse,
            params={"hydrate": hydrate},
        )

    # ----------------------------- Pipelines -----------------------------

    def create_pipeline(self, pipeline: PipelineRequest) -> PipelineResponse:
        """Creates a new pipeline in a workspace.

        Args:
            pipeline: The pipeline to create.

        Returns:
            The newly created pipeline.
        """
        return self._create_workspace_scoped_resource(
            resource=pipeline,
            route=PIPELINES,
            response_model=PipelineResponse,
        )

    def get_pipeline(
        self, pipeline_id: UUID, hydrate: bool = True
    ) -> PipelineResponse:
        """Get a pipeline with a given ID.

        Args:
            pipeline_id: ID of the pipeline.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The pipeline.
        """
        return self._get_resource(
            resource_id=pipeline_id,
            route=PIPELINES,
            response_model=PipelineResponse,
            params={"hydrate": hydrate},
        )

    def list_pipelines(
        self,
        pipeline_filter_model: PipelineFilter,
        hydrate: bool = False,
    ) -> Page[PipelineResponse]:
        """List all pipelines matching the given filter criteria.

        Args:
            pipeline_filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A list of all pipelines matching the filter criteria.
        """
        return self._list_paginated_resources(
            route=PIPELINES,
            response_model=PipelineResponse,
            filter_model=pipeline_filter_model,
            params={"hydrate": hydrate},
        )

    def update_pipeline(
        self, pipeline_id: UUID, pipeline_update: PipelineUpdate
    ) -> PipelineResponse:
        """Updates a pipeline.

        Args:
            pipeline_id: The ID of the pipeline to be updated.
            pipeline_update: The update to be applied.

        Returns:
            The updated pipeline.
        """
        return self._update_resource(
            resource_id=pipeline_id,
            resource_update=pipeline_update,
            route=PIPELINES,
            response_model=PipelineResponse,
        )

    def delete_pipeline(self, pipeline_id: UUID) -> None:
        """Deletes a pipeline.

        Args:
            pipeline_id: The ID of the pipeline to delete.
        """
        self._delete_resource(
            resource_id=pipeline_id,
            route=PIPELINES,
        )

    # --------------------------- Pipeline Builds ---------------------------

    def create_build(
        self,
        build: PipelineBuildRequest,
    ) -> PipelineBuildResponse:
        """Creates a new build in a workspace.

        Args:
            build: The build to create.

        Returns:
            The newly created build.
        """
        return self._create_workspace_scoped_resource(
            resource=build,
            route=PIPELINE_BUILDS,
            response_model=PipelineBuildResponse,
        )

    def get_build(
        self, build_id: UUID, hydrate: bool = True
    ) -> PipelineBuildResponse:
        """Get a build with a given ID.

        Args:
            build_id: ID of the build.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The build.
        """
        return self._get_resource(
            resource_id=build_id,
            route=PIPELINE_BUILDS,
            response_model=PipelineBuildResponse,
            params={"hydrate": hydrate},
        )

    def list_builds(
        self,
        build_filter_model: PipelineBuildFilter,
        hydrate: bool = False,
    ) -> Page[PipelineBuildResponse]:
        """List all builds matching the given filter criteria.

        Args:
            build_filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A page of all builds matching the filter criteria.
        """
        return self._list_paginated_resources(
            route=PIPELINE_BUILDS,
            response_model=PipelineBuildResponse,
            filter_model=build_filter_model,
            params={"hydrate": hydrate},
        )

    def delete_build(self, build_id: UUID) -> None:
        """Deletes a build.

        Args:
            build_id: The ID of the build to delete.
        """
        self._delete_resource(
            resource_id=build_id,
            route=PIPELINE_BUILDS,
        )

        # ----------------------
        # Pipeline Deployments
        # ----------------------

    # -------------------------- Pipeline Deployments --------------------------

    def create_deployment(
        self,
        deployment: PipelineDeploymentRequest,
    ) -> PipelineDeploymentResponse:
        """Creates a new deployment in a workspace.

        Args:
            deployment: The deployment to create.

        Returns:
            The newly created deployment.
        """
        return self._create_workspace_scoped_resource(
            resource=deployment,
            route=PIPELINE_DEPLOYMENTS,
            response_model=PipelineDeploymentResponse,
        )

    def get_deployment(
        self, deployment_id: UUID, hydrate: bool = True
    ) -> PipelineDeploymentResponse:
        """Get a deployment with a given ID.

        Args:
            deployment_id: ID of the deployment.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The deployment.
        """
        return self._get_resource(
            resource_id=deployment_id,
            route=PIPELINE_DEPLOYMENTS,
            response_model=PipelineDeploymentResponse,
            params={"hydrate": hydrate},
        )

    def list_deployments(
        self,
        deployment_filter_model: PipelineDeploymentFilter,
        hydrate: bool = False,
    ) -> Page[PipelineDeploymentResponse]:
        """List all deployments matching the given filter criteria.

        Args:
            deployment_filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A page of all deployments matching the filter criteria.
        """
        return self._list_paginated_resources(
            route=PIPELINE_DEPLOYMENTS,
            response_model=PipelineDeploymentResponse,
            filter_model=deployment_filter_model,
            params={"hydrate": hydrate},
        )

    def delete_deployment(self, deployment_id: UUID) -> None:
        """Deletes a deployment.

        Args:
            deployment_id: The ID of the deployment to delete.
        """
        self._delete_resource(
            resource_id=deployment_id,
            route=PIPELINE_DEPLOYMENTS,
        )

    # -------------------- Event Sources  --------------------

    def create_event_source(
        self, event_source: EventSourceRequest
    ) -> EventSourceResponse:
        """Create an event_source.

        Args:
            event_source: The event_source to create.

        Returns:
            The created event_source.
        """
        return self._create_resource(
            resource=event_source,
            route=EVENT_SOURCES,
            response_model=EventSourceResponse,
        )

    def get_event_source(
        self,
        event_source_id: UUID,
        hydrate: bool = True,
    ) -> EventSourceResponse:
        """Get an event_source by ID.

        Args:
            event_source_id: The ID of the event_source to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The event_source.

        Raises:
            KeyError: if the stack event_source doesn't exist.
        """
        return self._get_resource(
            resource_id=event_source_id,
            route=EVENT_SOURCES,
            response_model=EventSourceResponse,
            params={"hydrate": hydrate},
        )

    def list_event_sources(
        self,
        event_source_filter_model: EventSourceFilter,
        hydrate: bool = False,
    ) -> Page[EventSourceResponse]:
        """List all event_sources matching the given filter criteria.

        Args:
            event_source_filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A list of all event_sources matching the filter criteria.
        """
        return self._list_paginated_resources(
            route=EVENT_SOURCES,
            response_model=EventSourceResponse,
            filter_model=event_source_filter_model,
            params={"hydrate": hydrate},
        )

    def update_event_source(
        self,
        event_source_id: UUID,
        event_source_update: EventSourceUpdate,
    ) -> EventSourceResponse:
        """Update an existing event_source.

        Args:
            event_source_id: The ID of the event_source to update.
            event_source_update: The update to be applied to the event_source.

        Returns:
            The updated event_source.

        Raises:
            KeyError: if the event_source doesn't exist.
        """
        return self._update_resource(
            resource_id=event_source_id,
            resource_update=event_source_update,
            route=EVENT_SOURCES,
            response_model=EventSourceResponse,
        )

    def delete_event_source(self, event_source_id: UUID) -> None:
        """Delete an event_source.

        Args:
            event_source_id: The ID of the event_source to delete.

        Raises:
            KeyError: if the event_source doesn't exist.
        """
        self._delete_resource(
            resource_id=event_source_id,
            route=EVENT_SOURCES,
        )

    # ----------------------------- Pipeline runs -----------------------------

    def create_run(
        self, pipeline_run: PipelineRunRequest
    ) -> PipelineRunResponse:
        """Creates a pipeline run.

        Args:
            pipeline_run: The pipeline run to create.

        Returns:
            The created pipeline run.
        """
        return self._create_workspace_scoped_resource(
            resource=pipeline_run,
            response_model=PipelineRunResponse,
            route=RUNS,
        )

    def get_run(
        self, run_name_or_id: Union[UUID, str], hydrate: bool = True
    ) -> PipelineRunResponse:
        """Gets a pipeline run.

        Args:
            run_name_or_id: The name or ID of the pipeline run to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The pipeline run.
        """
        return self._get_resource(
            resource_id=run_name_or_id,
            route=RUNS,
            response_model=PipelineRunResponse,
            params={"hydrate": hydrate},
        )

    def list_runs(
        self,
        runs_filter_model: PipelineRunFilter,
        hydrate: bool = False,
    ) -> Page[PipelineRunResponse]:
        """List all pipeline runs matching the given filter criteria.

        Args:
            runs_filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A list of all pipeline runs matching the filter criteria.
        """
        return self._list_paginated_resources(
            route=RUNS,
            response_model=PipelineRunResponse,
            filter_model=runs_filter_model,
            params={"hydrate": hydrate},
        )

    def update_run(
        self, run_id: UUID, run_update: PipelineRunUpdate
    ) -> PipelineRunResponse:
        """Updates a pipeline run.

        Args:
            run_id: The ID of the pipeline run to update.
            run_update: The update to be applied to the pipeline run.


        Returns:
            The updated pipeline run.
        """
        return self._update_resource(
            resource_id=run_id,
            resource_update=run_update,
            response_model=PipelineRunResponse,
            route=RUNS,
        )

    def delete_run(self, run_id: UUID) -> None:
        """Deletes a pipeline run.

        Args:
            run_id: The ID of the pipeline run to delete.
        """
        self._delete_resource(
            resource_id=run_id,
            route=RUNS,
        )

    def get_or_create_run(
        self, pipeline_run: PipelineRunRequest
    ) -> Tuple[PipelineRunResponse, bool]:
        """Gets or creates a pipeline run.

        If a run with the same ID or name already exists, it is returned.
        Otherwise, a new run is created.

        Args:
            pipeline_run: The pipeline run to get or create.

        Returns:
            The pipeline run, and a boolean indicating whether the run was
            created or not.
        """
        return self._get_or_create_workspace_scoped_resource(
            resource=pipeline_run,
            route=RUNS,
            response_model=PipelineRunResponse,
        )

    # ----------------------------- Run Metadata -----------------------------

    def create_run_metadata(
        self, run_metadata: RunMetadataRequest
    ) -> List[RunMetadataResponse]:
        """Creates run metadata.

        Args:
            run_metadata: The run metadata to create.

        Returns:
            The created run metadata.
        """
        route = f"{WORKSPACES}/{str(run_metadata.workspace)}{RUN_METADATA}"
        response_body = self.post(f"{route}", body=run_metadata)
        result: List[RunMetadataResponse] = []
        if isinstance(response_body, list):
            for metadata in response_body or []:
                result.append(RunMetadataResponse.parse_obj(metadata))
        return result

    def get_run_metadata(
        self, run_metadata_id: UUID, hydrate: bool = True
    ) -> RunMetadataResponse:
        """Gets run metadata with the given ID.

        Args:
            run_metadata_id: The ID of the run metadata to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The run metadata.
        """
        return self._get_resource(
            resource_id=run_metadata_id,
            route=RUN_METADATA,
            response_model=RunMetadataResponse,
            params={"hydrate": hydrate},
        )

    def list_run_metadata(
        self,
        run_metadata_filter_model: RunMetadataFilter,
        hydrate: bool = False,
    ) -> Page[RunMetadataResponse]:
        """List run metadata.

        Args:
            run_metadata_filter_model: All filter parameters including
                pagination params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The run metadata.
        """
        return self._list_paginated_resources(
            route=RUN_METADATA,
            response_model=RunMetadataResponse,
            filter_model=run_metadata_filter_model,
            params={"hydrate": hydrate},
        )

    # ----------------------------- Schedules -----------------------------

    def create_schedule(self, schedule: ScheduleRequest) -> ScheduleResponse:
        """Creates a new schedule.

        Args:
            schedule: The schedule to create.

        Returns:
            The newly created schedule.
        """
        return self._create_workspace_scoped_resource(
            resource=schedule,
            route=SCHEDULES,
            response_model=ScheduleResponse,
        )

    def get_schedule(
        self, schedule_id: UUID, hydrate: bool = True
    ) -> ScheduleResponse:
        """Get a schedule with a given ID.

        Args:
            schedule_id: ID of the schedule.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The schedule.
        """
        return self._get_resource(
            resource_id=schedule_id,
            route=SCHEDULES,
            response_model=ScheduleResponse,
            params={"hydrate": hydrate},
        )

    def list_schedules(
        self,
        schedule_filter_model: ScheduleFilter,
        hydrate: bool = False,
    ) -> Page[ScheduleResponse]:
        """List all schedules in the workspace.

        Args:
            schedule_filter_model: All filter parameters including pagination
                params
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A list of schedules.
        """
        return self._list_paginated_resources(
            route=SCHEDULES,
            response_model=ScheduleResponse,
            filter_model=schedule_filter_model,
            params={"hydrate": hydrate},
        )

    def update_schedule(
        self,
        schedule_id: UUID,
        schedule_update: ScheduleUpdate,
    ) -> ScheduleResponse:
        """Updates a schedule.

        Args:
            schedule_id: The ID of the schedule to be updated.
            schedule_update: The update to be applied.

        Returns:
            The updated schedule.
        """
        return self._update_resource(
            resource_id=schedule_id,
            resource_update=schedule_update,
            route=SCHEDULES,
            response_model=ScheduleResponse,
        )

    def delete_schedule(self, schedule_id: UUID) -> None:
        """Deletes a schedule.

        Args:
            schedule_id: The ID of the schedule to delete.
        """
        self._delete_resource(
            resource_id=schedule_id,
            route=SCHEDULES,
        )

    # --------------------------- Secrets ---------------------------

    def create_secret(self, secret: SecretRequest) -> SecretResponse:
        """Creates a new secret.

        The new secret is also validated against the scoping rules enforced in
        the secrets store:

          - only one workspace-scoped secret with the given name can exist
            in the target workspace.
          - only one user-scoped secret with the given name can exist in the
            target workspace for the target user.

        Args:
            secret: The secret to create.

        Returns:
            The newly created secret.
        """
        return self._create_workspace_scoped_resource(
            resource=secret,
            route=SECRETS,
            response_model=SecretResponse,
        )

    def get_secret(
        self, secret_id: UUID, hydrate: bool = True
    ) -> SecretResponse:
        """Get a secret by ID.

        Args:
            secret_id: The ID of the secret to fetch.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The secret.
        """
        return self._get_resource(
            resource_id=secret_id,
            route=SECRETS,
            response_model=SecretResponse,
            params={"hydrate": hydrate},
        )

    def list_secrets(
        self, secret_filter_model: SecretFilter, hydrate: bool = False
    ) -> Page[SecretResponse]:
        """List all secrets matching the given filter criteria.

        Note that returned secrets do not include any secret values. To fetch
        the secret values, use `get_secret`.

        Args:
            secret_filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A list of all secrets matching the filter criteria, with pagination
            information and sorted according to the filter criteria. The
            returned secrets do not include any secret values, only metadata. To
            fetch the secret values, use `get_secret` individually with each
            secret.
        """
        return self._list_paginated_resources(
            route=SECRETS,
            response_model=SecretResponse,
            filter_model=secret_filter_model,
            params={"hydrate": hydrate},
        )

    def update_secret(
        self, secret_id: UUID, secret_update: SecretUpdate
    ) -> SecretResponse:
        """Updates a secret.

        Secret values that are specified as `None` in the update that are
        present in the existing secret are removed from the existing secret.
        Values that are present in both secrets are overwritten. All other
        values in both the existing secret and the update are kept (merged).

        If the update includes a change of name or scope, the scoping rules
        enforced in the secrets store are used to validate the update:

          - only one workspace-scoped secret with the given name can exist
            in the target workspace.
          - only one user-scoped secret with the given name can exist in the
            target workspace for the target user.

        Args:
            secret_id: The ID of the secret to be updated.
            secret_update: The update to be applied.

        Returns:
            The updated secret.
        """
        return self._update_resource(
            resource_id=secret_id,
            resource_update=secret_update,
            route=SECRETS,
            response_model=SecretResponse,
            # The default endpoint behavior is to replace all secret values
            # with the values in the update. We want to merge the values
            # instead.
            params=dict(patch_values=True),
        )

    def delete_secret(self, secret_id: UUID) -> None:
        """Delete a secret.

        Args:
            secret_id: The id of the secret to delete.
        """
        self._delete_resource(
            resource_id=secret_id,
            route=SECRETS,
        )

    def backup_secrets(
        self, ignore_errors: bool = True, delete_secrets: bool = False
    ) -> None:
        """Backs up all secrets to the configured backup secrets store.

        Args:
            ignore_errors: Whether to ignore individual errors during the backup
                process and attempt to backup all secrets.
            delete_secrets: Whether to delete the secrets that have been
                successfully backed up from the primary secrets store. Setting
                this flag effectively moves all secrets from the primary secrets
                store to the backup secrets store.
        """
        params: Dict[str, Any] = {
            "ignore_errors": ignore_errors,
            "delete_secrets": delete_secrets,
        }
        self.put(
            f"{SECRETS_OPERATIONS}{SECRETS_BACKUP}",
            params=params,
        )

    def restore_secrets(
        self, ignore_errors: bool = False, delete_secrets: bool = False
    ) -> None:
        """Restore all secrets from the configured backup secrets store.

        Args:
            ignore_errors: Whether to ignore individual errors during the
                restore process and attempt to restore all secrets.
            delete_secrets: Whether to delete the secrets that have been
                successfully restored from the backup secrets store. Setting
                this flag effectively moves all secrets from the backup secrets
                store to the primary secrets store.
        """
        params: Dict[str, Any] = {
            "ignore_errors": ignore_errors,
            "delete_secrets": delete_secrets,
        }
        self.put(
            f"{SECRETS_OPERATIONS}{SECRETS_RESTORE}",
            params=params,
        )

    # --------------------------- Service Accounts ---------------------------

    def create_service_account(
        self, service_account: ServiceAccountRequest
    ) -> ServiceAccountResponse:
        """Creates a new service account.

        Args:
            service_account: Service account to be created.

        Returns:
            The newly created service account.
        """
        return self._create_resource(
            resource=service_account,
            route=SERVICE_ACCOUNTS,
            response_model=ServiceAccountResponse,
        )

    def get_service_account(
        self,
        service_account_name_or_id: Union[str, UUID],
        hydrate: bool = True,
    ) -> ServiceAccountResponse:
        """Gets a specific service account.

        Args:
            service_account_name_or_id: The name or ID of the service account to
                get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The requested service account, if it was found.
        """
        return self._get_resource(
            resource_id=service_account_name_or_id,
            route=SERVICE_ACCOUNTS,
            response_model=ServiceAccountResponse,
            params={"hydrate": hydrate},
        )

    def list_service_accounts(
        self, filter_model: ServiceAccountFilter, hydrate: bool = False
    ) -> Page[ServiceAccountResponse]:
        """List all service accounts.

        Args:
            filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A list of filtered service accounts.
        """
        return self._list_paginated_resources(
            route=SERVICE_ACCOUNTS,
            response_model=ServiceAccountResponse,
            filter_model=filter_model,
            params={"hydrate": hydrate},
        )

    def update_service_account(
        self,
        service_account_name_or_id: Union[str, UUID],
        service_account_update: ServiceAccountUpdate,
    ) -> ServiceAccountResponse:
        """Updates an existing service account.

        Args:
            service_account_name_or_id: The name or the ID of the service
                account to update.
            service_account_update: The update to be applied to the service
                account.

        Returns:
            The updated service account.
        """
        return self._update_resource(
            resource_id=service_account_name_or_id,
            resource_update=service_account_update,
            route=SERVICE_ACCOUNTS,
            response_model=ServiceAccountResponse,
        )

    def delete_service_account(
        self,
        service_account_name_or_id: Union[str, UUID],
    ) -> None:
        """Delete a service account.

        Args:
            service_account_name_or_id: The name or the ID of the service
                account to delete.
        """
        self._delete_resource(
            resource_id=service_account_name_or_id,
            route=SERVICE_ACCOUNTS,
        )

    # --------------------------- Service Connectors ---------------------------

    def create_service_connector(
        self, service_connector: ServiceConnectorRequest
    ) -> ServiceConnectorResponse:
        """Creates a new service connector.

        Args:
            service_connector: Service connector to be created.

        Returns:
            The newly created service connector.
        """
        connector_model = self._create_workspace_scoped_resource(
            resource=service_connector,
            route=SERVICE_CONNECTORS,
            response_model=ServiceConnectorResponse,
        )
        self._populate_connector_type(connector_model)
        return connector_model

    def get_service_connector(
        self, service_connector_id: UUID, hydrate: bool = True
    ) -> ServiceConnectorResponse:
        """Gets a specific service connector.

        Args:
            service_connector_id: The ID of the service connector to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The requested service connector, if it was found.
        """
        connector_model = self._get_resource(
            resource_id=service_connector_id,
            route=SERVICE_CONNECTORS,
            response_model=ServiceConnectorResponse,
            params={"expand_secrets": False, "hydrate": hydrate},
        )
        self._populate_connector_type(connector_model)
        return connector_model

    def list_service_connectors(
        self,
        filter_model: ServiceConnectorFilter,
        hydrate: bool = False,
    ) -> Page[ServiceConnectorResponse]:
        """List all service connectors.

        Args:
            filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A page of all service connectors.
        """
        connector_models = self._list_paginated_resources(
            route=SERVICE_CONNECTORS,
            response_model=ServiceConnectorResponse,
            filter_model=filter_model,
            params={"expand_secrets": False, "hydrate": hydrate},
        )
        self._populate_connector_type(*connector_models.items)
        return connector_models

    def update_service_connector(
        self, service_connector_id: UUID, update: ServiceConnectorUpdate
    ) -> ServiceConnectorResponse:
        """Updates an existing service connector.

        The update model contains the fields to be updated. If a field value is
        set to None in the model, the field is not updated, but there are
        special rules concerning some fields:

        * the `configuration` and `secrets` fields together represent a full
        valid configuration update, not just a partial update. If either is
        set (i.e. not None) in the update, their values are merged together and
        will replace the existing configuration and secrets values.
        * the `resource_id` field value is also a full replacement value: if set
        to `None`, the resource ID is removed from the service connector.
        * the `expiration_seconds` field value is also a full replacement value:
        if set to `None`, the expiration is removed from the service connector.
        * the `secret_id` field value in the update is ignored, given that
        secrets are managed internally by the ZenML store.
        * the `labels` field is also a full labels update: if set (i.e. not
        `None`), all existing labels are removed and replaced by the new labels
        in the update.

        Args:
            service_connector_id: The ID of the service connector to update.
            update: The update to be applied to the service connector.

        Returns:
            The updated service connector.
        """
        connector_model = self._update_resource(
            resource_id=service_connector_id,
            resource_update=update,
            response_model=ServiceConnectorResponse,
            route=SERVICE_CONNECTORS,
        )
        self._populate_connector_type(connector_model)
        return connector_model

    def delete_service_connector(self, service_connector_id: UUID) -> None:
        """Deletes a service connector.

        Args:
            service_connector_id: The ID of the service connector to delete.
        """
        self._delete_resource(
            resource_id=service_connector_id, route=SERVICE_CONNECTORS
        )

    def _populate_connector_type(
        self,
        *connector_models: Union[
            ServiceConnectorResponse, ServiceConnectorResourcesModel
        ],
    ) -> None:
        """Populates or updates the connector type of the given connector or resource models.

        If the connector type is not locally available, the connector type
        field is left as is. The local and remote flags of the connector type
        are updated accordingly.

        Args:
            connector_models: The service connector or resource models to
                populate.
        """
        for service_connector in connector_models:
            # Mark the remote connector type as being only remotely available
            if not isinstance(service_connector.connector_type, str):
                service_connector.connector_type.local = False
                service_connector.connector_type.remote = True

            if not service_connector_registry.is_registered(
                service_connector.type
            ):
                continue

            connector_type = (
                service_connector_registry.get_service_connector_type(
                    service_connector.type
                )
            )
            connector_type.local = True
            if not isinstance(service_connector.connector_type, str):
                connector_type.remote = True

            # TODO: Normally, this could have been handled with setter
            #   functions over the connector type property in the response
            #   model. However, pydantic breaks property setter functions.
            #   We can find a more elegant solution here.
            if isinstance(service_connector, ServiceConnectorResponse):
                service_connector.set_connector_type(connector_type)
            elif isinstance(service_connector, ServiceConnectorResourcesModel):
                service_connector.connector_type = connector_type
            else:
                TypeError(
                    "The service connector must be an instance of either"
                    "`ServiceConnectorResponse` or "
                    "`ServiceConnectorResourcesModel`."
                )

    def verify_service_connector_config(
        self,
        service_connector: ServiceConnectorRequest,
        list_resources: bool = True,
    ) -> ServiceConnectorResourcesModel:
        """Verifies if a service connector configuration has access to resources.

        Args:
            service_connector: The service connector configuration to verify.
            list_resources: If True, the list of all resources accessible
                through the service connector and matching the supplied resource
                type and ID are returned.

        Returns:
            The list of resources that the service connector configuration has
            access to.
        """
        response_body = self.post(
            f"{SERVICE_CONNECTORS}{SERVICE_CONNECTOR_VERIFY}",
            body=service_connector,
            params={"list_resources": list_resources},
        )

        resources = ServiceConnectorResourcesModel.parse_obj(response_body)
        self._populate_connector_type(resources)
        return resources

    def verify_service_connector(
        self,
        service_connector_id: UUID,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        list_resources: bool = True,
    ) -> ServiceConnectorResourcesModel:
        """Verifies if a service connector instance has access to one or more resources.

        Args:
            service_connector_id: The ID of the service connector to verify.
            resource_type: The type of resource to verify access to.
            resource_id: The ID of the resource to verify access to.
            list_resources: If True, the list of all resources accessible
                through the service connector and matching the supplied resource
                type and ID are returned.

        Returns:
            The list of resources that the service connector has access to,
            scoped to the supplied resource type and ID, if provided.
        """
        params: Dict[str, Any] = {"list_resources": list_resources}
        if resource_type:
            params["resource_type"] = resource_type
        if resource_id:
            params["resource_id"] = resource_id
        response_body = self.put(
            f"{SERVICE_CONNECTORS}/{str(service_connector_id)}{SERVICE_CONNECTOR_VERIFY}",
            params=params,
        )

        resources = ServiceConnectorResourcesModel.parse_obj(response_body)
        self._populate_connector_type(resources)
        return resources

    def get_service_connector_client(
        self,
        service_connector_id: UUID,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
    ) -> ServiceConnectorResponse:
        """Get a service connector client for a service connector and given resource.

        Args:
            service_connector_id: The ID of the base service connector to use.
            resource_type: The type of resource to get a client for.
            resource_id: The ID of the resource to get a client for.

        Returns:
            A service connector client that can be used to access the given
            resource.
        """
        params = {}
        if resource_type:
            params["resource_type"] = resource_type
        if resource_id:
            params["resource_id"] = resource_id
        response_body = self.get(
            f"{SERVICE_CONNECTORS}/{str(service_connector_id)}{SERVICE_CONNECTOR_CLIENT}",
            params=params,
        )

        connector = ServiceConnectorResponse.parse_obj(response_body)
        self._populate_connector_type(connector)
        return connector

    def list_service_connector_resources(
        self,
        workspace_name_or_id: Union[str, UUID],
        connector_type: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
    ) -> List[ServiceConnectorResourcesModel]:
        """List resources that can be accessed by service connectors.

        Args:
            workspace_name_or_id: The name or ID of the workspace to scope to.
            connector_type: The type of service connector to scope to.
            resource_type: The type of resource to scope to.
            resource_id: The ID of the resource to scope to.

        Returns:
            The matching list of resources that available service
            connectors have access to.
        """
        params = {}
        if connector_type:
            params["connector_type"] = connector_type
        if resource_type:
            params["resource_type"] = resource_type
        if resource_id:
            params["resource_id"] = resource_id
        response_body = self.get(
            f"{WORKSPACES}/{workspace_name_or_id}{SERVICE_CONNECTORS}{SERVICE_CONNECTOR_RESOURCES}",
            params=params,
        )

        assert isinstance(response_body, list)
        resource_list = [
            ServiceConnectorResourcesModel.parse_obj(item)
            for item in response_body
        ]

        self._populate_connector_type(*resource_list)

        # For service connectors with types that are only locally available,
        # we need to retrieve the resource list locally
        for idx, resources in enumerate(resource_list):
            if isinstance(resources.connector_type, str):
                # Skip connector types that are neither locally nor remotely
                # available
                continue
            if resources.connector_type.remote:
                # Skip connector types that are remotely available
                continue

            # Retrieve the resource list locally
            assert resources.id is not None
            connector = self.get_service_connector(resources.id)
            connector_instance = (
                service_connector_registry.instantiate_connector(
                    model=connector
                )
            )

            try:
                local_resources = connector_instance.verify(
                    resource_type=resource_type,
                    resource_id=resource_id,
                )
            except (ValueError, AuthorizationException) as e:
                logger.error(
                    f'Failed to fetch {resource_type or "available"} '
                    f"resources from service connector {connector.name}/"
                    f"{connector.id}: {e}"
                )
                continue

            resource_list[idx] = local_resources

        return resource_list

    def list_service_connector_types(
        self,
        connector_type: Optional[str] = None,
        resource_type: Optional[str] = None,
        auth_method: Optional[str] = None,
    ) -> List[ServiceConnectorTypeModel]:
        """Get a list of service connector types.

        Args:
            connector_type: Filter by connector type.
            resource_type: Filter by resource type.
            auth_method: Filter by authentication method.

        Returns:
            List of service connector types.
        """
        params = {}
        if connector_type:
            params["connector_type"] = connector_type
        if resource_type:
            params["resource_type"] = resource_type
        if auth_method:
            params["auth_method"] = auth_method
        response_body = self.get(
            SERVICE_CONNECTOR_TYPES,
            params=params,
        )

        assert isinstance(response_body, list)
        remote_connector_types = [
            ServiceConnectorTypeModel.parse_obj(item) for item in response_body
        ]

        # Mark the remote connector types as being only remotely available
        for c in remote_connector_types:
            c.local = False
            c.remote = True

        local_connector_types = (
            service_connector_registry.list_service_connector_types(
                connector_type=connector_type,
                resource_type=resource_type,
                auth_method=auth_method,
            )
        )

        # Add the connector types in the local registry to the list of
        # connector types available remotely. Overwrite those that have
        # the same connector type but mark them as being remotely available.
        connector_types_map = {
            connector_type.connector_type: connector_type
            for connector_type in remote_connector_types
        }

        for connector in local_connector_types:
            if connector.connector_type in connector_types_map:
                connector.remote = True
            connector_types_map[connector.connector_type] = connector

        return list(connector_types_map.values())

    def get_service_connector_type(
        self,
        connector_type: str,
    ) -> ServiceConnectorTypeModel:
        """Returns the requested service connector type.

        Args:
            connector_type: the service connector type identifier.

        Returns:
            The requested service connector type.
        """
        # Use the local registry to get the service connector type, if it
        # exists.
        local_connector_type: Optional[ServiceConnectorTypeModel] = None
        if service_connector_registry.is_registered(connector_type):
            local_connector_type = (
                service_connector_registry.get_service_connector_type(
                    connector_type
                )
            )
        try:
            response_body = self.get(
                f"{SERVICE_CONNECTOR_TYPES}/{connector_type}",
            )
            remote_connector_type = ServiceConnectorTypeModel.parse_obj(
                response_body
            )
            if local_connector_type:
                # If locally available, return the local connector type but
                # mark it as being remotely available.
                local_connector_type.remote = True
                return local_connector_type

            # Mark the remote connector type as being only remotely available
            remote_connector_type.local = False
            remote_connector_type.remote = True

            return remote_connector_type
        except KeyError:
            # If the service connector type is not found, check the local
            # registry.
            return service_connector_registry.get_service_connector_type(
                connector_type
            )

    # ----------------------------- Stacks -----------------------------

    def create_stack(self, stack: StackRequest) -> StackResponse:
        """Register a new stack.

        Args:
            stack: The stack to register.

        Returns:
            The registered stack.
        """
        return self._create_workspace_scoped_resource(
            resource=stack,
            route=STACKS,
            response_model=StackResponse,
        )

    def get_stack(self, stack_id: UUID, hydrate: bool = True) -> StackResponse:
        """Get a stack by its unique ID.

        Args:
            stack_id: The ID of the stack to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The stack with the given ID.
        """
        return self._get_resource(
            resource_id=stack_id,
            route=STACKS,
            response_model=StackResponse,
            params={"hydrate": hydrate},
        )

    def list_stacks(
        self, stack_filter_model: StackFilter, hydrate: bool = False
    ) -> Page[StackResponse]:
        """List all stacks matching the given filter criteria.

        Args:
            stack_filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A list of all stacks matching the filter criteria.
        """
        return self._list_paginated_resources(
            route=STACKS,
            response_model=StackResponse,
            filter_model=stack_filter_model,
            params={"hydrate": hydrate},
        )

    def update_stack(
        self, stack_id: UUID, stack_update: StackUpdate
    ) -> StackResponse:
        """Update a stack.

        Args:
            stack_id: The ID of the stack update.
            stack_update: The update request on the stack.

        Returns:
            The updated stack.
        """
        return self._update_resource(
            resource_id=stack_id,
            resource_update=stack_update,
            route=STACKS,
            response_model=StackResponse,
        )

    def delete_stack(self, stack_id: UUID) -> None:
        """Delete a stack.

        Args:
            stack_id: The ID of the stack to delete.
        """
        self._delete_resource(
            resource_id=stack_id,
            route=STACKS,
        )

    # ----------------------------- Step runs -----------------------------

    def create_run_step(self, step_run: StepRunRequest) -> StepRunResponse:
        """Creates a step run.

        Args:
            step_run: The step run to create.

        Returns:
            The created step run.
        """
        return self._create_resource(
            resource=step_run,
            response_model=StepRunResponse,
            route=STEPS,
        )

    def get_run_step(
        self, step_run_id: UUID, hydrate: bool = True
    ) -> StepRunResponse:
        """Get a step run by ID.

        Args:
            step_run_id: The ID of the step run to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The step run.
        """
        return self._get_resource(
            resource_id=step_run_id,
            route=STEPS,
            response_model=StepRunResponse,
            params={"hydrate": hydrate},
        )

    def list_run_steps(
        self,
        step_run_filter_model: StepRunFilter,
        hydrate: bool = False,
    ) -> Page[StepRunResponse]:
        """List all step runs matching the given filter criteria.

        Args:
            step_run_filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A list of all step runs matching the filter criteria.
        """
        return self._list_paginated_resources(
            route=STEPS,
            response_model=StepRunResponse,
            filter_model=step_run_filter_model,
            params={"hydrate": hydrate},
        )

    def update_run_step(
        self,
        step_run_id: UUID,
        step_run_update: StepRunUpdate,
    ) -> StepRunResponse:
        """Updates a step run.

        Args:
            step_run_id: The ID of the step to update.
            step_run_update: The update to be applied to the step.

        Returns:
            The updated step run.
        """
        return self._update_resource(
            resource_id=step_run_id,
            resource_update=step_run_update,
            response_model=StepRunResponse,
            route=STEPS,
        )

    # -------------------- Triggers  --------------------

    def create_trigger(self, trigger: TriggerRequest) -> TriggerResponse:
        """Create an trigger.

        Args:
            trigger: The trigger to create.

        Returns:
            The created trigger.
        """
        return self._create_resource(
            resource=trigger,
            route=TRIGGERS,
            response_model=TriggerResponse,
        )

    def get_trigger(
        self,
        trigger_id: UUID,
        hydrate: bool = True,
    ) -> TriggerResponse:
        """Get a trigger by ID.

        Args:
            trigger_id: The ID of the trigger to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The trigger.

        Raises:
            KeyError: if the stack trigger doesn't exist.
        """
        return self._get_resource(
            resource_id=trigger_id,
            route=TRIGGERS,
            response_model=TriggerResponse,
            params={"hydrate": hydrate},
        )

    def list_triggers(
        self,
        trigger_filter_model: TriggerFilter,
        hydrate: bool = False,
    ) -> Page[TriggerResponse]:
        """List all triggers matching the given filter criteria.

        Args:
            trigger_filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A list of all triggers matching the filter criteria.
        """
        return self._list_paginated_resources(
            route=TRIGGERS,
            response_model=TriggerResponse,
            filter_model=trigger_filter_model,
            params={"hydrate": hydrate},
        )

    def update_trigger(
        self,
        trigger_id: UUID,
        trigger_update: TriggerUpdate,
    ) -> TriggerResponse:
        """Update an existing trigger.

        Args:
            trigger_id: The ID of the trigger to update.
            trigger_update: The update to be applied to the trigger.

        Returns:
            The updated trigger.

        Raises:
            KeyError: if the trigger doesn't exist.
        """
        return self._update_resource(
            resource_id=trigger_id,
            resource_update=trigger_update,
            route=TRIGGERS,
            response_model=TriggerResponse,
        )

    def delete_trigger(self, trigger_id: UUID) -> None:
        """Delete an trigger.

        Args:
            trigger_id: The ID of the trigger to delete.

        Raises:
            KeyError: if the trigger doesn't exist.
        """
        self._delete_resource(
            resource_id=trigger_id,
            route=TRIGGERS,
        )

    # -------------------- Trigger Executions --------------------

    def get_trigger_execution(
        self,
        trigger_execution_id: UUID,
        hydrate: bool = True,
    ) -> TriggerExecutionResponse:
        """Get an trigger execution by ID.

        Args:
            trigger_execution_id: The ID of the trigger execution to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The trigger execution.

        Raises:
            KeyError: If the trigger execution doesn't exist.
        """
        return self._get_resource(
            resource_id=trigger_execution_id,
            route=TRIGGER_EXECUTIONS,
            response_model=TriggerExecutionResponse,
            params={"hydrate": hydrate},
        )

    def list_trigger_executions(
        self,
        trigger_execution_filter_model: TriggerExecutionFilter,
        hydrate: bool = False,
    ) -> Page[TriggerExecutionResponse]:
        """List all trigger executions matching the given filter criteria.

        Args:
            trigger_execution_filter_model: All filter parameters including
                pagination params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A list of all trigger executions matching the filter criteria.
        """
        return self._list_paginated_resources(
            route=TRIGGER_EXECUTIONS,
            response_model=TriggerExecutionResponse,
            filter_model=trigger_execution_filter_model,
            params={"hydrate": hydrate},
        )

    def delete_trigger_execution(self, trigger_execution_id: UUID) -> None:
        """Delete a trigger execution.

        Args:
            trigger_execution_id: The ID of the trigger execution to delete.

        Raises:
            KeyError: If the trigger execution doesn't exist.
        """
        self._delete_resource(
            resource_id=trigger_execution_id,
            route=TRIGGER_EXECUTIONS,
        )

    # ----------------------------- Users -----------------------------

    def create_user(self, user: UserRequest) -> UserResponse:
        """Creates a new user.

        Args:
            user: User to be created.

        Returns:
            The newly created user.
        """
        return self._create_resource(
            resource=user,
            route=USERS,
            response_model=UserResponse,
        )

    def get_user(
        self,
        user_name_or_id: Optional[Union[str, UUID]] = None,
        include_private: bool = False,
        hydrate: bool = True,
    ) -> UserResponse:
        """Gets a specific user, when no id is specified get the active user.

        The `include_private` parameter is ignored here as it is handled
        implicitly by the /current-user endpoint that is queried when no
        user_name_or_id is set. Raises a KeyError in case a user with that id
        does not exist.

        Args:
            user_name_or_id: The name or ID of the user to get.
            include_private: Whether to include private user information.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The requested user, if it was found.
        """
        if user_name_or_id:
            return self._get_resource(
                resource_id=user_name_or_id,
                route=USERS,
                response_model=UserResponse,
                params={"hydrate": hydrate},
            )
        else:
            body = self.get(CURRENT_USER, params={"hydrate": hydrate})
            return UserResponse.parse_obj(body)

    def list_users(
        self,
        user_filter_model: UserFilter,
        hydrate: bool = False,
    ) -> Page[UserResponse]:
        """List all users.

        Args:
            user_filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A list of all users.
        """
        return self._list_paginated_resources(
            route=USERS,
            response_model=UserResponse,
            filter_model=user_filter_model,
            params={"hydrate": hydrate},
        )

    def update_user(
        self, user_id: UUID, user_update: UserUpdate
    ) -> UserResponse:
        """Updates an existing user.

        Args:
            user_id: The id of the user to update.
            user_update: The update to be applied to the user.

        Returns:
            The updated user.
        """
        return self._update_resource(
            resource_id=user_id,
            resource_update=user_update,
            route=USERS,
            response_model=UserResponse,
        )

    def delete_user(self, user_name_or_id: Union[str, UUID]) -> None:
        """Deletes a user.

        Args:
            user_name_or_id: The name or ID of the user to delete.
        """
        self._delete_resource(
            resource_id=user_name_or_id,
            route=USERS,
        )

    # ----------------------------- Workspaces -----------------------------

    def create_workspace(
        self, workspace: WorkspaceRequest
    ) -> WorkspaceResponse:
        """Creates a new workspace.

        Args:
            workspace: The workspace to create.

        Returns:
            The newly created workspace.
        """
        return self._create_resource(
            resource=workspace,
            route=WORKSPACES,
            response_model=WorkspaceResponse,
        )

    def get_workspace(
        self, workspace_name_or_id: Union[UUID, str], hydrate: bool = True
    ) -> WorkspaceResponse:
        """Get an existing workspace by name or ID.

        Args:
            workspace_name_or_id: Name or ID of the workspace to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The requested workspace.
        """
        return self._get_resource(
            resource_id=workspace_name_or_id,
            route=WORKSPACES,
            response_model=WorkspaceResponse,
            params={"hydrate": hydrate},
        )

    def list_workspaces(
        self,
        workspace_filter_model: WorkspaceFilter,
        hydrate: bool = False,
    ) -> Page[WorkspaceResponse]:
        """List all workspace matching the given filter criteria.

        Args:
            workspace_filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A list of all workspace matching the filter criteria.
        """
        return self._list_paginated_resources(
            route=WORKSPACES,
            response_model=WorkspaceResponse,
            filter_model=workspace_filter_model,
            params={"hydrate": hydrate},
        )

    def update_workspace(
        self, workspace_id: UUID, workspace_update: WorkspaceUpdate
    ) -> WorkspaceResponse:
        """Update an existing workspace.

        Args:
            workspace_id: The ID of the workspace to be updated.
            workspace_update: The update to be applied to the workspace.

        Returns:
            The updated workspace.
        """
        return self._update_resource(
            resource_id=workspace_id,
            resource_update=workspace_update,
            route=WORKSPACES,
            response_model=WorkspaceResponse,
        )

    def delete_workspace(self, workspace_name_or_id: Union[str, UUID]) -> None:
        """Deletes a workspace.

        Args:
            workspace_name_or_id: Name or ID of the workspace to delete.
        """
        self._delete_resource(
            resource_id=workspace_name_or_id,
            route=WORKSPACES,
        )

    # --------------------------- Model ---------------------------

    def create_model(self, model: ModelRequest) -> ModelResponse:
        """Creates a new model.

        Args:
            model: the Model to be created.

        Returns:
            The newly created model.
        """
        return self._create_workspace_scoped_resource(
            resource=model,
            response_model=ModelResponse,
            route=MODELS,
        )

    def delete_model(self, model_name_or_id: Union[str, UUID]) -> None:
        """Deletes a model.

        Args:
            model_name_or_id: name or id of the model to be deleted.
        """
        self._delete_resource(resource_id=model_name_or_id, route=MODELS)

    def update_model(
        self,
        model_id: UUID,
        model_update: ModelUpdate,
    ) -> ModelResponse:
        """Updates an existing model.

        Args:
            model_id: UUID of the model to be updated.
            model_update: the Model to be updated.

        Returns:
            The updated model.
        """
        return self._update_resource(
            resource_id=model_id,
            resource_update=model_update,
            route=MODELS,
            response_model=ModelResponse,
        )

    def get_model(
        self, model_name_or_id: Union[str, UUID], hydrate: bool = True
    ) -> ModelResponse:
        """Get an existing model.

        Args:
            model_name_or_id: name or id of the model to be retrieved.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The model of interest.
        """
        return self._get_resource(
            resource_id=model_name_or_id,
            route=MODELS,
            response_model=ModelResponse,
            params={"hydrate": hydrate},
        )

    def list_models(
        self,
        model_filter_model: ModelFilter,
        hydrate: bool = False,
    ) -> Page[ModelResponse]:
        """Get all models by filter.

        Args:
            model_filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A page of all models.
        """
        return self._list_paginated_resources(
            route=MODELS,
            response_model=ModelResponse,
            filter_model=model_filter_model,
            params={"hydrate": hydrate},
        )

    # ----------------------------- Model Versions -----------------------------

    def create_model_version(
        self, model_version: ModelVersionRequest
    ) -> ModelVersionResponse:
        """Creates a new model version.

        Args:
            model_version: the Model Version to be created.

        Returns:
            The newly created model version.
        """
        return self._create_workspace_scoped_resource(
            resource=model_version,
            response_model=ModelVersionResponse,
            route=f"{MODELS}/{model_version.model}{MODEL_VERSIONS}",
        )

    def delete_model_version(
        self,
        model_version_id: UUID,
    ) -> None:
        """Deletes a model version.

        Args:
            model_version_id: name or id of the model version to be deleted.
        """
        self._delete_resource(
            resource_id=model_version_id,
            route=f"{MODEL_VERSIONS}",
        )

    def get_model_version(
        self, model_version_id: UUID, hydrate: bool = True
    ) -> ModelVersionResponse:
        """Get an existing model version.

        Args:
            model_version_id: name, id, stage or number of the model version to
                be retrieved. If skipped - latest is retrieved.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The model version of interest.
        """
        return self._get_resource(
            resource_id=model_version_id,
            route=MODEL_VERSIONS,
            response_model=ModelVersionResponse,
            params={"hydrate": hydrate},
        )

    def list_model_versions(
        self,
        model_version_filter_model: ModelVersionFilter,
        model_name_or_id: Optional[Union[str, UUID]] = None,
        hydrate: bool = False,
    ) -> Page[ModelVersionResponse]:
        """Get all model versions by filter.

        Args:
            model_name_or_id: name or id of the model containing the model
                versions.
            model_version_filter_model: All filter parameters including
                pagination params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A page of all model versions.
        """
        if model_name_or_id:
            return self._list_paginated_resources(
                route=f"{MODELS}/{model_name_or_id}{MODEL_VERSIONS}",
                response_model=ModelVersionResponse,
                filter_model=model_version_filter_model,
                params={"hydrate": hydrate},
            )
        else:
            return self._list_paginated_resources(
                route=MODEL_VERSIONS,
                response_model=ModelVersionResponse,
                filter_model=model_version_filter_model,
                params={"hydrate": hydrate},
            )

    def update_model_version(
        self,
        model_version_id: UUID,
        model_version_update_model: ModelVersionUpdate,
    ) -> ModelVersionResponse:
        """Get all model versions by filter.

        Args:
            model_version_id: The ID of model version to be updated.
            model_version_update_model: The model version to be updated.

        Returns:
            An updated model version.

        """
        return self._update_resource(
            resource_id=model_version_id,
            resource_update=model_version_update_model,
            route=MODEL_VERSIONS,
            response_model=ModelVersionResponse,
        )

    # ------------------------ Model Versions Artifacts ------------------------

    def create_model_version_artifact_link(
        self, model_version_artifact_link: ModelVersionArtifactRequest
    ) -> ModelVersionArtifactResponse:
        """Creates a new model version link.

        Args:
            model_version_artifact_link: the Model Version to Artifact Link
                to be created.

        Returns:
            The newly created model version to artifact link.
        """
        return self._create_workspace_scoped_resource(
            resource=model_version_artifact_link,
            response_model=ModelVersionArtifactResponse,
            route=f"{MODEL_VERSIONS}/{model_version_artifact_link.model_version}{ARTIFACTS}",
        )

    def list_model_version_artifact_links(
        self,
        model_version_artifact_link_filter_model: ModelVersionArtifactFilter,
        hydrate: bool = False,
    ) -> Page[ModelVersionArtifactResponse]:
        """Get all model version to artifact links by filter.

        Args:
            model_version_artifact_link_filter_model: All filter parameters
                including pagination params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A page of all model version to artifact links.
        """
        return self._list_paginated_resources(
            route=MODEL_VERSION_ARTIFACTS,
            response_model=ModelVersionArtifactResponse,
            filter_model=model_version_artifact_link_filter_model,
            params={"hydrate": hydrate},
        )

    def delete_model_version_artifact_link(
        self,
        model_version_id: UUID,
        model_version_artifact_link_name_or_id: Union[str, UUID],
    ) -> None:
        """Deletes a model version to artifact link.

        Args:
            model_version_id: ID of the model version containing the link.
            model_version_artifact_link_name_or_id: name or ID of the model
                version to artifact link to be deleted.
        """
        self._delete_resource(
            resource_id=model_version_artifact_link_name_or_id,
            route=f"{MODEL_VERSIONS}/{model_version_id}{ARTIFACTS}",
        )

    def delete_all_model_version_artifact_links(
        self,
        model_version_id: UUID,
        only_links: bool = True,
    ) -> None:
        """Deletes all links between model version and an artifact.

        Args:
            model_version_id: ID of the model version containing the link.
            only_links: Flag deciding whether to delete only links or all.
        """
        self.delete(
            f"{MODEL_VERSIONS}/{model_version_id}{ARTIFACTS}",
            params={"only_links": only_links},
        )

    # ---------------------- Model Versions Pipeline Runs ----------------------

    def create_model_version_pipeline_run_link(
        self,
        model_version_pipeline_run_link: ModelVersionPipelineRunRequest,
    ) -> ModelVersionPipelineRunResponse:
        """Creates a new model version to pipeline run link.

        Args:
            model_version_pipeline_run_link: the Model Version to Pipeline Run
                Link to be created.

        Returns:
            - If Model Version to Pipeline Run Link already exists - returns
                the existing link.
            - Otherwise, returns the newly created model version to pipeline
                run link.
        """
        return self._create_workspace_scoped_resource(
            resource=model_version_pipeline_run_link,
            response_model=ModelVersionPipelineRunResponse,
            route=f"{MODEL_VERSIONS}/{model_version_pipeline_run_link.model_version}{RUNS}",
        )

    def list_model_version_pipeline_run_links(
        self,
        model_version_pipeline_run_link_filter_model: ModelVersionPipelineRunFilter,
        hydrate: bool = False,
    ) -> Page[ModelVersionPipelineRunResponse]:
        """Get all model version to pipeline run links by filter.

        Args:
            model_version_pipeline_run_link_filter_model: All filter parameters
                including pagination params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A page of all model version to pipeline run links.
        """
        return self._list_paginated_resources(
            route=MODEL_VERSION_PIPELINE_RUNS,
            response_model=ModelVersionPipelineRunResponse,
            filter_model=model_version_pipeline_run_link_filter_model,
            params={"hydrate": hydrate},
        )

    def delete_model_version_pipeline_run_link(
        self,
        model_version_id: UUID,
        model_version_pipeline_run_link_name_or_id: Union[str, UUID],
    ) -> None:
        """Deletes a model version to pipeline run link.

        Args:
            model_version_id: ID of the model version containing the link.
            model_version_pipeline_run_link_name_or_id: name or ID of the model version to pipeline run link to be deleted.
        """
        self._delete_resource(
            resource_id=model_version_pipeline_run_link_name_or_id,
            route=f"{MODEL_VERSIONS}/{model_version_id}{RUNS}",
        )

    # ---------------------------- Devices ----------------------------

    def get_authorized_device(
        self, device_id: UUID, hydrate: bool = True
    ) -> OAuthDeviceResponse:
        """Gets a specific OAuth 2.0 authorized device.

        Args:
            device_id: The ID of the device to get.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The requested device, if it was found.
        """
        return self._get_resource(
            resource_id=device_id,
            route=DEVICES,
            response_model=OAuthDeviceResponse,
            params={"hydrate": hydrate},
        )

    def list_authorized_devices(
        self, filter_model: OAuthDeviceFilter, hydrate: bool = False
    ) -> Page[OAuthDeviceResponse]:
        """List all OAuth 2.0 authorized devices for a user.

        Args:
            filter_model: All filter parameters including pagination
                params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A page of all matching OAuth 2.0 authorized devices.
        """
        return self._list_paginated_resources(
            route=DEVICES,
            response_model=OAuthDeviceResponse,
            filter_model=filter_model,
            params={"hydrate": hydrate},
        )

    def update_authorized_device(
        self, device_id: UUID, update: OAuthDeviceUpdate
    ) -> OAuthDeviceResponse:
        """Updates an existing OAuth 2.0 authorized device for internal use.

        Args:
            device_id: The ID of the device to update.
            update: The update to be applied to the device.

        Returns:
            The updated OAuth 2.0 authorized device.
        """
        return self._update_resource(
            resource_id=device_id,
            resource_update=update,
            response_model=OAuthDeviceResponse,
            route=DEVICES,
        )

    def delete_authorized_device(self, device_id: UUID) -> None:
        """Deletes an OAuth 2.0 authorized device.

        Args:
            device_id: The ID of the device to delete.
        """
        self._delete_resource(resource_id=device_id, route=DEVICES)

    # -------------------
    # Pipeline API Tokens
    # -------------------

    def get_api_token(
        self,
        pipeline_id: Optional[UUID] = None,
        schedule_id: Optional[UUID] = None,
        expires_minutes: Optional[int] = None,
    ) -> str:
        """Get an API token for a workload.

        Args:
            pipeline_id: The ID of the pipeline to get a token for.
            schedule_id: The ID of the schedule to get a token for.
            expires_minutes: The number of minutes for which the token should
                be valid. If not provided, the token will be valid indefinitely.

        Returns:
            The API token.

        Raises:
            ValueError: if the server response is not valid.
        """
        params: Dict[str, Any] = {}
        if pipeline_id:
            params["pipeline_id"] = pipeline_id
        if schedule_id:
            params["schedule_id"] = schedule_id
        if expires_minutes:
            params["expires_minutes"] = expires_minutes
        response_body = self.get(API_TOKEN, params=params)
        if not isinstance(response_body, str):
            raise ValueError(
                f"Bad API Response. Expected API token, got "
                f"{type(response_body)}"
            )
        return response_body

    #################
    # Tags
    #################

    def create_tag(self, tag: TagRequest) -> TagResponse:
        """Creates a new tag.

        Args:
            tag: the tag to be created.

        Returns:
            The newly created tag.
        """
        return self._create_resource(
            resource=tag,
            response_model=TagResponse,
            route=TAGS,
        )

    def delete_tag(
        self,
        tag_name_or_id: Union[str, UUID],
    ) -> None:
        """Deletes a tag.

        Args:
            tag_name_or_id: name or id of the tag to delete.
        """
        self._delete_resource(resource_id=tag_name_or_id, route=TAGS)

    def get_tag(
        self, tag_name_or_id: Union[str, UUID], hydrate: bool = True
    ) -> TagResponse:
        """Get an existing tag.

        Args:
            tag_name_or_id: name or id of the tag to be retrieved.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            The tag of interest.
        """
        return self._get_resource(
            resource_id=tag_name_or_id,
            route=TAGS,
            response_model=TagResponse,
            params={"hydrate": hydrate},
        )

    def list_tags(
        self,
        tag_filter_model: TagFilter,
        hydrate: bool = False,
    ) -> Page[TagResponse]:
        """Get all tags by filter.

        Args:
            tag_filter_model: All filter parameters including pagination params.
            hydrate: Flag deciding whether to hydrate the output model(s)
                by including metadata fields in the response.

        Returns:
            A page of all tags.
        """
        return self._list_paginated_resources(
            route=TAGS,
            response_model=TagResponse,
            filter_model=tag_filter_model,
            params={"hydrate": hydrate},
        )

    def update_tag(
        self,
        tag_name_or_id: Union[str, UUID],
        tag_update_model: TagUpdate,
    ) -> TagResponse:
        """Update tag.

        Args:
            tag_name_or_id: name or id of the tag to be updated.
            tag_update_model: Tag to use for the update.

        Returns:
            An updated tag.
        """
        tag = self.get_tag(tag_name_or_id)
        return self._update_resource(
            resource_id=tag.id,
            resource_update=tag_update_model,
            route=TAGS,
            response_model=TagResponse,
        )

    # =======================
    # Internal helper methods
    # =======================

    def _get_auth_token(self) -> str:
        """Get the authentication token for the REST store.

        Returns:
            The authentication token.

        Raises:
            ValueError: if the response from the server isn't in the right
                format.
        """
        if self._api_token is None:
            # Check if the API token is already stored in the config
            if self.config.api_token:
                self._api_token = self.config.api_token
            # Check if the username and password are provided in the config
            elif (
                self.config.username is not None
                and self.config.password is not None
                or self.config.api_key is not None
            ):
                data: Optional[Dict[str, str]] = None
                if self.config.api_key is not None:
                    data = {
                        "grant_type": OAuthGrantTypes.ZENML_API_KEY.value,
                        "password": self.config.api_key,
                    }
                elif (
                    self.config.username is not None
                    and self.config.password is not None
                ):
                    data = {
                        "grant_type": OAuthGrantTypes.OAUTH_PASSWORD.value,
                        "username": self.config.username,
                        "password": self.config.password,
                    }

                response = self._handle_response(
                    requests.post(
                        self.url + API + VERSION_1 + LOGIN,
                        data=data,
                        verify=self.config.verify_ssl,
                        timeout=self.config.http_timeout,
                    )
                )
                if (
                    not isinstance(response, dict)
                    or "access_token" not in response
                ):
                    raise ValueError(
                        f"Bad API Response. Expected access token dict, got "
                        f"{type(response)}"
                    )
                self._api_token = response["access_token"]
                self.config.api_token = self._api_token
            else:
                raise ValueError(
                    "No API token, API key or username/password provided. "
                    "Please provide either an API token, an API key or a "
                    "username and password in the ZenML config."
                )
        return self._api_token

    @property
    def session(self) -> requests.Session:
        """Authenticate to the ZenML server.

        Returns:
            A requests session with the authentication token.
        """
        if self._session is None:
            if self.config.verify_ssl is False:
                urllib3.disable_warnings(
                    urllib3.exceptions.InsecureRequestWarning
                )

            self._session = requests.Session()
            retries = Retry(backoff_factor=0.1, connect=5)
            self._session.mount("https://", HTTPAdapter(max_retries=retries))
            self._session.mount("http://", HTTPAdapter(max_retries=retries))
            self._session.verify = self.config.verify_ssl
            token = self._get_auth_token()
            self._session.headers.update({"Authorization": "Bearer " + token})
            logger.debug("Authenticated to ZenML server.")
        return self._session

    def clear_session(self) -> None:
        """Clear the authentication session and any cached API tokens."""
        self._session = None
        self._api_token = None
        # Clear the configured API token only if it's possible to fetch a new
        # one from the server using other credentials (username/password or
        # service account API key).
        if (
            self.config.username is not None
            and self.config.password is not None
            or self.config.api_key is not None
        ):
            self.config.api_token = None

    @staticmethod
    def _handle_response(response: requests.Response) -> Json:
        """Handle API response, translating http status codes to Exception.

        Args:
            response: The response to handle.

        Returns:
            The parsed response.

        Raises:
            ValueError: if the response is not in the right format.
            RuntimeError: if an error response is received from the server
                and a more specific exception cannot be determined.
            exc: the exception converted from an error response, if one
                is returned from the server.
        """
        if 200 <= response.status_code < 300:
            try:
                payload: Json = response.json()
                return payload
            except requests.exceptions.JSONDecodeError:
                raise ValueError(
                    "Bad response from API. Expected json, got\n"
                    f"{response.text}"
                )
        elif response.status_code >= 400:
            exc = exception_from_response(response)
            if exc is not None:
                raise exc
            else:
                raise RuntimeError(
                    f"{response.status_code} HTTP Error received from server: "
                    f"{response.text}"
                )
        else:
            raise RuntimeError(
                "Error retrieving from API. Got response "
                f"{response.status_code} with body:\n{response.text}"
            )

    def _request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Json:
        """Make a request to the REST API.

        Args:
            method: The HTTP method to use.
            url: The URL to request.
            params: The query parameters to pass to the endpoint.
            kwargs: Additional keyword arguments to pass to the request.

        Returns:
            The parsed response.

        Raises:
            AuthorizationException: if the request fails due to an expired
                authentication token.
        """
        params = {k: str(v) for k, v in params.items()} if params else {}

        self.session.headers.update(
            {source_context.name: source_context.get().value}
        )

        try:
            return self._handle_response(
                self.session.request(
                    method,
                    url,
                    params=params,
                    verify=self.config.verify_ssl,
                    timeout=self.config.http_timeout,
                    **kwargs,
                )
            )
        except AuthorizationException:
            # The authentication token could have expired; refresh it and try
            # again. This will clear any cached token and trigger a new
            # authentication flow.
            self.clear_session()
            logger.info("Authentication token expired; refreshing...")

        try:
            return self._handle_response(
                self.session.request(
                    method,
                    url,
                    params=params,
                    verify=self.config.verify_ssl,
                    timeout=self.config.http_timeout,
                    **kwargs,
                )
            )
        except AuthorizationException:
            logger.info(
                "Your authentication token has expired. Please re-authenticate."
            )
            raise

    def get(
        self, path: str, params: Optional[Dict[str, Any]] = None, **kwargs: Any
    ) -> Json:
        """Make a GET request to the given endpoint path.

        Args:
            path: The path to the endpoint.
            params: The query parameters to pass to the endpoint.
            kwargs: Additional keyword arguments to pass to the request.

        Returns:
            The response body.
        """
        logger.debug(f"Sending GET request to {path}...")
        return self._request(
            "GET", self.url + API + VERSION_1 + path, params=params, **kwargs
        )

    def delete(
        self, path: str, params: Optional[Dict[str, Any]] = None, **kwargs: Any
    ) -> Json:
        """Make a DELETE request to the given endpoint path.

        Args:
            path: The path to the endpoint.
            params: The query parameters to pass to the endpoint.
            kwargs: Additional keyword arguments to pass to the request.

        Returns:
            The response body.
        """
        logger.debug(f"Sending DELETE request to {path}...")
        return self._request(
            "DELETE",
            self.url + API + VERSION_1 + path,
            params=params,
            **kwargs,
        )

    def post(
        self,
        path: str,
        body: BaseModel,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Json:
        """Make a POST request to the given endpoint path.

        Args:
            path: The path to the endpoint.
            body: The body to send.
            params: The query parameters to pass to the endpoint.
            kwargs: Additional keyword arguments to pass to the request.

        Returns:
            The response body.
        """
        logger.debug(f"Sending POST request to {path}...")
        return self._request(
            "POST",
            self.url + API + VERSION_1 + path,
            data=body.json(),
            params=params,
            **kwargs,
        )

    def put(
        self,
        path: str,
        body: Optional[BaseModel] = None,
        params: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Json:
        """Make a PUT request to the given endpoint path.

        Args:
            path: The path to the endpoint.
            body: The body to send.
            params: The query parameters to pass to the endpoint.
            kwargs: Additional keyword arguments to pass to the request.

        Returns:
            The response body.
        """
        logger.debug(f"Sending PUT request to {path}...")
        data = body.json(exclude_unset=True) if body else None
        return self._request(
            "PUT",
            self.url + API + VERSION_1 + path,
            data=data,
            params=params,
            **kwargs,
        )

    def _create_resource(
        self,
        resource: AnyRequest,
        response_model: Type[AnyResponse],
        route: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> AnyResponse:
        """Create a new resource.

        Args:
            resource: The resource to create.
            route: The resource REST API route to use.
            response_model: Optional model to use to deserialize the response
                body. If not provided, the resource class itself will be used.
            params: Optional query parameters to pass to the endpoint.

        Returns:
            The created resource.
        """
        response_body = self.post(f"{route}", body=resource, params=params)
        return response_model.parse_obj(response_body)

    def _create_workspace_scoped_resource(
        self,
        resource: AnyWorkspaceScopedRequest,
        response_model: Type[AnyResponse],
        route: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> AnyResponse:
        """Create a new workspace scoped resource.

        Args:
            resource: The resource to create.
            route: The resource REST API route to use.
            response_model: Optional model to use to deserialize the response
                body. If not provided, the resource class itself will be used.
            params: Optional query parameters to pass to the endpoint.

        Returns:
            The created resource.
        """
        return self._create_resource(
            resource=resource,
            response_model=response_model,
            route=f"{WORKSPACES}/{str(resource.workspace)}{route}",
            params=params,
        )

    def _get_or_create_resource(
        self,
        resource: AnyRequest,
        response_model: Type[AnyResponse],
        route: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[AnyResponse, bool]:
        """Get or create a resource.

        Args:
            resource: The resource to get or create.
            route: The resource REST API route to use.
            response_model: Optional model to use to deserialize the response
                body. If not provided, the resource class itself will be used.
            params: Optional query parameters to pass to the endpoint.

        Returns:
            The created resource, and a boolean indicating whether the resource
            was created or not.

        Raises:
            ValueError: If the response body is not a list with 2 elements
                where the first element is the resource and the second element
                a boolean indicating whether the resource was created or not.
        """
        response_body = self.post(
            f"{route}{GET_OR_CREATE}",
            body=resource,
            params=params,
        )
        if not isinstance(response_body, list):
            raise ValueError(
                f"Expected a list response from the {route}{GET_OR_CREATE} "
                f"endpoint but got {type(response_body)} instead."
            )
        if len(response_body) != 2:
            raise ValueError(
                f"Expected a list response with 2 elements from the "
                f"{route}{GET_OR_CREATE} endpoint but got {len(response_body)} "
                f"elements instead."
            )
        model_json, was_created = response_body
        if not isinstance(was_created, bool):
            raise ValueError(
                f"Expected a boolean as the second element of the list "
                f"response from the {route}{GET_OR_CREATE} endpoint but got "
                f"{type(was_created)} instead."
            )
        return response_model.parse_obj(model_json), was_created

    def _get_or_create_workspace_scoped_resource(
        self,
        resource: AnyWorkspaceScopedRequest,
        response_model: Type[AnyResponse],
        route: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[AnyResponse, bool]:
        """Get or create a workspace scoped resource.

        Args:
            resource: The resource to get or create.
            route: The resource REST API route to use.
            response_model: Optional model to use to deserialize the response
                body. If not provided, the resource class itself will be used.
            params: Optional query parameters to pass to the endpoint.

        Returns:
            The created resource, and a boolean indicating whether the resource
            was created or not.
        """
        return self._get_or_create_resource(
            resource=resource,
            response_model=response_model,
            route=f"{WORKSPACES}/{str(resource.workspace)}{route}",
            params=params,
        )

    def _get_resource(
        self,
        resource_id: Union[str, int, UUID],
        route: str,
        response_model: Type[AnyResponse],
        params: Optional[Dict[str, Any]] = None,
    ) -> AnyResponse:
        """Retrieve a single resource.

        Args:
            resource_id: The ID of the resource to retrieve.
            route: The resource REST API route to use.
            response_model: Model to use to serialize the response body.
            params: Optional query parameters to pass to the endpoint.

        Returns:
            The retrieved resource.
        """
        body = self.get(f"{route}/{str(resource_id)}", params=params)
        return response_model.parse_obj(body)

    def _list_paginated_resources(
        self,
        route: str,
        response_model: Type[AnyResponse],
        filter_model: BaseFilter,
        params: Optional[Dict[str, Any]] = None,
    ) -> Page[AnyResponse]:
        """Retrieve a list of resources filtered by some criteria.

        Args:
            route: The resource REST API route to use.
            response_model: Model to use to serialize the response body.
            filter_model: The filter model to use for the list query.
            params: Optional query parameters to pass to the endpoint.

        Returns:
            List of retrieved resources matching the filter criteria.

        Raises:
            ValueError: If the value returned by the server is not a list.
        """
        # leave out filter params that are not supplied
        params = params or {}
        params.update(filter_model.dict(exclude_none=True))
        body = self.get(f"{route}", params=params)
        if not isinstance(body, dict):
            raise ValueError(
                f"Bad API Response. Expected list, got {type(body)}"
            )
        # The initial page of items will be of type BaseResponseModel
        page_of_items: Page[AnyResponse] = Page.parse_obj(body)
        # So these items will be parsed into their correct types like here
        page_of_items.items = [
            response_model.parse_obj(generic_item)
            for generic_item in body["items"]
        ]
        return page_of_items

    def _list_resources(
        self,
        route: str,
        response_model: Type[AnyResponse],
        **filters: Any,
    ) -> List[AnyResponse]:
        """Retrieve a list of resources filtered by some criteria.

        Args:
            route: The resource REST API route to use.
            response_model: Model to use to serialize the response body.
            filters: Filter parameters to use in the query.

        Returns:
            List of retrieved resources matching the filter criteria.

        Raises:
            ValueError: If the value returned by the server is not a list.
        """
        # leave out filter params that are not supplied
        params = dict(filter(lambda x: x[1] is not None, filters.items()))
        body = self.get(f"{route}", params=params)
        if not isinstance(body, list):
            raise ValueError(
                f"Bad API Response. Expected list, got {type(body)}"
            )
        return [response_model.parse_obj(entry) for entry in body]

    def _update_resource(
        self,
        resource_id: Union[str, int, UUID],
        resource_update: BaseModel,
        response_model: Type[AnyResponse],
        route: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> AnyResponse:
        """Update an existing resource.

        Args:
            resource_id: The id of the resource to update.
            resource_update: The resource update.
            response_model: Optional model to use to deserialize the response
                body. If not provided, the resource class itself will be used.
            route: The resource REST API route to use.
            params: Optional query parameters to pass to the endpoint.

        Returns:
            The updated resource.
        """
        response_body = self.put(
            f"{route}/{str(resource_id)}", body=resource_update, params=params
        )

        return response_model.parse_obj(response_body)

    def _delete_resource(
        self, resource_id: Union[str, UUID], route: str
    ) -> None:
        """Delete a resource.

        Args:
            resource_id: The ID of the resource to delete.
            route: The resource REST API route to use.
        """
        self.delete(f"{route}/{str(resource_id)}")
