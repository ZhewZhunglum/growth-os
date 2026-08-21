"""Deployment-owned publication runtime injection.

The application default is deliberately offline.  A deployment may opt in to
an explicitly configured runtime factory, but form data can never select the
factory, transport endpoint, or secret material.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from .runtime import PublicationRuntime


def get_publication_runtime() -> PublicationRuntime:
    """Build one request-scoped runtime from trusted Django settings.

    Both the global opt-in and the dotted factory path are required.  Keeping
    the empty runtime as the disabled default preserves fail-closed behaviour
    for local development and deployments that have not configured publishing.
    """

    enabled = getattr(settings, "PUBLICATION_NETWORK_ENABLED", False)
    if not isinstance(enabled, bool):
        raise ImproperlyConfigured("PUBLICATION_NETWORK_ENABLED must be a boolean.")
    if not enabled:
        return PublicationRuntime()

    factory_path = getattr(settings, "PUBLICATION_RUNTIME_FACTORY", "")
    if not isinstance(factory_path, str) or not factory_path.strip():
        raise ImproperlyConfigured(
            "PUBLICATION_RUNTIME_FACTORY must name a trusted dotted callable when publication networking is enabled."
        )
    try:
        factory = import_string(factory_path.strip())
    except (ImportError, AttributeError) as exc:
        raise ImproperlyConfigured(
            "PUBLICATION_RUNTIME_FACTORY could not be imported."
        ) from exc
    if not callable(factory):
        raise ImproperlyConfigured("PUBLICATION_RUNTIME_FACTORY must be callable.")

    runtime = factory()
    if not isinstance(runtime, PublicationRuntime):
        raise ImproperlyConfigured(
            "PUBLICATION_RUNTIME_FACTORY must return a PublicationRuntime."
        )
    return runtime
