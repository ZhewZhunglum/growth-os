from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from integrations.publishing import PublicationRuntime, get_publication_runtime


NOT_CALLABLE = object()


def configured_runtime_factory() -> PublicationRuntime:
    return PublicationRuntime()


def invalid_runtime_factory():
    return object()


class PublicationRuntimeFactoryTests(SimpleTestCase):
    @override_settings(
        PUBLICATION_NETWORK_ENABLED=False,
        PUBLICATION_RUNTIME_FACTORY="does.not.need.to.exist",
    )
    def test_networking_disabled_is_the_fail_closed_default(self):
        self.assertIsInstance(get_publication_runtime(), PublicationRuntime)

    @override_settings(PUBLICATION_NETWORK_ENABLED=True, PUBLICATION_RUNTIME_FACTORY="")
    def test_enabled_networking_requires_an_explicit_factory(self):
        with self.assertRaises(ImproperlyConfigured):
            get_publication_runtime()

    @override_settings(
        PUBLICATION_NETWORK_ENABLED=True,
        PUBLICATION_RUNTIME_FACTORY=(
            "integrations.tests.test_publishing_factory.configured_runtime_factory"
        ),
    )
    def test_trusted_dotted_factory_builds_a_request_scoped_runtime(self):
        first = get_publication_runtime()
        second = get_publication_runtime()

        self.assertIsInstance(first, PublicationRuntime)
        self.assertIsInstance(second, PublicationRuntime)
        self.assertIsNot(first, second)

    @override_settings(
        PUBLICATION_NETWORK_ENABLED=True,
        PUBLICATION_RUNTIME_FACTORY=(
            "integrations.tests.test_publishing_factory.NOT_CALLABLE"
        ),
    )
    def test_non_callable_factory_setting_fails_closed(self):
        with self.assertRaises(ImproperlyConfigured):
            get_publication_runtime()

    @override_settings(
        PUBLICATION_NETWORK_ENABLED=True,
        PUBLICATION_RUNTIME_FACTORY=(
            "integrations.tests.test_publishing_factory.invalid_runtime_factory"
        ),
    )
    def test_wrong_factory_return_type_fails_closed(self):
        with self.assertRaises(ImproperlyConfigured):
            get_publication_runtime()
