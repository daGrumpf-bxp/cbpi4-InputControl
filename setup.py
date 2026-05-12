from setuptools import setup

setup(
    name="cbpi4-InputControl",
    version="0.0.6",
    description="CraftBeerPi 4 plugin for input control (physical buttons, etc.) → actor on/off/toggle",
    author="daGrumpf",
    author_email="",
    url="https://github.com/daGrumpf-bxp/cbpi4-InputControl",
    license="GPL-3.0",
    include_package_data=True,
    package_data={
        "": ["*.txt", "*.rst", "*.yaml", "*.json", "*.md"],
        "cbpi4-InputControl": ["*", "*.txt", "*.rst", "*.yaml", "*.json", "*.md"],
    },
    packages=["cbpi4-InputControl"],
    install_requires=[
        "gpiozero>=2.0",
        "lgpio>=0.2.0",
    ],
)
