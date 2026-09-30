"""bauta under Dagster: an asset per bauta job, depending on its
predecessors' assets, so the lineage graph shows what each copy is built
from and each job materializes, fails and retries on its own.

Two jobs over the same assets:

- `bauta` every 15 minutes, each asset an incremental run of its job.
- `bauta_full_refresh` weekly, each asset its job's whole source, swapped in:
  what takes rows deleted in production out of incremental copies.

Load it with `dagster dev -f dagsterDefinitions.py`, with bautaTasks.py
beside it and $BAUTA_CONFIG set. Assets for different jobs run side by side,
as `run --job` locks only its own job; the `bauta` concurrency key limits how
many at once, standing in for `workers`:

    dagster instance concurrency set bauta 4

The masking key is read from the environment Dagster runs in, as bauta reads
it: set MASKING_KEY there, from your secrets manager.
"""
from __future__ import annotations

import re
from typing import Any, List

from dagster import (AssetExecutionContext, AssetKey, AssetSelection, AssetsDefinition, Config, Definitions, Failure, ScheduleDefinition, asset,
                     define_asset_job)

from bautaTasks import command, jobGraph, runCommand, shellCommand

GROUP = 'bauta'


class BautaRun(Config):
    """Whether a materialization loads the job's whole source and replaces its copy."""

    fullRefresh: bool = False


def assetName(job: str) -> str:
    """A job's name as an asset's, which takes letters, digits and _."""

    return re.sub(r'[^A-Za-z0-9_]', '_', job)


def bautaAsset(job: str, predecessors: List[str]) -> AssetsDefinition:
    """The asset `bauta run --job <job>` materializes. bauta's exit code decides
    whether it succeeded; its one-line errors become the failure's description.
    """

    @asset(name=assetName(job), deps=[AssetKey(assetName(predecessor)) for predecessor in predecessors], group_name=GROUP,
           op_tags={'dagster/concurrency_key': 'bauta'}, description='bauta job {}'.format(job))
    def materialize(context: AssetExecutionContext, config: BautaRun) -> None:

        argv = command('run', job=job, fullRefresh=config.fullRefresh)
        context.log.info(shellCommand(argv))
        result = runCommand(argv)
        if result.stdout.strip():
            context.log.info(result.stdout.strip())
        if result.returncode != 0:
            raise Failure(description='bauta exited {}: {}'.format(result.returncode, result.stderr.strip() or 'see the run log'),
                          metadata={'exit code': result.returncode})

    return materialize


GRAPH = jobGraph()

ASSETS = [bautaAsset(job, predecessors) for job, predecessors in GRAPH.items()]

INCREMENTAL = define_asset_job('bauta', selection=AssetSelection.groups(GROUP))

FULL_REFRESH_CONFIG: Any = {'ops': {assetName(job): {'config': {'fullRefresh': True}} for job in GRAPH}}

FULL_REFRESH = define_asset_job('bauta_full_refresh', selection=AssetSelection.groups(GROUP), config=FULL_REFRESH_CONFIG)

defs = Definitions(
    assets=ASSETS,
    jobs=[INCREMENTAL, FULL_REFRESH],
    schedules=[ScheduleDefinition(job=INCREMENTAL, cron_schedule='*/15 * * * *'),
               ScheduleDefinition(job=FULL_REFRESH, cron_schedule='0 3 * * 0')],
    )
