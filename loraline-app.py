#!/usr/bin/env python3
"""Double click this.

It opens loraline in a browser window. Everything else, including which serial
port the radio is on, it works out for itself.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from loraline.app import main
except ImportError as missing:
    raise SystemExit(
        f"loraline needs one or two small pieces installed first: {missing}\n\n"
        "    pip3 install pyserial pynacl\n")

raise SystemExit(main())
