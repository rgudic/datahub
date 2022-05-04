import logging
from typing import Dict, Optional

import pydantic
import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from snowflake.connector.network import (
    DEFAULT_AUTHENTICATOR,
    EXTERNAL_BROWSER_AUTHENTICATOR,
    KEY_PAIR_AUTHENTICATOR,
    OAUTH_AUTHENTICATOR,
)

from datahub.configuration.common import (
    AllowDenyPattern,
    ConfigModel,
    ConfigurationError,
)
from datahub.configuration.time_window_config import BaseTimeWindowConfig
from datahub.ingestion.source.sql.oauth_generator import OauthTokenGenerator
from datahub.ingestion.source.sql.sql_common import (
    SQLAlchemyConfig,
    make_sqlalchemy_uri,
)
from datahub.utilities.config_clean import (
    remove_protocol,
    remove_suffix,
    remove_trailing_slashes,
)

logger: logging.Logger = logging.getLogger(__name__)

APPLICATION_NAME: str = "acryl_datahub"

VALID_AUTH_TYPES: Dict[str, str] = {
    "DEFAULT_AUTHENTICATOR": DEFAULT_AUTHENTICATOR,
    "EXTERNAL_BROWSER_AUTHENTICATOR": EXTERNAL_BROWSER_AUTHENTICATOR,
    "KEY_PAIR_AUTHENTICATOR": KEY_PAIR_AUTHENTICATOR,
    "OAUTH_AUTHENTICATOR": OAUTH_AUTHENTICATOR,
}


class SnowflakeProvisionRoleConfig(ConfigModel):
    enabled: bool = False

    # Can be used by account admin to test what sql statements will be run
    dry_run: bool = False

    # Setting this to True is helpful in case you want a clean role without any extra privileges
    # Not set to True by default because multiple parallel
    #   snowflake ingestions can be dependent on single role
    drop_role_if_exists: bool = False

    # When Account admin is testing they might not want to actually do the ingestion
    # Set this to False in case the account admin would want to
    #   create role
    #   grant role to user in main config
    #   run ingestion as the user in main config
    run_ingestion: bool = False

    admin_role: Optional[str] = "accountadmin"

    admin_username: str
    admin_password: pydantic.SecretStr = pydantic.Field(default=None, exclude=True)

    @pydantic.validator("admin_username", always=True)
    def username_not_empty(cls, v, values, **kwargs):
        v_str: str = str(v)
        if v_str.strip() == "":
            raise ValueError("username is empty")
        return v


class BaseSnowflakeConfig(BaseTimeWindowConfig):
    # Note: this config model is also used by the snowflake-usage source.

    scheme: str = "snowflake"
    username: Optional[str] = None
    use_certificate: Optional[bool] = False
    oauth_client_id: Optional[str]
    oauth_scope: Optional[str]
    oauth_client_secret: Optional[str]
    oauth_authority_url: Optional[str]
    base64_encoded_oauth_public_key: Optional[str]
    base64_encoded_oauth_private_key: Optional[str]
    password: Optional[pydantic.SecretStr] = pydantic.Field(default=None, exclude=True)
    private_key_path: Optional[str]
    private_key_password: Optional[pydantic.SecretStr] = pydantic.Field(
        default=None, exclude=True
    )
    authentication_type: str = "DEFAULT_AUTHENTICATOR"
    host_port: Optional[str]  # Deprecated
    account_id: Optional[str]  # Once host_port is removed this will be made mandatory
    warehouse: Optional[str]
    role: Optional[str]
    include_table_lineage: bool = True
    include_view_lineage: bool = True
    connect_args: Optional[Dict] = pydantic.Field(default=None, exclude=True)
    check_role_grants: bool = False

    def get_account(self) -> str:
        assert self.account_id
        return self.account_id

    @pydantic.root_validator
    def one_of_host_port_or_account_id_is_required(cls, values):
        host_port = values.get("host_port")
        if host_port is not None:
            logger.warning(
                "snowflake's `host_port` option has been deprecated; use account_id instead"
            )
            host_port = remove_protocol(host_port)
            host_port = remove_trailing_slashes(host_port)
            host_port = remove_suffix(host_port, ".snowflakecomputing.com")
            values["host_port"] = host_port
        account_id = values.get("account_id")
        if account_id is None:
            if host_port is None:
                raise ConfigurationError(
                    "One of account_id (recommended) or host_port (deprecated) is required"
                )
            else:
                values["account_id"] = host_port
        return values

    @pydantic.validator("authentication_type", always=True)
    def authenticator_type_is_valid(cls, v, values, **kwargs):
        if v not in VALID_AUTH_TYPES.keys():
            raise ValueError(
                f"unsupported authenticator type '{v}' was provided,"
                f" use one of {list(VALID_AUTH_TYPES.keys())}"
            )
        else:
            if v == "KEY_PAIR_AUTHENTICATOR":
                # If we are using key pair auth, we need the private key path and password to be set
                if values.get("private_key_path") is None:
                    raise ValueError(
                        f"'private_key_path' was none "
                        f"but should be set when using {v} authentication"
                    )
            elif v == "OAUTH_AUTHENTICATOR":
                if values.get("oauth_client_id") is None:
                    raise ValueError(
                        f"'oauth_client_id' is none "
                        f"but should be set when using {v} authentication"
                    )
                if values.get("oauth_scope") is None:
                    raise ValueError(
                        f"'oauth_scope' was none "
                        f"but should be set when using {v} authentication"
                    )
                if values.get("oauth_authority_url") is None:
                    raise ValueError(
                        f"'oauth_authority_url' was none "
                        f"but should be set when using {v} authentication"
                    )
                if values.get("use_certificate") is True:
                    if values.get("base64_encoded_oauth_private_key") is None:
                        raise ValueError(
                            f"'base64_encoded_oauth_private_key' was none "
                            f"but should be set when using {v} authentication"
                        )
                    if values.get("base64_encoded_oauth_public_key") is None:
                        raise ValueError(
                            f"'base64_encoded_oauth_public_key' was none"
                            f"but should be set when using {v} authentication"
                        )
                else:
                    if values.get("oauth_client_secret") is None:
                        raise ValueError(
                            f"'oauth_client_secret' was none "
                            f"but should be set when using {v} authentication"
                        )
            logger.info(f"using authenticator type '{v}'")
        return v

    @pydantic.validator("include_view_lineage")
    def validate_include_view_lineage(cls, v, values):
        if not values.get("include_table_lineage") and v:
            raise ValueError(
                "include_table_lineage must be True for include_view_lineage to be set."
            )
        return v

    def get_oauth_connection(self):
        generator = OauthTokenGenerator(self.oauth_client_id, self.oauth_authority_url)
        if self.use_certificate is True:
            response = generator.get_token_with_certificate(
                private_key_content=str(self.base64_encoded_oauth_private_key),
                public_key_content=str(self.base64_encoded_oauth_public_key),
                scope=str(self.oauth_scope),
            )
        else:
            response = generator.get_token_with_secret(
                secret=str(self.oauth_client_secret), scope=str(self.oauth_scope)
            )
        token = response["access_token"]

        return snowflake.connector.connect(
            user=self.username,
            account=self.host_port,
            authenticator="oauth",
            token=token,
            warehouse=self.warehouse,
        )

    def get_sql_alchemy_url(
        self,
        database: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[pydantic.SecretStr] = None,
        role: Optional[str] = None,
    ) -> str:
        if username is None:
            username = self.username
        if password is None:
            password = self.password
        if role is None:
            role = self.role
        return make_sqlalchemy_uri(
            self.scheme,
            username,
            password.get_secret_value() if password else None,
            self.account_id,
            f'"{database}"' if database is not None else database,
            uri_opts={
                # Drop the options if value is None.
                key: value
                for (key, value) in {
                    "authenticator": VALID_AUTH_TYPES.get(self.authentication_type),
                    "warehouse": self.warehouse,
                    "role": role,
                    "application": APPLICATION_NAME,
                }.items()
                if value
            },
        )

    def get_sql_alchemy_connect_args(self) -> dict:
        if self.authentication_type != "KEY_PAIR_AUTHENTICATOR":
            return {}
        if self.connect_args is None:
            if self.private_key_path is None:
                raise ValueError("missing required private key path to read key from")
            if self.private_key_password is None:
                raise ValueError("missing required private key password")
            with open(self.private_key_path, "rb") as key:
                p_key = serialization.load_pem_private_key(
                    key.read(),
                    password=self.private_key_password.get_secret_value().encode(),
                    backend=default_backend(),
                )

            pkb = p_key.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            self.connect_args = {"private_key": pkb}
        return self.connect_args


class SnowflakeConfig(BaseSnowflakeConfig, SQLAlchemyConfig):
    database_pattern: AllowDenyPattern = AllowDenyPattern(
        deny=[r"^UTIL_DB$", r"^SNOWFLAKE$", r"^SNOWFLAKE_SAMPLE_DATA$"]
    )

    provision_role: Optional[SnowflakeProvisionRoleConfig] = None
    ignore_start_time_lineage: bool = False
    upstream_lineage_in_report: bool = False

    def get_sql_alchemy_url(
        self,
        database: str = None,
        username: Optional[str] = None,
        password: Optional[pydantic.SecretStr] = None,
        role: Optional[str] = None,
    ) -> str:
        return super().get_sql_alchemy_url(
            database=database, username=username, password=password, role=role
        )

    def get_sql_alchemy_connect_args(self) -> dict:
        return super().get_sql_alchemy_connect_args()
