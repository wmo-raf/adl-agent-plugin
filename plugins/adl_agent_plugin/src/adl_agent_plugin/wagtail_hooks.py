from django.urls import path
from wagtail import hooks

from .views import issue_pairing_code, revoke_device
from .viewsets import agent_device_viewset


@hooks.register("register_admin_urls")
def urlconf_adl_agent_plugin():
    return [
        path(
            "adl-agent-plugin/devices/<int:pk>/issue-pairing-code/",
            issue_pairing_code,
            name="agent_device_issue_pairing_code",
        ),
        path(
            "adl-agent-plugin/devices/<int:pk>/revoke/",
            revoke_device,
            name="agent_device_revoke",
        ),
    ]


@hooks.register("register_admin_viewset")
def register_agent_device_viewset():
    return agent_device_viewset
