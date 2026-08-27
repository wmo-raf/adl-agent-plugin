#!/usr/bin/env python
import os

from setuptools import find_packages, setup

PROJECT_DIR = os.path.dirname(__file__)
REQUIREMENTS_DIR = os.path.join(PROJECT_DIR, "requirements")
VERSION_FILE = os.path.join(PROJECT_DIR, "src", "adl_agent_plugin", "version.py")


def get_version():
    """Read the version out of the package rather than restating it here.

    Executed rather than imported: at build time the package's dependencies
    are not installed yet, so importing ``adl_agent_plugin`` would fail on
    its Django imports. ``version.py`` holds nothing but a docstring and two
    assignments, which is what makes that safe.

    The point of reading it at all is that the packaged version and the
    version an agent is told over the wire come from one line of one file.
    """
    namespace = {}

    with open(VERSION_FILE) as fp:
        exec(fp.read(), namespace)

    return namespace["VERSION"]


VERSION = get_version()


def get_requirements(env):
    with open(os.path.join(REQUIREMENTS_DIR, f"{env}.txt")) as fp:
        return [
            x.strip()
            for x in fp.read().split("\n")
            if not x.strip().startswith("#") and not x.strip().startswith("-")
        ]


install_requires = get_requirements("base")

setup(
    name="adl-agent-plugin",
    version=VERSION,
    url="https://github.com/wmo-raf/adl-agent-plugin",
    author="WMO RAF",
    author_email="eotenyo@wmo.int",
    license="MIT",
    description="ADL server-side plugin for the ADL Agent: push-based file delivery from country servers",
    long_description="TODO",
    platforms=["linux"],
    package_dir={"": "src"},
    packages=find_packages("src"),
    include_package_data=True,
    install_requires=install_requires
)
