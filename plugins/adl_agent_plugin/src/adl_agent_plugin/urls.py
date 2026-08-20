from django.urls import path

from .api_views import (
    AgentDeviceMeView,
    AgentFileUploadView,
    AgentHeartbeatView,
    AgentManifestView,
    AgentPairView,
    AgentStationLinkConfigView,
    AgentSyncView,
)

app_name = "adl_agent_plugin"

urlpatterns = [
    path("pair/", AgentPairView.as_view(), name="pair"),
    path("me/", AgentDeviceMeView.as_view(), name="device_me"),
    path("sync/", AgentSyncView.as_view(), name="sync"),
    path("heartbeat/", AgentHeartbeatView.as_view(), name="heartbeat"),
    path("manifest/", AgentManifestView.as_view(), name="manifest"),
    path("files/", AgentFileUploadView.as_view(), name="files"),
    path(
        "station-links/<int:pk>/config/",
        AgentStationLinkConfigView.as_view(),
        name="station_link_config",
    ),
]
