"""
Universe configuration builder.
"""
# absolute_import needed for tool_shed package.

import collections
import errno
import ipaddress
import logging
import logging.config
import os
import re
import signal
import socket
import string
import sys
import tempfile
import threading
import time
from datetime import timedelta

import yaml
from beaker.cache import CacheManager
from beaker.util import parse_cache_config_options
from six.moves import configparser

from galaxy.config.schema import AppSchema
from galaxy.containers import parse_containers_config
from galaxy.exceptions import ConfigurationError
from galaxy.model import mapping
from galaxy.model.tool_shed_install.migrate.check import create_or_verify_database as tsi_create_or_verify_database
from galaxy.util import (
    ExecutionTimer,
    listify,
    string_as_bool,
    unicodify,
)
from galaxy.util.custom_logging import LOGLV_TRACE
from galaxy.util.dbkeys import GenomeBuilds
from galaxy.util.properties import (
    find_config_file,
    read_properties_from_file,
    running_from_source,
)
from galaxy.web.formatting import expand_pretty_datetime_format
from galaxy.web_stack import (
    get_stack_facts,
    register_postfork_function
)
from ..version import VERSION_MAJOR

log = logging.getLogger(__name__)

GALAXY_APP_NAME = 'galaxy'
GALAXY_CONFIG_SCHEMA_PATH = 'lib/galaxy/webapps/galaxy/config_schema.yml'
LOGGING_CONFIG_DEFAULT = {
    'disable_existing_loggers': False,
    'version': 1,
    'root': {
        'handlers': ['console'],
        'level': 'DEBUG',
    },
    'loggers': {
        'paste.httpserver.ThreadPool': {
            'level': 'WARN',
            'qualname': 'paste.httpserver.ThreadPool',
        },
        'routes.middleware': {
            'level': 'WARN',
            'qualname': 'routes.middleware',
        },
        'amqp': {
            'level': 'INFO',
            'qualname': 'amqp',
        },
        'dogpile': {
            'level': 'WARN',
            'qualname': 'dogpile',
        },
    },
    'filters': {
        'stack': {
            '()': 'galaxy.web_stack.application_stack_log_filter',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'stack',
            'level': 'DEBUG',
            'stream': 'ext://sys.stderr',
            'filters': ['stack'],
        },
    },
    'formatters': {
        'stack': {
            '()': 'galaxy.web_stack.application_stack_log_formatter',
        },
    },
}
"""Default value for logging configuration, passed to :func:`logging.config.dictConfig`"""


def find_root(kwargs):
    return os.path.abspath(kwargs.get('root_dir', '.'))


class BaseAppConfiguration:
    # Override in subclasses (optional): {KEY: config option, VALUE: deprecated directory name}
    # If VALUE == first directory in a user-supplied path that resolves to KEY, it will be stripped from that path
    renamed_options = None
    deprecated_dirs = None

    def __init__(self, **kwargs):
        self._process_renamed_options(kwargs)
        self._kwargs = kwargs  # Save these as a record of explicitly set options
        self.config_dict = kwargs
        self.root = find_root(kwargs)
        self._set_config_base(kwargs)
        self.schema = self._load_schema()  # Load schema from schema definition file
        self._raw_config = self.schema.defaults.copy()  # Save schema defaults as initial config values (raw_config)
        self._update_raw_config_from_kwargs(kwargs)  # Overwrite raw_config with values passed in kwargs
        self._create_attributes_from_raw_config()  # Create attributes based on raw_config
        self._resolve_paths()  # Overwrite attribute values with resolved paths

    def _process_renamed_options(self, kwargs):
        """Update kwargs to set any unset renamed options to values of old-named options, if set.

        Does not remove the old options from kwargs so that deprecated option usage can be logged.
        """
        if self.renamed_options is not None:
            for old, new in self.renamed_options.items():
                if old in kwargs and new not in kwargs:
                    kwargs[new] = kwargs[old]

    def is_set(self, key):
        """Check if a configuration option has been explicitly set."""
        # NOTE: This will check all supplied keyword arguments, including those not in the schema.
        # To check only schema options, change the line below to `if property not in self._raw_config:`
        if key not in self._raw_config:
            log.warning("Configuration option does not exist: '%s'" % key)
        return key in self._kwargs

    def resolve_path(self, path):
        """Resolve a path relative to Galaxy's root."""
        return self._in_root_dir(path)

    def _set_config_base(self, config_kwargs):

        def _set_global_conf():
            self.config_file = find_config_file('galaxy')
            self.global_conf = config_kwargs.get('global_conf')
            self.global_conf_parser = configparser.ConfigParser()
            if not self.config_file and self.global_conf and "__file__" in self.global_conf:
                self.config_file = os.path.join(self.root, self.global_conf['__file__'])

            if self.config_file is None:
                log.warning("No Galaxy config file found, running from current working directory: %s", os.getcwd())
            else:
                try:
                    self.global_conf_parser.read(self.config_file)
                except OSError:
                    raise
                except Exception:
                    pass  # Not an INI file

        def _set_config_directories():
            # Set config_dir to value from kwargs OR dirname of config_file OR None
            _config_dir = os.path.dirname(self.config_file) if self.config_file else None
            self.config_dir = config_kwargs.get('config_dir', _config_dir)
            # Make path absolute before using it as base for other paths
            if self.config_dir:
                self.config_dir = os.path.abspath(self.config_dir)

            self.data_dir = config_kwargs.get('data_dir')
            if self.data_dir:
                self.data_dir = os.path.abspath(self.data_dir)

            self.sample_config_dir = os.path.join(os.path.dirname(__file__), 'sample')

            self.managed_config_dir = config_kwargs.get('managed_config_dir')
            if self.managed_config_dir:
                self.managed_config_dir = os.path.abspath(self.managed_config_dir)

            if running_from_source:
                if not self.config_dir:
                    self.config_dir = os.path.join(self.root, 'config')
                if not self.data_dir:
                    self.data_dir = os.path.join(self.root, 'database')
                if not self.managed_config_dir:
                    self.managed_config_dir = self.config_dir
            else:
                if not self.config_dir:
                    self.config_dir = os.getcwd()
                if not self.data_dir:
                    self.data_dir = self._in_config_dir('data')
                if not self.managed_config_dir:
                    self.managed_config_dir = self._in_data_dir('config')

            # TODO: do we still need to support ../shed_tools when running_from_source?
            self.shed_tools_dir = self._in_data_dir('shed_tools')

            log.debug("Configuration directory is %s", self.config_dir)
            log.debug("Data directory is %s", self.data_dir)
            log.debug("Managed config directory is %s", self.managed_config_dir)

        _set_global_conf()
        _set_config_directories()

    def _load_schema(self):
        # Override in subclasses
        raise Exception('Not implemented')

    def _update_raw_config_from_kwargs(self, kwargs):

        def convert_datatype(key, value):
            datatype = self.schema.app_schema[key].get('type')
            # check for `not None` explicitly (value can be falsy)
            if value is not None and datatype in type_converters:
                return type_converters[datatype](value)
            return value

        def strip_deprecated_dir(key, value):
            resolves_to = self.schema.paths_to_resolve.get(key)
            if resolves_to:  # value is a path that will be resolved
                first_dir = value.split(os.sep)[0]  # get first directory component
                if first_dir == self.deprecated_dirs.get(resolves_to):  # first_dir is deprecated for this option
                    ignore = first_dir + os.sep
                    log.warning(
                        "Paths for the '%s' option are now relative to '%s', remove the leading '%s' "
                        "to suppress this warning: %s", key, resolves_to, ignore, value
                    )
                    return value[len(ignore):]
            return value

        type_converters = {'bool': string_as_bool, 'int': int, 'float': float, 'str': str}

        for key, value in kwargs.items():
            if key in self.schema.app_schema:
                value = convert_datatype(key, value)
                if value and self.deprecated_dirs:
                    value = strip_deprecated_dir(key, value)
                self._raw_config[key] = value

    def _create_attributes_from_raw_config(self):
        # `base_configs` are a special case: these attributes have been created and will be ignored
        # by the code below. Trying to overwrite any other existing attributes will raise an error.
        base_configs = {'config_dir', 'data_dir', 'managed_config_dir'}
        for key, value in self._raw_config.items():
            if not hasattr(self, key):
                setattr(self, key, value)
            elif key not in base_configs:
                raise ConfigurationError("Attempting to override existing attribute '%s'" % key)

    def _resolve_paths(self):

        def resolve(key):
            if key in _cache:  # resolve each path only once
                return _cache[key]

            path = getattr(self, key)  # path prior to being resolved
            parent = self.schema.paths_to_resolve.get(key)
            if not parent:  # base case: nothing else needs resolving
                return path
            parent_path = resolve(parent)  # recursively resolve parent path
            if path is not None:
                path = os.path.join(parent_path, path)  # resolve path
            else:
                path = parent_path  # or use parent path

            setattr(self, key, path)  # update property
            _cache[key] = path  # cache it!
            return path

        _cache = {}
        for key in self.schema.paths_to_resolve:
            resolve(key)

    def _in_root_dir(self, path):
        return self._in_dir(self.root, path)

    def _in_managed_config_dir(self, path):
        return self._in_dir(self.managed_config_dir, path)

    def _in_config_dir(self, path):
        return self._in_dir(self.config_dir, path)

    def _in_sample_dir(self, path):
        return self._in_dir(self.sample_config_dir, path)

    def _in_data_dir(self, path):
        return self._in_dir(self.data_dir, path)

    def _in_dir(self, _dir, path):
        return os.path.join(_dir, path) if path else None

    def _parse_config_file_options(self, defaults, listify_defaults, config_kwargs):

        def root_join(val):
            try:
                return os.path.join(self.root, val)
            except TypeError:
                template = "Failed to set configuration variable %s, value %s of wrong type %s"
                message = template % (var, val, type(val))
                raise ConfigurationError(message)

        for var, values in defaults.items():
            if config_kwargs.get(var) is not None:
                path = config_kwargs.get(var)
                setattr(self, var + '_set', True)
            else:
                for value in values:
                    if os.path.exists(value):
                        path = value
                        break
                else:
                    path = values[-1]
                setattr(self, var + '_set', False)
            setattr(self, var, root_join(path))

        for var, values in listify_defaults.items():
            paths = []
            if config_kwargs.get(var) is not None:
                paths = listify(config_kwargs.get(var))
                setattr(self, var + '_set', True)
            else:
                for value in values:
                    for path in listify(value):
                        if not os.path.exists(path):
                            break
                    else:
                        paths = listify(value)
                        break
                else:
                    paths = listify(values[-1])
                setattr(self, var + '_set', False)
            setattr(self, var, [root_join(x) for x in paths])


class CommonConfigurationMixin:
    """Shared configuration settings code for Galaxy and ToolShed."""

    @property
    def admin_users(self):
        return self._admin_users

    @admin_users.setter
    def admin_users(self, value):
        self._admin_users = value
        if value:
            self.admin_users_list = [u.strip() for u in value.split(',') if u]
        else:  # provide empty list for convenience (check membership, etc.)
            self.admin_users_list = []

    def is_admin_user(self, user):
        """Determine if the provided user is listed in `admin_users`."""
        return user is not None and user.email in self.admin_users_list

    @property
    def sentry_dsn_public(self):
        """
        Sentry URL with private key removed for use in client side scripts,
        sentry server will need to be configured to accept events
        """
        if self.sentry_dsn:
            return re.sub(r"^([^:/?#]+:)?//(\w+):(\w+)", r"\1//\2", self.sentry_dsn)

    def get_bool(self, key, default):
        # Warning: the value of self.config_dict['foo'] may be different from self.foo
        if key in self.config_dict:
            return string_as_bool(self.config_dict[key])
        else:
            return default

    def get(self, key, default=None):
        # Warning: the value of self.config_dict['foo'] may be different from self.foo
        return self.config_dict.get(key, default)

    def _ensure_directory(self, path):
        if path not in [None, False] and not os.path.isdir(path):
            try:
                os.makedirs(path)
            except Exception as e:
                raise ConfigurationError("Unable to create missing directory: {}\n{}".format(path, unicodify(e)))


class GalaxyAppConfiguration(BaseAppConfiguration, CommonConfigurationMixin):
    deprecated_options = ('database_file', 'track_jobs_in_database', 'blacklist_file', 'whitelist_file',
                          'sanitize_whitelist_file', 'user_library_import_symlink_whitelist', 'fetch_url_whitelist')
    renamed_options = {
        'blacklist_file': 'email_domain_blocklist_file',
        'whitelist_file': 'email_domain_allowlist_file',
        'sanitize_whitelist_file': 'sanitize_allowlist_file',
        'user_library_import_symlink_whitelist': 'user_library_import_symlink_allowlist',
        'fetch_url_whitelist': 'fetch_url_allowlist',
    }
    default_config_file_name = 'galaxy.yml'
    deprecated_dirs = {'config_dir': 'config', 'data_dir': 'database'}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._override_tempdir(kwargs)
        self._process_config(kwargs)

    def _load_schema(self):
        return AppSchema(GALAXY_CONFIG_SCHEMA_PATH, GALAXY_APP_NAME)

    def _override_tempdir(self, kwargs):
        if string_as_bool(kwargs.get("override_tempdir", "True")):
            tempfile.tempdir = self.new_file_path

    def _process_config(self, kwargs):
        # Resolve paths of other config files
        self.parse_config_file_options(kwargs)

        # Collect the umask and primary gid from the environment
        self.umask = os.umask(0o77)  # get the current umask
        os.umask(self.umask)  # can't get w/o set, so set it back
        self.gid = os.getgid()  # if running under newgrp(1) we'll need to fix the group of data created on the cluster

        self.version_major = VERSION_MAJOR

        # Database related configuration
        self.check_migrate_databases = kwargs.get('check_migrate_databases', True)
        if not self.database_connection:  # Provide default if not supplied by user
            db_path = self._in_data_dir('universe.sqlite')
            self.database_connection = 'sqlite:///%s?isolation_level=IMMEDIATE' % db_path
        self.database_engine_options = get_database_engine_options(kwargs)
        self.database_create_tables = string_as_bool(kwargs.get('database_create_tables', 'True'))
        self.database_encoding = kwargs.get('database_encoding')  # Create new databases with this encoding
        self.thread_local_log = None
        if self.enable_per_request_sql_debugging:
            self.thread_local_log = threading.local()
        # Install database related configuration (if different)
        self.install_database_engine_options = get_database_engine_options(kwargs, model_prefix="install_")
        self.shared_home_dir = kwargs.get("shared_home_dir")
        self.cookie_path = kwargs.get("cookie_path")
        self.tool_path = self._in_root_dir(self.tool_path)
        self.tool_data_path = self._in_root_dir(self.tool_data_path)
        if not running_from_source and kwargs.get("tool_data_path") is None:
            self.tool_data_path = self._in_data_dir(self.schema.defaults['tool_data_path'])
        self.builds_file_path = os.path.join(self.tool_data_path, self.builds_file_path)
        self.len_file_path = os.path.join(self.tool_data_path, self.len_file_path)
        # Galaxy OIDC settings.
        self.oidc_config = kwargs.get("oidc_config_file", self.oidc_config_file)
        self.oidc_backends_config = kwargs.get("oidc_backends_config_file", self.oidc_backends_config_file)
        self.oidc = {}
        self.integrated_tool_panel_config = self._in_managed_config_dir(self.integrated_tool_panel_config)
        integrated_tool_panel_tracking_directory = kwargs.get('integrated_tool_panel_tracking_directory')
        if integrated_tool_panel_tracking_directory:
            self.integrated_tool_panel_tracking_directory = self._in_root_dir(integrated_tool_panel_tracking_directory)
        else:
            self.integrated_tool_panel_tracking_directory = None
        self.toolbox_filter_base_modules = listify(self.toolbox_filter_base_modules)
        self.tool_filters = listify(self.tool_filters, do_strip=True)
        self.tool_label_filters = listify(self.tool_label_filters, do_strip=True)
        self.tool_section_filters = listify(self.tool_section_filters, do_strip=True)

        self.user_tool_filters = listify(self.user_tool_filters, do_strip=True)
        self.user_tool_label_filters = listify(self.user_tool_label_filters, do_strip=True)
        self.user_tool_section_filters = listify(self.user_tool_section_filters, do_strip=True)
        self.has_user_tool_filters = bool(self.user_tool_filters or self.user_tool_label_filters or self.user_tool_section_filters)

        self.password_expiration_period = timedelta(days=int(self.password_expiration_period))

        if self.shed_tool_data_path:
            self.shed_tool_data_path = self._in_root_dir(self.shed_tool_data_path)
        else:
            self.shed_tool_data_path = self.tool_data_path

        self.running_functional_tests = string_as_bool(kwargs.get('running_functional_tests', False))
        if isinstance(self.hours_between_check, str):
            self.hours_between_check = float(self.hours_between_check)
        try:
            if isinstance(self.hours_between_check, int):
                if self.hours_between_check < 1 or self.hours_between_check > 24:
                    self.hours_between_check = 12
            elif isinstance(self.hours_between_check, float):
                # If we're running functional tests, the minimum hours between check should be reduced to 0.001, or 3.6 seconds.
                if self.running_functional_tests:
                    if self.hours_between_check < 0.001 or self.hours_between_check > 24.0:
                        self.hours_between_check = 12.0
                else:
                    if self.hours_between_check < 1.0 or self.hours_between_check > 24.0:
                        self.hours_between_check = 12.0
            else:
                self.hours_between_check = 12
        except Exception:
            self.hours_between_check = 12
        self.update_integrated_tool_panel = kwargs.get("update_integrated_tool_panel", True)
        self.galaxy_data_manager_data_path = self.galaxy_data_manager_data_path or self.tool_data_path
        self.tool_secret = kwargs.get("tool_secret", "")
        self.metadata_strategy = kwargs.get("metadata_strategy", "directory")
        self.use_remote_user = self.use_remote_user or self.single_user
        self.fetch_url_allowlist_ips = [
            ipaddress.ip_network(unicodify(ip.strip()))  # If it has a slash, assume 127.0.0.1/24 notation
            if '/' in ip else
            ipaddress.ip_address(unicodify(ip.strip()))  # Otherwise interpret it as an ip address.
            for ip in kwargs.get("fetch_url_allowlist", "").split(',')
            if len(ip.strip()) > 0
        ]
        self.template_path = self._in_root_dir(kwargs.get("template_path", "templates"))
        self.job_queue_cleanup_interval = int(kwargs.get("job_queue_cleanup_interval", "5"))
        self.cluster_files_directory = self._in_root_dir(self.cluster_files_directory)

        # Fall back to legacy job_working_directory config variable if set.
        self.jobs_directory = self._in_data_dir(kwargs.get("jobs_directory", self.job_working_directory))
        if self.preserve_python_environment not in ["legacy_only", "legacy_and_local", "always"]:
            log.warning("preserve_python_environment set to unknown value [%s], defaulting to legacy_only")
            self.preserve_python_environment = "legacy_only"
        self.nodejs_path = kwargs.get("nodejs_path")
        # Older default container cache path, I don't think anyone is using it anymore and it wasn't documented - we
        # should probably drop the backward compatiblity to save the path check.
        self.container_image_cache_path = self._in_data_dir(kwargs.get("container_image_cache_path", "container_images"))
        if not os.path.exists(self.container_image_cache_path):
            self.container_image_cache_path = self._in_root_dir(kwargs.get("container_image_cache_path", self._in_data_dir("container_cache")))
        self.output_size_limit = int(kwargs.get('output_size_limit', 0))
        # activation_email was used until release_15.03
        activation_email = kwargs.get('activation_email')
        self.email_from = self.email_from or activation_email

        self.email_domain_blocklist_content = self._load_list_from_file(self._in_config_dir(self.email_domain_blocklist_file)) if self.email_domain_blocklist_file else None
        self.email_domain_allowlist_content = self._load_list_from_file(self._in_config_dir(self.email_domain_allowlist_file)) if self.email_domain_allowlist_file else None

        self.persistent_communication_rooms = listify(self.persistent_communication_rooms, do_strip=True)
        # The transfer manager and deferred job queue
        self.enable_beta_job_managers = string_as_bool(kwargs.get('enable_beta_job_managers', 'False'))
        # These are not even beta - just experiments - don't use them unless
        # you want yours tools to be broken in the future.
        self.enable_beta_tool_formats = string_as_bool(kwargs.get('enable_beta_tool_formats', 'False'))

        if self.workflow_resource_params_mapper and ':' not in self.workflow_resource_params_mapper:
            # Assume it is not a Python function, so a file; else: a Python function
            self.workflow_resource_params_mapper = self._in_root_dir(self.workflow_resource_params_mapper)

        self.pbs_application_server = kwargs.get('pbs_application_server', "")
        self.pbs_dataset_server = kwargs.get('pbs_dataset_server', "")
        self.pbs_dataset_path = kwargs.get('pbs_dataset_path', "")
        self.pbs_stage_path = kwargs.get('pbs_stage_path', "")

        _sanitize_allowlist_path = self._in_managed_config_dir(self.sanitize_allowlist_file)
        if not os.path.isfile(_sanitize_allowlist_path):  # then check old default location
            for deprecated in (
                    self._in_managed_config_dir('sanitize_whitelist.txt'),
                    self._in_root_dir('config/sanitize_whitelist.txt')):
                if os.path.isfile(deprecated):
                    log.warning("The path '%s' for the 'sanitize_allowlist_file' config option is "
                        "deprecated and will be no longer checked in a future release. Please consult "
                        "the latest version of the sample configuration file." % deprecated)
                    _sanitize_allowlist_path = deprecated
                    break
        self.sanitize_allowlist_file = _sanitize_allowlist_path

        self.allowed_origin_hostnames = self._parse_allowed_origin_hostnames(kwargs)
        if "trust_jupyter_notebook_conversion" not in kwargs:
            # if option not set, check IPython-named alternative, falling back to schema default if not set either
            _default = self.trust_jupyter_notebook_conversion
            self.trust_jupyter_notebook_conversion = string_as_bool(kwargs.get('trust_ipython_notebook_conversion', _default))
        # Configuration for the message box directly below the masthead.
        self.blog_url = kwargs.get('blog_url')
        self.user_library_import_symlink_allowlist = listify(self.user_library_import_symlink_allowlist, do_strip=True)
        self.user_library_import_dir_auto_creation = self.user_library_import_dir_auto_creation if self.user_library_import_dir else False
        # Searching data libraries
        self.ftp_upload_dir_template = kwargs.get('ftp_upload_dir_template', '${ftp_upload_dir}%s${ftp_upload_dir_identifier}' % os.path.sep)
        # Support older library-specific path paste option but just default to the new
        # allow_path_paste value.
        self.allow_library_path_paste = string_as_bool(kwargs.get('allow_library_path_paste', self.allow_path_paste))
        self.disable_library_comptypes = kwargs.get('disable_library_comptypes', '').lower().split(',')
        self.check_upload_content = string_as_bool(kwargs.get('check_upload_content', True))
        # On can mildly speed up Galaxy startup time by disabling index of help,
        # not needed on production systems but useful if running many functional tests.
        self.index_tool_help = string_as_bool(kwargs.get("index_tool_help", True))
        self.tool_labels_boost = kwargs.get("tool_labels_boost", 1)
        default_tool_test_data_directories = os.environ.get("GALAXY_TEST_FILE_DIR", self._in_root_dir("test-data"))
        self.tool_test_data_directories = kwargs.get("tool_test_data_directories", default_tool_test_data_directories)
        # Deployers may either specify a complete list of mapping files or get the default for free and just
        # specify a local mapping file to adapt and extend the default one.
        if "conda_mapping_files" not in kwargs:
            _default_mapping = self._in_root_dir(os.path.join("lib", "galaxy", "tool_util", "deps", "resolvers", "default_conda_mapping.yml"))
            # dependency resolution options are consumed via config_dict - so don't populate
            # self, populate config_dict
            self.config_dict["conda_mapping_files"] = [self.local_conda_mapping_file, _default_mapping]

        if self.containers_resolvers_config_file:
            self.containers_resolvers_config_file = self._in_config_dir(self.containers_resolvers_config_file)

        # tool_dependency_dir can be "none" (in old configs). If so, set it to None
        if self.tool_dependency_dir and self.tool_dependency_dir.lower() == 'none':
            self.tool_dependency_dir = None
        if self.involucro_path is None:
            target_dir = self.tool_dependency_dir or self.schema.defaults['tool_dependency_dir']
            self.involucro_path = self._in_data_dir(os.path.join(target_dir, "involucro"))
        self.involucro_path = self._in_root_dir(self.involucro_path)
        if self.mulled_channels:
            self.mulled_channels = [c.strip() for c in self.mulled_channels.split(',')]

        default_job_resubmission_condition = kwargs.get('default_job_resubmission_condition', '')
        if not default_job_resubmission_condition.strip():
            default_job_resubmission_condition = None
        self.default_job_resubmission_condition = default_job_resubmission_condition

        # Configuration options for taking advantage of nginx features
        if self.nginx_upload_store:
            self.nginx_upload_store = os.path.abspath(self.nginx_upload_store)
        self.object_store = kwargs.get('object_store', 'disk')
        self.object_store_check_old_style = string_as_bool(kwargs.get('object_store_check_old_style', False))
        self.object_store_cache_path = self._in_root_dir(kwargs.get("object_store_cache_path", self._in_data_dir("object_store_cache")))
        if self.object_store_store_by is None:
            self.object_store_store_by = 'id'
            if not self.file_path_set and self.file_path.endswith('objects'):
                self.object_store_store_by = 'uuid'
        assert self.object_store_store_by in ['id', 'uuid'], "Invalid value for object_store_store_by [%s]" % self.object_store_store_by
        # Handle AWS-specific config options for backward compatibility
        if kwargs.get('aws_access_key') is not None:
            self.os_access_key = kwargs.get('aws_access_key')
            self.os_secret_key = kwargs.get('aws_secret_key')
            self.os_bucket_name = kwargs.get('s3_bucket')
            self.os_use_reduced_redundancy = kwargs.get('use_reduced_redundancy', False)
        else:
            self.os_access_key = kwargs.get('os_access_key')
            self.os_secret_key = kwargs.get('os_secret_key')
            self.os_bucket_name = kwargs.get('os_bucket_name')
            self.os_use_reduced_redundancy = kwargs.get('os_use_reduced_redundancy', False)
        self.os_host = kwargs.get('os_host')
        self.os_port = kwargs.get('os_port')
        self.os_is_secure = string_as_bool(kwargs.get('os_is_secure', True))
        self.os_conn_path = kwargs.get('os_conn_path', '/')
        self.object_store_cache_size = float(kwargs.get('object_store_cache_size', -1))
        self.distributed_object_store_config_file = kwargs.get('distributed_object_store_config_file')
        if self.distributed_object_store_config_file is not None:
            self.distributed_object_store_config_file = self._in_root_dir(self.distributed_object_store_config_file)
        self.irods_root_collection_path = kwargs.get('irods_root_collection_path')
        self.irods_default_resource = kwargs.get('irods_default_resource')
        # Heartbeat log file name override
        if self.global_conf is not None and 'heartbeat_log' in self.global_conf:
            self.heartbeat_log = self.global_conf['heartbeat_log']
        # Determine which 'server:' this is
        self.server_name = 'main'
        for arg in sys.argv:
            # Crummy, but PasteScript does not give you a way to determine this
            if arg.lower().startswith('--server-name='):
                self.server_name = arg.split('=', 1)[-1]
        # Allow explicit override of server name in config params
        if "server_name" in kwargs:
            self.server_name = kwargs.get("server_name")
        # The application stack code may manipulate the server name. It also needs to be accessible via the get() method
        # for galaxy.util.facts()
        self.config_dict['base_server_name'] = self.base_server_name = self.server_name
        # Store all configured server names for the message queue routing
        self.server_names = []
        for section in self.global_conf_parser.sections():
            if section.startswith('server:'):
                self.server_names.append(section.replace('server:', '', 1))

        self._set_galaxy_infrastructure_url(kwargs)

        # Asynchronous execution process pools - limited functionality for now, attach_to_pools is designed to allow
        # webless Galaxy server processes to attach to arbitrary message queues (e.g. as job handlers) so they do not
        # have to be explicitly defined as such in the job configuration.
        self.attach_to_pools = kwargs.get('attach_to_pools', []) or []

        # Store advanced job management config
        self.job_handlers = [x.strip() for x in kwargs.get('job_handlers', self.server_name).split(',')]
        self.default_job_handlers = [x.strip() for x in kwargs.get('default_job_handlers', ','.join(self.job_handlers)).split(',')]
        # Galaxy internal control queue configuration.
        # If specified in universe, use it, otherwise we use whatever 'real'
        # database is specified.  Lastly, we create and use new sqlite database
        # (to minimize locking) as a final option.
        if 'amqp_internal_connection' in kwargs:
            self.amqp_internal_connection = kwargs.get('amqp_internal_connection')
            # TODO Get extra amqp args as necessary for ssl
        elif 'database_connection' in kwargs:
            self.amqp_internal_connection = "sqlalchemy+" + self.database_connection
        else:
            self.amqp_internal_connection = "sqlalchemy+sqlite:///%s?isolation_level=IMMEDIATE" % self._in_data_dir("control.sqlite")
        self.pretty_datetime_format = expand_pretty_datetime_format(self.pretty_datetime_format)
        try:
            with open(self.user_preferences_extra_conf_path) as stream:
                self.user_preferences_extra = yaml.safe_load(stream)
        except Exception:
            if self.user_preferences_extra_conf_path_set:
                log.warning('Config file (%s) could not be found or is malformed.' % self.user_preferences_extra_conf_path)
            self.user_preferences_extra = {'preferences': {}}

        # Experimental: This will not be enabled by default and will hide
        # nonproduction code.
        # The api_folders refers to whether the API exposes the /folders section.
        self.api_folders = string_as_bool(kwargs.get('api_folders', False))
        # This is for testing new library browsing capabilities.
        self.new_lib_browse = string_as_bool(kwargs.get('new_lib_browse', False))
        # Logging configuration with logging.config.configDict:
        # Statistics and profiling with statsd
        self.statsd_host = kwargs.get('statsd_host', '')

        ie_dirs = self.interactive_environment_plugins_directory
        self.gie_dirs = [d.strip() for d in (ie_dirs.split(",") if ie_dirs else [])]
        if ie_dirs:
            self.visualization_plugins_directory += ",%s" % ie_dirs

        self.proxy_session_map = self.dynamic_proxy_session_map
        self.manage_dynamic_proxy = self.dynamic_proxy_manage  # Set to false if being launched externally

        # InteractiveTools propagator mapping file
        self.interactivetools_map = self._in_root_dir(kwargs.get("interactivetools_map", self._in_data_dir("interactivetools_map.sqlite")))
        self.interactivetools_prefix = kwargs.get("interactivetools_prefix", "interactivetool")
        self.interactivetools_proxy_host = kwargs.get("interactivetool_proxy_host", None)

        self.containers_conf = parse_containers_config(self.containers_config_file)

        # Compliance/Policy variables
        self.redact_username_during_deletion = False
        self.redact_email_during_deletion = False
        self.redact_ip_address = False
        self.redact_username_in_logs = False
        self.redact_email_in_job_name = False
        self.redact_user_details_in_bugreport = False
        self.redact_user_address_during_deletion = False
        # GDPR compliance mode changes values on a number of variables. Other
        # policies could change (non)overlapping subsets of these variables.
        if self.enable_beta_gdpr:
            self.expose_user_name = False
            self.expose_user_email = False

            self.redact_username_during_deletion = True
            self.redact_email_during_deletion = True
            self.redact_ip_address = True
            self.redact_username_in_logs = True
            self.redact_email_in_job_name = True
            self.redact_user_details_in_bugreport = True
            self.redact_user_address_during_deletion = True
            self.allow_user_deletion = True

            LOGGING_CONFIG_DEFAULT['formatters']['brief'] = {
                'format': '%(asctime)s %(levelname)-8s %(name)-15s %(message)s'
            }
            LOGGING_CONFIG_DEFAULT['handlers']['compliance_log'] = {
                'class': 'logging.handlers.RotatingFileHandler',
                'formatter': 'brief',
                'filename': 'compliance.log',
                'backupCount': 0,
            }
            LOGGING_CONFIG_DEFAULT['loggers']['COMPLIANCE'] = {
                'handlers': ['compliance_log'],
                'level': 'DEBUG',
                'qualname': 'COMPLIANCE'
            }

        log_destination = kwargs.get("log_destination")
        if log_destination == "stdout":
            LOGGING_CONFIG_DEFAULT['handlers']['console'] = {
                'class': 'logging.StreamHandler',
                'formatter': 'stack',
                'level': 'DEBUG',
                'stream': 'ext://sys.stdout',
                'filters': ['stack']
            }
        elif log_destination:
            LOGGING_CONFIG_DEFAULT['handlers']['console'] = {
                'class': 'logging.FileHandler',
                'formatter': 'stack',
                'level': 'DEBUG',
                'filename': kwargs['log_destination'],
                'filters': ['stack']
            }

    def _load_list_from_file(self, filepath):
        with open(filepath) as f:
            return [line.strip() for line in f]

    def _set_galaxy_infrastructure_url(self, kwargs):
        # indicate if this was not set explicitly, so dependending on the context a better default
        # can be used (request url in a web thread, Docker parent in IE stuff, etc.)
        self.galaxy_infrastructure_url_set = kwargs.get('galaxy_infrastructure_url') is not None
        if "HOST_IP" in self.galaxy_infrastructure_url:
            self.galaxy_infrastructure_url = string.Template(self.galaxy_infrastructure_url).safe_substitute({
                'HOST_IP': socket.gethostbyname(socket.gethostname())
            })
        if "UWSGI_PORT" in self.galaxy_infrastructure_url:
            import uwsgi
            http = unicodify(uwsgi.opt['http'])
            host, port = http.split(":", 1)
            assert port, "galaxy_infrastructure_url depends on dynamic PORT determination but port unknown"
            self.galaxy_infrastructure_url = string.Template(self.galaxy_infrastructure_url).safe_substitute({
                'UWSGI_PORT': port
            })

    def parse_config_file_options(self, kwargs):
        """Backwards compatibility for config files moved to the config/ dir."""
        defaults = dict(
            build_sites_config_file=[self._in_config_dir('build_sites.yml'), self._in_sample_dir('build_sites.yml.sample')],
            containers_config_file=[self._in_config_dir('containers_conf.yml')],
            data_manager_config_file=[self._in_config_dir('data_manager_conf.xml')],
            datatypes_config_file=[self._in_config_dir('datatypes_conf.xml'), self._in_sample_dir('datatypes_conf.xml.sample')],
            dependency_resolvers_config_file=[self._in_config_dir('dependency_resolvers_conf.xml')],
            error_report_file=[self._in_config_dir('error_report.yml')],
            job_config_file=[self._in_config_dir('job_conf.xml')],
            job_metrics_config_file=[self._in_config_dir('job_metrics_conf.xml'), self._in_sample_dir('job_metrics_conf.xml.sample')],
            job_resource_params_file=[self._in_config_dir('job_resource_params_conf.xml')],
            file_sources_config_file=[self._in_config_dir('file_sources_conf.yml')],
            local_conda_mapping_file=[self._in_config_dir('local_conda_mapping.yml')],
            migrated_tools_config=[self._in_managed_config_dir('migrated_tools_conf.xml')],
            modules_mapping_files=[self._in_config_dir('environment_modules_mapping.yml')],
            object_store_config_file=[self._in_config_dir('object_store_conf.xml')],
            oidc_backends_config_file=[self._in_config_dir('oidc_backends_config.xml')],
            oidc_config_file=[self._in_config_dir('oidc_config.xml')],
            shed_data_manager_config_file=[self._in_managed_config_dir('shed_data_manager_conf.xml')],
            shed_tool_config_file=[self._in_managed_config_dir('shed_tool_conf.xml')],
            shed_tool_data_table_config=[self._in_managed_config_dir('shed_tool_data_table_conf.xml')],
            tool_destinations_config_file=[self._in_config_dir('tool_destinations.yml')],
            tool_sheds_config_file=[self._in_config_dir('tool_sheds_conf.xml')],
            trs_servers_config_file=[self._in_config_dir('trs_servers_conf.yml')],
            user_preferences_extra_conf_path=[self._in_config_dir('user_preferences_extra_conf.yml')],
            workflow_resource_params_file=[self._in_config_dir('workflow_resource_params_conf.xml')],
            workflow_schedulers_config_file=[self._in_config_dir('workflow_schedulers_conf.xml')],
            markdown_export_css=[self._in_config_dir('markdown_export.css')],
            markdown_export_css_pages=[self._in_config_dir('markdown_export_pages.css')],
            markdown_export_css_invocation_reports=[self._in_config_dir('markdown_export_invocation_reports.css')],
            # self.file_path set to self._in_data_dir('objects') by schema
            file_path=[self._in_data_dir('files'), self.file_path],
        )
        listify_defaults = {
            'tool_data_table_config_path': [
                self._in_config_dir('tool_data_table_conf.xml'),
                self._in_sample_dir('tool_data_table_conf.xml.sample')],
            'tool_config_file': [
                self._in_config_dir('tool_conf.xml'),
                self._in_sample_dir('tool_conf.xml.sample')]
        }

        self._parse_config_file_options(defaults, listify_defaults, kwargs)

        # Backwards compatibility for names used in too many places to fix
        self.datatypes_config = self.datatypes_config_file
        self.tool_configs = self.tool_config_file

    def reload_sanitize_allowlist(self, explicit=True):
        self.sanitize_allowlist = []
        try:
            with open(self.sanitize_allowlist_file) as f:
                for line in f.readlines():
                    if not line.startswith("#"):
                        self.sanitize_allowlist.append(line.strip())
        except OSError:
            if explicit:
                log.warning("Sanitize log file explicitly specified as '%s' but does not exist, continuing with no tools allowlisted.", self.sanitize_allowlist_file)

    def ensure_tempdir(self):
        self._ensure_directory(self.new_file_path)

    def check(self):
        # Check that required directories exist; attempt to create otherwise
        paths_to_check = [
            self.data_dir,
            self.ftp_upload_dir,
            self.library_import_dir,
            self.managed_config_dir,
            self.new_file_path,
            self.nginx_upload_store,
            self.object_store_cache_path,
            self.template_cache_path,
            self.tool_data_path,
            self.user_library_import_dir,
        ]
        for path in paths_to_check:
            self._ensure_directory(path)
        # Check that required files exist
        tool_configs = self.tool_configs
        for path in tool_configs:
            if not os.path.exists(path) and path not in (self.shed_tool_config_file, self.migrated_tools_config):
                raise ConfigurationError("Tool config file not found: %s" % path)
        for datatypes_config in listify(self.datatypes_config):
            if not os.path.isfile(datatypes_config):
                raise ConfigurationError("Datatypes config file not found: %s" % datatypes_config)
        # Check for deprecated options.
        for key in self.config_dict.keys():
            if key in self.deprecated_options:
                log.warning("Config option '%s' is deprecated and will be removed in a future release.  Please consult the latest version of the sample configuration file." % key)

    @staticmethod
    def _parse_allowed_origin_hostnames(kwargs):
        """
        Parse a CSV list of strings/regexp of hostnames that should be allowed
        to use CORS and will be sent the Access-Control-Allow-Origin header.
        """
        allowed_origin_hostnames = listify(kwargs.get('allowed_origin_hostnames'))
        if not allowed_origin_hostnames:
            return None

        def parse(string):
            # a string enclosed in fwd slashes will be parsed as a regexp: e.g. /<some val>/
            if string[0] == '/' and string[-1] == '/':
                string = string[1:-1]
                return re.compile(string, flags=(re.UNICODE))
            return string

        return [parse(v) for v in allowed_origin_hostnames if v]


# legacy naming
Configuration = GalaxyAppConfiguration


def reload_config_options(current_config):
    """Reload modified reloadable config options."""
    modified_config = read_properties_from_file(current_config.config_file)
    for option in current_config.schema.reloadable_options:
        if option in modified_config:
            # compare to raw value, as that one is set only on load and reload
            if current_config._raw_config[option] != modified_config[option]:
                current_config._raw_config[option] = modified_config[option]
                setattr(current_config, option, modified_config[option])
                log.info('Reloaded %s' % option)


def get_database_engine_options(kwargs, model_prefix=''):
    """
    Allow options for the SQLAlchemy database engine to be passed by using
    the prefix "database_engine_option".
    """
    conversions = {
        'convert_unicode': string_as_bool,
        'pool_timeout': int,
        'echo': string_as_bool,
        'echo_pool': string_as_bool,
        'pool_recycle': int,
        'pool_size': int,
        'max_overflow': int,
        'pool_threadlocal': string_as_bool,
        'server_side_cursors': string_as_bool
    }
    prefix = "%sdatabase_engine_option_" % model_prefix
    prefix_len = len(prefix)
    rval = {}
    for key, value in kwargs.items():
        if key.startswith(prefix):
            key = key[prefix_len:]
            if key in conversions:
                value = conversions[key](value)
            rval[key] = value
    return rval


def get_database_url(config):
    db_url = config.database_connection
    return db_url


def init_models_from_config(config, map_install_models=False, object_store=None, trace_logger=None):
    db_url = get_database_url(config)
    model = mapping.init(
        config.file_path,
        db_url,
        config.database_engine_options,
        map_install_models=map_install_models,
        database_query_profiling_proxy=config.database_query_profiling_proxy,
        object_store=object_store,
        trace_logger=trace_logger,
        use_pbkdf2=config.get_bool('use_pbkdf2', True),
        slow_query_log_threshold=config.slow_query_log_threshold,
        thread_local_log=config.thread_local_log,
        log_query_counts=config.database_log_query_counts,
    )
    return model


def configure_logging(config):
    """Allow some basic logging configuration to be read from ini file.

    This should be able to consume either a galaxy.config.Configuration object
    or a simple dictionary of configuration variables.
    """
    # Get root logger
    logging.addLevelName(LOGLV_TRACE, "TRACE")
    root = logging.getLogger()
    # PasteScript will have already configured the logger if the
    # 'loggers' section was found in the config file, otherwise we do
    # some simple setup using the 'log_*' values from the config.
    parser = getattr(config, "global_conf_parser", None)
    if parser:
        paste_configures_logging = config.global_conf_parser.has_section("loggers")
    else:
        paste_configures_logging = False
    auto_configure_logging = not paste_configures_logging and string_as_bool(config.get("auto_configure_logging", "True"))
    if auto_configure_logging:
        logging_conf = config.get('logging', None)
        if logging_conf is None:
            # if using the default logging config, honor the log_level setting
            logging_conf = LOGGING_CONFIG_DEFAULT
            if config.get('log_level', 'DEBUG') != 'DEBUG':
                logging_conf['handlers']['console']['level'] = config.get('log_level', 'DEBUG')
        # configure logging with logging dict in config, template *FileHandler handler filenames with the `filename_template` option
        for name, conf in logging_conf.get('handlers', {}).items():
            if conf['class'].startswith('logging.') and conf['class'].endswith('FileHandler') and 'filename_template' in conf:
                conf['filename'] = conf.pop('filename_template').format(**get_stack_facts(config=config))
                logging_conf['handlers'][name] = conf
        logging.config.dictConfig(logging_conf)
    if getattr(config, "sentry_dsn", None):
        from raven.handlers.logging import SentryHandler
        sentry_handler = SentryHandler(config.sentry_dsn)
        sentry_handler.setLevel(logging.WARN)
        register_postfork_function(root.addHandler, sentry_handler)


class ConfiguresGalaxyMixin:
    """Shared code for configuring Galaxy-like app objects."""

    def _configure_genome_builds(self, data_table_name="__dbkeys__", load_old_style=True):
        self.genome_builds = GenomeBuilds(self, data_table_name=data_table_name, load_old_style=load_old_style)

    def wait_for_toolbox_reload(self, old_toolbox):
        timer = ExecutionTimer()
        log.debug('Waiting for toolbox reload')
        # Wait till toolbox reload has been triggered (or more than 60 seconds have passed)
        while timer.elapsed < 60:
            if self.toolbox.has_reloaded(old_toolbox):
                log.debug('Finished waiting for toolbox reload %s', timer)
                break
            time.sleep(0.1)
        else:
            log.warning('Waiting for toolbox reload timed out after 60 seconds')

    def _configure_tool_config_files(self):
        if self.config.shed_tool_config_file not in self.config.tool_configs:
            self.config.tool_configs.append(self.config.shed_tool_config_file)
        # The value of migrated_tools_config is the file reserved for containing only those tools that have been
        # eliminated from the distribution and moved to the tool shed. If migration checking is disabled, only add it if
        # it exists (since this may be an existing deployment where migrations were previously run).
        if ((self.config.check_migrate_tools or os.path.exists(self.config.migrated_tools_config))
                and self.config.migrated_tools_config not in self.config.tool_configs):
            self.config.tool_configs.append(self.config.migrated_tools_config)

    def _configure_toolbox(self):
        from galaxy import tools
        from galaxy.managers.citations import CitationsManager
        from galaxy.tool_util.deps import containers
        from galaxy.tool_util.deps.dependencies import AppInfo
        import galaxy.tools.search

        self.citations_manager = CitationsManager(self)

        from galaxy.managers.tools import DynamicToolManager
        self.dynamic_tools_manager = DynamicToolManager(self)
        self._toolbox_lock = threading.RLock()
        self.toolbox = tools.ToolBox(self.config.tool_configs, self.config.tool_path, self)
        galaxy_root_dir = os.path.abspath(self.config.root)
        file_path = os.path.abspath(self.config.file_path)
        app_info = AppInfo(
            galaxy_root_dir=galaxy_root_dir,
            default_file_path=file_path,
            tool_data_path=self.config.tool_data_path,
            shed_tool_data_path=self.config.shed_tool_data_path,
            outputs_to_working_directory=self.config.outputs_to_working_directory,
            container_image_cache_path=self.config.container_image_cache_path,
            library_import_dir=self.config.library_import_dir,
            enable_mulled_containers=self.config.enable_mulled_containers,
            containers_resolvers_config_file=self.config.containers_resolvers_config_file,
            involucro_path=self.config.involucro_path,
            involucro_auto_init=self.config.involucro_auto_init,
            mulled_channels=self.config.mulled_channels,
        )
        mulled_resolution_cache = None
        if self.config.mulled_resolution_cache_type:
            cache_opts = {
                'cache.type': self.config.mulled_resolution_cache_type,
                'cache.data_dir': self.config.mulled_resolution_cache_data_dir,
                'cache.lock_dir': self.config.mulled_resolution_cache_lock_dir,
            }
            mulled_resolution_cache = CacheManager(**parse_cache_config_options(cache_opts)).get_cache('mulled_resolution')
        self.container_finder = containers.ContainerFinder(app_info, mulled_resolution_cache=mulled_resolution_cache)
        self._set_enabled_container_types()
        index_help = getattr(self.config, "index_tool_help", True)
        self.toolbox_search = galaxy.tools.search.ToolBoxSearch(self.toolbox, index_dir=self.config.tool_search_index_dir, index_help=index_help)

    def reindex_tool_search(self):
        # Call this when tools are added or removed.
        self.toolbox_search.build_index(tool_cache=self.tool_cache)
        self.tool_cache.reset_status()

    def _set_enabled_container_types(self):
        container_types_to_destinations = collections.defaultdict(list)
        for destinations in self.job_config.destinations.values():
            for destination in destinations:
                for enabled_container_type in self.container_finder._enabled_container_types(destination.params):
                    container_types_to_destinations[enabled_container_type].append(destination)
        self.toolbox.dependency_manager.set_enabled_container_types(container_types_to_destinations)
        self.toolbox.dependency_manager.resolver_classes.update(self.container_finder.container_registry.resolver_classes)
        self.toolbox.dependency_manager.dependency_resolvers.extend(self.container_finder.container_registry.container_resolvers)

    def _configure_tool_data_tables(self, from_shed_config):
        from galaxy.tools.data import ToolDataTableManager

        # Initialize tool data tables using the config defined by self.config.tool_data_table_config_path.
        self.tool_data_tables = ToolDataTableManager(tool_data_path=self.config.tool_data_path,
                                                     config_filename=self.config.tool_data_table_config_path,
                                                     other_config_dict=self.config)
        # Load additional entries defined by self.config.shed_tool_data_table_config into tool data tables.
        try:
            self.tool_data_tables.load_from_config_file(config_filename=self.config.shed_tool_data_table_config,
                                                        tool_data_path=self.tool_data_tables.tool_data_path,
                                                        from_shed_config=from_shed_config)
        except OSError as exc:
            # Missing shed_tool_data_table_config is okay if it's the default
            if exc.errno != errno.ENOENT or self.config.shed_tool_data_table_config_set:
                raise

    def _configure_datatypes_registry(self, installed_repository_manager=None):
        from galaxy.datatypes import registry
        # Create an empty datatypes registry.
        self.datatypes_registry = registry.Registry(self.config)
        if installed_repository_manager:
            # Load proprietary datatypes defined in datatypes_conf.xml files in all installed tool shed repositories.  We
            # load proprietary datatypes before datatypes in the distribution because Galaxy's default sniffers include some
            # generic sniffers (eg text,xml) which catch anything, so it's impossible for proprietary sniffers to be used.
            # However, if there is a conflict (2 datatypes with the same extension) between a proprietary datatype and a datatype
            # in the Galaxy distribution, the datatype in the Galaxy distribution will take precedence.  If there is a conflict
            # between 2 proprietary datatypes, the datatype from the repository that was installed earliest will take precedence.
            installed_repository_manager.load_proprietary_datatypes()
        # Load the data types in the Galaxy distribution, which are defined in self.config.datatypes_config.
        datatypes_configs = self.config.datatypes_config
        for datatypes_config in listify(datatypes_configs):
            # Setting override=False would make earlier files would take
            # precedence - but then they wouldn't override tool shed
            # datatypes.
            self.datatypes_registry.load_datatypes(self.config.root, datatypes_config, override=True)

    def _configure_object_store(self, **kwds):
        from galaxy.objectstore import build_object_store_from_config
        self.object_store = build_object_store_from_config(self.config, **kwds)

    def _configure_security(self):
        from galaxy.security import idencoding
        self.security = idencoding.IdEncodingHelper(id_secret=self.config.id_secret)

    def _configure_tool_shed_registry(self):
        import galaxy.tool_shed.tool_shed_registry

        # Set up the tool sheds registry
        if os.path.isfile(self.config.tool_sheds_config_file):
            self.tool_shed_registry = galaxy.tool_shed.tool_shed_registry.Registry(self.config.tool_sheds_config_file)
        else:
            self.tool_shed_registry = galaxy.tool_shed.tool_shed_registry.Registry()

    def _configure_models(self, check_migrate_databases=False, check_migrate_tools=False, config_file=None):
        """Preconditions: object_store must be set on self."""
        db_url = get_database_url(self.config)
        install_db_url = self.config.install_database_connection
        # TODO: Consider more aggressive check here that this is not the same
        # database file under the hood.
        combined_install_database = not(install_db_url and install_db_url != db_url)
        install_db_url = install_db_url or db_url
        install_database_options = self.config.database_engine_options if combined_install_database else self.config.install_database_engine_options

        if self.config.database_wait:
            self._wait_for_database(db_url)

        if getattr(self.config, "max_metadata_value_size", None):
            from galaxy.model import custom_types
            custom_types.MAX_METADATA_VALUE_SIZE = self.config.max_metadata_value_size

        if check_migrate_databases:
            # Initialize database / check for appropriate schema version.  # If this
            # is a new installation, we'll restrict the tool migration messaging.
            from galaxy.model.migrate.check import create_or_verify_database
            create_or_verify_database(db_url, config_file, self.config.database_engine_options, app=self, map_install_models=combined_install_database)
            if not combined_install_database:
                tsi_create_or_verify_database(install_db_url, install_database_options, app=self)

        if check_migrate_tools:
            # Alert the Galaxy admin to tools that have been moved from the distribution to the tool shed.
            from galaxy.tool_shed.galaxy_install.migrate.check import verify_tools
            verify_tools(self, install_db_url, config_file, install_database_options)

        self.model = init_models_from_config(
            self.config,
            map_install_models=combined_install_database,
            object_store=self.object_store,
            trace_logger=getattr(self, "trace_logger", None)
        )
        if combined_install_database:
            log.info("Install database targetting Galaxy's database configuration.")
            self.install_model = self.model
        else:
            from galaxy.model.tool_shed_install import mapping as install_mapping
            install_db_url = self.config.install_database_connection
            log.info("Install database using its own connection %s" % install_db_url)
            self.install_model = install_mapping.init(install_db_url,
                                                      install_database_options)

    def _configure_signal_handlers(self, handlers):
        for sig, handler in handlers.items():
            signal.signal(sig, handler)

    def _wait_for_database(self, url):
        from sqlalchemy_utils import database_exists
        attempts = self.config.database_wait_attempts
        pause = self.config.database_wait_sleep
        for i in range(1, attempts):
            try:
                database_exists(url)
                break
            except Exception:
                log.info("Waiting for database: attempt %d of %d" % (i, attempts))
                time.sleep(pause)

    @property
    def tool_dependency_dir(self):
        return self.toolbox.dependency_manager.default_base_path
