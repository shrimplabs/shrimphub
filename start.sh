#!/bin/bash
cd "$(dirname "$0")"
exec .venv/bin/python3.12 swarm_runner.py api
