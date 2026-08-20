"""
Tests for the ingestion-diagnostic contracts: ``get_source_endpoint()``,
``check_source()``, ``check_station_source()`` and the ``adl_sources_count``
duck-typed handover. See the "Ingestion Diagnostic Contracts" page in the ADL
developer guide.

Convention for everything added to this module: **the tests touch no database**.
Build model instances unsaved and stub the source client, so the seam under test
is exactly the contract core consumes. That means ``SimpleTestCase``, never
``TestCase`` — no fixtures, no per-test migrations. Django still calls
``setup_databases()`` whatever the test class, so the suite is run on this
plugin's own compose stack with ``make test`` from the repo root; "DB-free" is
about what the tests touch, not where they run.

Add one test class per surface this plugin implements, each with a happy path
plus every failure branch it classifies. Surfaces the plugin deliberately
declines get no test: asserting that core still returns ``UNSUPPORTED`` tests
core, not this plugin.

The guard below ships with the scaffold and is correct before any plugin code
exists — it parses the source rather than running it.
"""

import ast
import os

from django.test import SimpleTestCase


class OlderCoreImportSafetyTests(SimpleTestCase):
    """The plugin must import cleanly on a core release that predates the
    source-check contracts, so nothing may import ``adl.core.source_checks``
    at module level.

    The contracts import it lazily instead, inside the method that needs it::

        def check_source(self):
            from adl.core.source_checks import SourceCheckResult
            ...

    Never wrap that import in ``try/except ImportError``: on an older core the
    method is never called, so the handler is unreachable, and it would turn a
    genuine import failure into a silent "this plugin does not support the
    check".
    """

    DENIED = "adl.core.source_checks"

    def test_no_module_level_import_of_source_checks(self):
        package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # Every module the plugin ships, found rather than listed: a
        # hand-maintained list is one a later slice forgets to add to, and the
        # module it forgot is exactly the one that would break an older core.
        names = sorted(
            entry for entry in os.listdir(package_dir) if entry.endswith(".py")
        )
        self.assertIn("plugins.py", names, "the package directory was not found")

        for name in names:
            path = os.path.join(package_dir, name)
            with open(path) as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                if node.col_offset != 0:
                    continue  # indented imports are lazy, inside a function
                names = [a.name for a in node.names]
                module = getattr(node, "module", "") or ""
                self.assertNotIn(
                    self.DENIED, [module] + names,
                    f"{name} imports {self.DENIED} at module level")


class InvertedSourceTests(SimpleTestCase):
    """The agent inverts ADL's direction of travel, and the connection says
    so twice -- once for each external layer, differently.

    **Layer 5 has a subject.** There is a real source out there: a machine in
    a country, with an operating system, a disk, and a service that either is
    or is not doing its work. So the connection declares an external source
    and answers the layer -- from the heartbeats that machine sends, since it
    is the machine that dials, not ADL (decision #264).

    **Layer 4 does not.** There is no host for ADL to resolve and no port for
    it to connect to, so the connection declares that it does not dial its
    source and core reports the network path NOT_APPLICABLE.

    That second declaration is not cosmetic, and it is why both are asserted
    here rather than only the first. It is also what stops core reading this
    connection's own ingestion runs as evidence about the source: a run here
    sweeps a staging store, and without the declaration a drain of files that
    arrived before the machine died would report the connection green while
    the country is dark. The behaviour that depends on it is driven end to
    end in ``test_fleet_health``; what is pinned here is the declaration
    those tests rest on.

    DB-free, per this module's convention: all three facts are properties of
    the class, so an unsaved instance is the whole fixture.
    """

    def test_an_agent_connection_claims_the_source_layer(self):
        from adl_agent_plugin.models import AgentConnection

        connection = AgentConnection()

        self.assertTrue(connection.has_external_source)
        self.assertTrue(connection.source_probe_supported)

    def test_an_agent_connection_declares_that_adl_does_not_dial_it(self):
        from adl_agent_plugin.models import AgentConnection

        self.assertFalse(AgentConnection().dials_source)

    def test_an_agent_connection_names_no_endpoint_to_dial(self):
        from adl_agent_plugin.models import AgentConnection

        self.assertIsNone(AgentConnection().get_source_endpoint())
