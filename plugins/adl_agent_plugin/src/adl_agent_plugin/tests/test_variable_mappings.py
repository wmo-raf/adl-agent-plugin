"""
Which variable mappings a station link ingests with (plugin authoring guide,
Pattern C).

Mappings are admin-only tier: what the data *means* is decided at HQ, never on
the machine. The pattern gives a connection one list that serves every station
on it, and lets a single awkward station override a parameter without being
given a whole list of its own.
"""

from django.test import TestCase
from django.utils import timezone as dj_timezone

from .helpers import (
    celsius,
    create_connection,
    create_parameter,
    create_station_link,
    map_on_connection,
    map_on_station_link,
)


class VariableMappingResolutionTests(TestCase):
    def setUp(self):
        self.connection = create_connection()
        self.link = create_station_link(self.connection)
        self.unit = celsius()
        self.temperature = create_parameter(name="air_temperature")
        self.rainfall = create_parameter(name="precipitation")

    def resolved(self):
        return {
            m.adl_parameter_id: m.source_parameter_name
            for m in self.link.get_variable_mappings()
        }

    def test_a_link_with_nothing_configured_maps_nothing(self):
        self.assertEqual(self.link.get_variable_mappings(), [])

    def test_the_connections_mappings_serve_every_station_on_it(self):
        map_on_connection(self.connection, self.temperature, self.unit, "AirTemp")

        self.assertEqual(self.resolved(), {self.temperature.pk: "AirTemp"})

    def test_a_station_may_override_one_parameter(self):
        map_on_connection(self.connection, self.temperature, self.unit, "AirTemp")
        map_on_connection(self.connection, self.rainfall, self.unit, "Rain")
        map_on_station_link(self.link, self.temperature, self.unit, "Temp_2")

        self.assertEqual(
            self.resolved(),
            {self.temperature.pk: "Temp_2", self.rainfall.pk: "Rain"},
        )

    def test_a_station_may_add_a_parameter_the_connection_never_named(self):
        map_on_station_link(self.link, self.rainfall, self.unit, "Rain_mm")

        self.assertEqual(self.resolved(), {self.rainfall.pk: "Rain_mm"})

    def test_a_mapping_answers_the_two_questions_core_asks_of_it(self):
        mapping = map_on_connection(
            self.connection, self.temperature, self.unit, "AirTemp"
        )

        self.assertEqual(mapping.source_parameter_name, "AirTemp")
        self.assertEqual(mapping.source_parameter_unit, self.unit)

    def test_another_stations_override_does_not_leak_across_the_connection(self):
        map_on_connection(self.connection, self.temperature, self.unit, "AirTemp")
        other = create_station_link(self.connection)
        map_on_station_link(other, self.temperature, self.unit, "Temp_2")

        self.assertEqual(self.resolved(), {self.temperature.pk: "AirTemp"})


class CollectionStartDateTests(TestCase):
    def test_the_collection_start_date_is_what_core_asks_for(self):
        link = create_station_link()

        self.assertIsNone(link.get_first_collection_date())

        start = dj_timezone.now()
        link.start_date = start
        link.save()

        self.assertEqual(link.get_first_collection_date(), start)
