"""Offline provider profile validation."""
import argparse
from . import check

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('command', choices=['check'])
parser.parse_args()
check()
