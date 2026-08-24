#!/bin/sh
set -eu
python3 -m compileall -q autolearn tests
python3 -m unittest discover -s tests -v
