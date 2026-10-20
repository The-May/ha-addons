#!/bin/bash

# exec: python replaces the shell and becomes the main process,
# so it receives the stop signal from Home Assistant and shuts down quickly.
exec python3 /app/plotter.py