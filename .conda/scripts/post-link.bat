@echo off
setlocal enabledelayedexpansion

REM upgrade pip
"%PREFIX%\python.exe" -m pip install --upgrade pip

REM install extra deps
"%PREFIX%\python.exe" -m pip install paho-mqtt opentrons_shared_data
"%PREFIX%\python.exe" -m pip install git+https://github.com/Xuwznln/pylabrobot.git
