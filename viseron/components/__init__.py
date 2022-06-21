"""Viseron components."""
from __future__ import annotations

import concurrent
import importlib
import logging
import threading
import time
import traceback
from dataclasses import dataclass
from timeit import default_timer as timer
from typing import TYPE_CHECKING, List

import voluptuous as vol
from voluptuous.humanize import humanize_error

from viseron.const import (
    COMPONENT_RETRY_INTERVAL,
    COMPONENT_RETRY_INTERVAL_MAX,
    DOMAIN_IDENTIFIERS,
    DOMAIN_RETRY_INTERVAL,
    DOMAIN_RETRY_INTERVAL_MAX,
    DOMAIN_SETUP_TASKS,
    DOMAINS_TO_SETUP,
    FAILED,
    LOADED,
    LOADING,
    SLOW_DEPENDENCY_WARNING,
    SLOW_SETUP_WARNING,
)
from viseron.exceptions import ComponentNotReady, DomainNotReady

if TYPE_CHECKING:
    from viseron import Viseron
    from viseron.domains import OptionalDomain, RequireDomain


@dataclass
class DomainToSetup:
    """Represent a domain to setup."""

    component: Component
    domain: str
    config: dict
    identifier: str
    require_domains: List[RequireDomain]
    optional_domains: List[OptionalDomain]


LOGGING_COMPONENTS = {"logger"}
# Core components are always loaded even if they are not present in config
CORE_COMPONENTS = {"data_stream"}
# Default components are always loaded even if they are not present in config
DEFAULT_COMPONENTS = {"webserver"}

DOMAIN_SETUP_LOCK = threading.Lock()

LOGGER = logging.getLogger(__name__)


class Component:
    """Represents a Viseron component."""

    def __init__(self, vis, path, name, config):
        self._vis = vis
        self._path = path
        self._name = name
        self._config = config

        self.domains_to_setup: List[DomainToSetup] = []

    def __str__(self):
        """Return string representation."""
        return self._name

    @property
    def name(self):
        """Return component name."""
        return self._name

    @property
    def path(self):
        """Return component path."""
        return self._path

    def get_component(self):
        """Return component module."""
        return importlib.import_module(self._path)

    def validate_component_config(self, component_module):
        """Validate component config."""
        if hasattr(component_module, "CONFIG_SCHEMA"):
            try:
                return component_module.CONFIG_SCHEMA(self._config)  # type: ignore
            except vol.Invalid as ex:
                LOGGER.exception(
                    f"Error validating config for component {self.name}: "
                    f"{humanize_error(self._config, ex)}"
                )
                return None
            except Exception:  # pylint: disable=broad-except
                LOGGER.exception("Unknown error calling %s CONFIG_SCHEMA", self.name)
                return None
        return True

    def setup_component(self, tries=1):
        """Set up component."""
        LOGGER.info(
            "Setting up component %s%s",
            self.name,
            (f", attempt {tries}" if tries > 1 else ""),
        )
        slow_setup_warning = threading.Timer(
            SLOW_SETUP_WARNING,
            LOGGER.warning,
            (
                f"Setup of component {self.name} "
                f"is taking longer than {SLOW_SETUP_WARNING} seconds",
            ),
        )

        component_module = self.get_component()
        config = self.validate_component_config(component_module)

        start = timer()
        result = False
        if config:
            try:
                slow_setup_warning.start()
                result = component_module.setup(self._vis, config)
            except ComponentNotReady as error:
                wait_time = min(
                    tries * COMPONENT_RETRY_INTERVAL, COMPONENT_RETRY_INTERVAL_MAX
                )
                LOGGER.error(
                    f"Component {self.name} is not ready. "
                    f"Retrying in {wait_time} seconds in the background. "
                    f"Error: {str(error)}"
                )
                threading.Timer(
                    wait_time,
                    setup_component,
                    args=(self._vis, self),
                    kwargs={"tries": tries + 1},
                ).start()
            except Exception as ex:  # pylint: disable=broad-except
                LOGGER.error(
                    f"Uncaught exception setting up component {self.name}: {ex}\n"
                    f"{traceback.print_exc()}"
                )
            finally:
                slow_setup_warning.cancel()

        end = timer()
        if result is True:
            LOGGER.info(
                "Setup of component %s took %.1f seconds",
                self.name,
                end - start,
            )
            return True

        # Clear any domains that were marked for setup
        for domain_to_setup in self.domains_to_setup:
            del self._vis.data[DOMAINS_TO_SETUP][domain_to_setup.domain][
                domain_to_setup.identifier
            ]
        self.domains_to_setup.clear()
        if result is False:
            LOGGER.error(
                "Setup of component %s failed",
                self.name,
            )
            return False

        LOGGER.error(
            "Setup of component %s did not return boolean",
            self.name,
        )
        return False

    def add_domain_to_setup(
        self, domain, config, identifier, require_domains, optional_domains
    ):
        """Add a domain to setup queue."""
        domain_to_setup = DomainToSetup(
            component=self,
            domain=domain,
            config=config,
            identifier=identifier,
            require_domains=require_domains if require_domains else [],
            optional_domains=optional_domains if optional_domains else [],
        )
        self.domains_to_setup.append(domain_to_setup)
        self._vis.data[DOMAINS_TO_SETUP].setdefault(domain, {})[
            identifier
        ] = domain_to_setup

    def get_domain(self, domain):
        """Return domain module."""
        return importlib.import_module(f"{self._path}.{domain}")

    def validate_domain_config(self, config, domain, domain_module):
        """Validate domain config."""
        if hasattr(domain_module, "CONFIG_SCHEMA"):
            try:
                return domain_module.CONFIG_SCHEMA(config)  # type: ignore
            except vol.Invalid as ex:
                LOGGER.exception(
                    f"Error validating config for domain {domain} and "
                    f"component {self.name}: "
                    f"{humanize_error(self._config, ex)}"
                )
                return None
            except Exception:  # pylint: disable=broad-except
                LOGGER.exception(
                    "Unknown error calling %s.%s CONFIG_SCHEMA", self.name, domain
                )
                return None
        return config

    def _setup_dependencies(self, domain_to_setup: DomainToSetup):
        """Await the setup of all dependencies."""

        def _slow_dependency_warning(futures):
            unfinished_dependencies = [future for future in futures if future.running()]
            if unfinished_dependencies:
                LOGGER.warning(
                    "Domain %s for component %s%s "
                    "is still waiting for dependencies: %s",
                    domain_to_setup.domain,
                    self.name,
                    (
                        f" with identifier {domain_to_setup.identifier}"
                        if domain_to_setup.identifier
                        else ""
                    ),
                    [
                        f"domain: {future.domain}, identifier: {future.identifier}"
                        for future in unfinished_dependencies
                    ],
                )

        dependencies_futures = [
            self._vis.data[DOMAIN_SETUP_TASKS][required_domain.domain][
                required_domain.identifier
            ]
            for required_domain in domain_to_setup.require_domains
        ]

        optional_dependencies_futures = [
            self._vis.data[DOMAIN_SETUP_TASKS][optional_domain.domain][
                optional_domain.identifier
            ]
            for optional_domain in domain_to_setup.optional_domains
            if (
                optional_domain.domain in self._vis.data[DOMAIN_IDENTIFIERS]
                and optional_domain.identifier
                in self._vis.data[DOMAIN_IDENTIFIERS][optional_domain.domain]
            )
        ]

        if dependencies_futures:
            LOGGER.debug(
                "Domain %s for component %s%s will wait for dependencies %s",
                domain_to_setup.domain,
                self.name,
                (
                    f" with identifier {domain_to_setup.identifier}"
                    if domain_to_setup.identifier
                    else ""
                ),
                [
                    f"domain: {future.domain}, identifier: {future.identifier}"
                    for future in dependencies_futures
                ],
            )
        if optional_dependencies_futures:
            LOGGER.debug(
                "Domain %s for component %s%s will wait for optional dependencies %s",
                domain_to_setup.domain,
                self.name,
                (
                    f" with identifier {domain_to_setup.identifier}"
                    if domain_to_setup.identifier
                    else ""
                ),
                [
                    f"domain: {future.domain}, identifier: {future.identifier}"
                    for future in optional_dependencies_futures
                ],
            )

        slow_dependency_warning = self._vis.background_scheduler.add_job(
            _slow_dependency_warning,
            "interval",
            seconds=SLOW_DEPENDENCY_WARNING,
            args=[dependencies_futures + optional_dependencies_futures],
        )
        failed = []
        for future in list(
            concurrent.futures.as_completed(
                dependencies_futures + optional_dependencies_futures
            )
        ):
            if future.result() is True:
                continue
            failed.append(future)
        slow_dependency_warning.remove()

        if failed:
            LOGGER.error(
                "Unable to setup dependencies for domain %s for component %s. "
                "Failed dependencies: %s",
                domain_to_setup.domain,
                self.name,
                [
                    f"domain: {future.domain}, "  # type: ignore
                    f"identifier: {future.identifier}"  # type: ignore
                    for future in failed
                ],
            )
            return False
        return True

    def setup_domain(self, domain_to_setup: DomainToSetup, tries=1):
        """Set up domain."""
        LOGGER.info(
            "Setting up domain %s for component %s%s%s",
            domain_to_setup.domain,
            self.name,
            (
                f" with identifier {domain_to_setup.identifier}"
                if domain_to_setup.identifier
                else ""
            ),
            (f", attempt {tries}" if tries > 1 else ""),
        )

        domain_module = self.get_domain(domain_to_setup.domain)
        config = self.validate_domain_config(
            domain_to_setup.config, domain_to_setup.domain, domain_module
        )

        if not self._setup_dependencies(domain_to_setup):
            return False

        slow_setup_warning = threading.Timer(
            SLOW_SETUP_WARNING,
            LOGGER.warning,
            args=(
                (
                    "Setup of domain %s for component %s%s "
                    "is taking longer than %s seconds"
                ),
                domain_to_setup.domain,
                self.name,
                (
                    f" with identifier {domain_to_setup.identifier}"
                    if domain_to_setup.identifier
                    else ""
                ),
                SLOW_SETUP_WARNING,
            ),
        )

        start = timer()
        result = False
        if config:
            try:
                slow_setup_warning.start()
                if domain_to_setup.identifier:
                    result = domain_module.setup(
                        self._vis, config, domain_to_setup.identifier
                    )
                else:
                    result = domain_module.setup(self._vis, config)
            except DomainNotReady as error:
                # Cancel the slow setup warning here since the retrying blocks
                slow_setup_warning.cancel()
                wait_time = min(
                    tries * DOMAIN_RETRY_INTERVAL, DOMAIN_RETRY_INTERVAL_MAX
                )
                LOGGER.error(
                    f"Domain {domain_to_setup.domain} "
                    f"for component {self.name} is not ready. "
                    f"Retrying in {wait_time} seconds. "
                    f"Error: {str(error)}"
                )
                time.sleep(wait_time)
                # Running with ThreadPoolExecutor and awaiting the future does not
                # cause a max recursion error if we retry for a long time
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        self.setup_domain,
                        domain_to_setup,
                        tries=tries + 1,
                    )
                    return future.result()
            except Exception as ex:  # pylint: disable=broad-except
                LOGGER.exception(
                    f"Uncaught exception setting up domain {domain_to_setup.domain} for"
                    f" component {self.name}: {ex}"
                )
                try:
                    self._vis.data[DOMAIN_IDENTIFIERS][domain_to_setup.domain].remove(
                        domain_to_setup.identifier
                    )
                except KeyError:
                    pass
            finally:
                slow_setup_warning.cancel()

        end = timer()

        if result is True:
            LOGGER.info(
                "Setup of domain %s for component %s%s took %.1f seconds",
                domain_to_setup.domain,
                self.name,
                (
                    f" with identifier {domain_to_setup.identifier}"
                    if domain_to_setup.identifier
                    else ""
                ),
                end - start,
            )
            return True

        if result is False:
            LOGGER.error(
                "Setup of domain %s for component %s%s failed",
                domain_to_setup.domain,
                self.name,
                (
                    f" with identifier {domain_to_setup.identifier}"
                    if domain_to_setup.identifier
                    else ""
                ),
            )
            return False

        LOGGER.error(
            "Setup of domain %s for component %s did not return boolean",
            domain_to_setup.domain,
            self.name,
        )
        return False


def get_component(vis, component, config):
    """Get configured component."""
    from viseron import (  # pylint: disable=import-outside-toplevel,import-self
        components,
    )

    for _ in components.__path__:
        return Component(vis, f"{components.__name__}.{component}", component, config)


def setup_component(vis, component: Component, tries=1):
    """Set up single component."""
    # When tries is larger than one, it means we are in a retry loop.
    if tries > 1:
        # Remove component from being marked as failed
        del vis.data[FAILED][component.name]

    try:
        vis.data[LOADING][component.name] = component
        if component.setup_component(tries=tries):
            vis.data[LOADED][component.name] = component
            del vis.data[LOADING][component.name]
        else:
            LOGGER.error(f"Failed setup of component {component.name}")
            vis.data[FAILED][component.name] = component
            del vis.data[LOADING][component.name]

    except ModuleNotFoundError as err:
        LOGGER.error(f"Failed to load component {component.name}: {err}")
        vis.data[FAILED][component.name] = component
        del vis.data[LOADING][component.name]


def domain_dependencies(vis):
    """Check that domain dependencies are resolved."""
    for domain in vis.data[DOMAINS_TO_SETUP]:
        for domain_to_setup in vis.data[DOMAINS_TO_SETUP][domain].values():
            if domain_to_setup.identifier:
                vis.data[DOMAIN_IDENTIFIERS].setdefault(
                    domain_to_setup.domain, []
                ).append(domain_to_setup.identifier)

    for domain in vis.data[DOMAINS_TO_SETUP]:
        for domain_to_setup in list(vis.data[DOMAINS_TO_SETUP][domain].values())[:]:
            if not domain_to_setup.require_domains:
                continue
            for require_domain in domain_to_setup.require_domains:
                if (
                    require_domain.domain in vis.data[DOMAIN_IDENTIFIERS]
                    and require_domain.identifier
                    in vis.data[DOMAIN_IDENTIFIERS][require_domain.domain]
                ):
                    continue
                LOGGER.error(
                    f"Domain {domain_to_setup.domain} "
                    f"for component {domain_to_setup.component.name} "
                    f"requires domain {require_domain.domain} with "
                    f"identifier {require_domain.identifier} but it has not been setup"
                )
                domain_to_setup.component.domains_to_setup.remove(domain_to_setup)
                del vis.data[DOMAINS_TO_SETUP][domain_to_setup.domain][
                    domain_to_setup.identifier
                ]


def _setup_domain(vis, executor, domain_to_setup: DomainToSetup):
    with DOMAIN_SETUP_LOCK:
        future = executor.submit(
            domain_to_setup.component.setup_domain,
            domain_to_setup,
        )
        future.domain = domain_to_setup.domain
        future.identifier = domain_to_setup.identifier
        vis.data[DOMAIN_SETUP_TASKS].setdefault(domain_to_setup.domain, {})[
            domain_to_setup.identifier
        ] = future


def setup_domain(vis, executor, domain_to_setup: DomainToSetup):
    """Set up single domain and all its dependencies."""
    with DOMAIN_SETUP_LOCK:
        if domain_to_setup.identifier in vis.data[DOMAIN_SETUP_TASKS].get(
            domain_to_setup.domain, {}
        ):
            return

    for required_domain in domain_to_setup.require_domains:
        setup_domain(
            vis,
            executor,
            vis.data[DOMAINS_TO_SETUP][required_domain.domain][
                required_domain.identifier
            ],
        )

    for optional_domain in domain_to_setup.optional_domains:
        if (
            optional_domain.domain in vis.data[DOMAIN_IDENTIFIERS]
            and optional_domain.identifier
            in vis.data[DOMAIN_IDENTIFIERS][optional_domain.domain]
        ):
            setup_domain(
                vis,
                executor,
                vis.data[DOMAINS_TO_SETUP][optional_domain.domain][
                    optional_domain.identifier
                ],
            )

    _setup_domain(vis, executor, domain_to_setup)


def setup_domains(vis):
    """Set up all domains."""
    domain_dependencies(vis)

    with concurrent.futures.ThreadPoolExecutor(max_workers=100) as executor:
        for domain in vis.data[DOMAINS_TO_SETUP]:
            for domain_to_setup in vis.data[DOMAINS_TO_SETUP][domain].values():
                setup_domain(vis, executor, domain_to_setup)

        for future in concurrent.futures.as_completed(
            [
                future
                for domain in vis.data[DOMAIN_SETUP_TASKS]
                for future in vis.data[DOMAIN_SETUP_TASKS][domain].values()
            ]
        ):
            # Await results so that any errors are raised
            future.result()


def setup_components(vis: Viseron, config):
    """Set up configured components."""
    components_in_config = {key.split(" ")[0] for key in config}
    # Setup logger first
    for component in components_in_config & LOGGING_COMPONENTS:
        setup_component(vis, get_component(vis, component, config))

    # Setup core components
    for component in CORE_COMPONENTS:
        setup_component(vis, get_component(vis, component, config))

    # Setup default components
    for component in DEFAULT_COMPONENTS:
        setup_component(vis, get_component(vis, component, config))

    # Setup components in parallel
    setup_threads = []
    for component in (
        components_in_config
        - set(LOGGING_COMPONENTS)
        - set(CORE_COMPONENTS)
        - set(DEFAULT_COMPONENTS)
    ):
        setup_threads.append(
            threading.Thread(
                target=setup_component,
                args=(vis, get_component(vis, component, config)),
                name=f"{component}_setup",
            )
        )
    for thread in setup_threads:
        thread.start()

    def join(thread):
        thread.join(timeout=30)
        time.sleep(0.5)  # Wait for thread to exit properly
        if thread.is_alive():
            LOGGER.error(f"{thread.name} did not finish in time")

    with concurrent.futures.ThreadPoolExecutor(max_workers=100) as executor:
        setup_thread_future = {
            executor.submit(join, setup_thread): setup_thread
            for setup_thread in setup_threads
        }
        for future in concurrent.futures.as_completed(setup_thread_future):
            future.result()
