"""Running jobs and what they leave behind. `runner` runs cycles in
`dependencyGraph` order; `workers` gives each job a process of its own, in
which `pipeline` moves its rows, in slices `partitions` divides where a job
asks; `keys` checks masking keys before a run;
`memory` keeps run state and watermarks, and `reporting` records history and
manifests and sends notifications.
"""
