from django.urls import path

from .api_views import AgentDeviceMeView, AgentPairView

app_name = "adl_agent_plugin"

urlpatterns = [
    path("pair/", AgentPairView.as_view(), name="pair"),
    path("me/", AgentDeviceMeView.as_view(), name="device_me"),
]
