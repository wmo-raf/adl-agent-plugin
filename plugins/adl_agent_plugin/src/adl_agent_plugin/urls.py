from django.urls import path

from .api_views import (
    AgentDeviceMeView,
    AgentPairView,
    AgentStationLinkConfigView,
    AgentSyncView,
)

app_name = "adl_agent_plugin"

urlpatterns = [
    path("pair/", AgentPairView.as_view(), name="pair"),
    path("me/", AgentDeviceMeView.as_view(), name="device_me"),
    path("sync/", AgentSyncView.as_view(), name="sync"),
    path(
        "station-links/<int:pk>/config/",
        AgentStationLinkConfigView.as_view(),
        name="station_link_config",
    ),
]
