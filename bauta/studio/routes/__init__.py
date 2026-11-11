"""Studio's HTTP API, a module for each part of the console, each building
its router from the Context the app gives it.
"""
from . import accounts, configuration, jobs, proof, runs

MODULES = [accounts, runs, jobs, proof, configuration]
