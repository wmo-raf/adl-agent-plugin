from django.urls import path

from .api_views import (
    AgentDeviceMeView,
    AgentFileUploadView,
    AgentHeartbeatView,
    AgentManifestView,
    AgentPairView,
    AgentStationLinkConfigView,
    AgentSyncView,
    AgentUpdatePackageView,
    AgentUpdateView,
)

app_name = "adl_agent_plugin"

urlpatterns = [
    path("pair/", AgentPairView.as_view(), name="pair"),
    path("me/", AgentDeviceMeView.as_view(), name="device_me"),
    path("sync/", AgentSyncView.as_view(), name="sync"),
    path("heartbeat/", AgentHeartbeatView.as_view(), name="heartbeat"),
    path("manifest/", AgentManifestView.as_view(), name="manifest"),
    path("files/", AgentFileUploadView.as_view(), name="files"),
    path("update/", AgentUpdateView.as_view(), name="update"),
    # The version is in the path rather than the query so that the package a
    # machine fetched is legible in a proxy log, which on these deployments is
    # sometimes the only record anybody can get at.
    path(
        "update/<str:version>/<str:kind>/",
        AgentUpdatePackageView.as_view(),
        name="update_package",
    ),
    path(
        "station-links/<int:pk>/config/",
        AgentStationLinkConfigView.as_view(),
        name="station_link_config",
    ),
]
