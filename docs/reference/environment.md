# Environment variables


| Variable | Effect |
| --- | --- |
| `BAUTA_CONFIG` | The configuration directory, when `--config` isn't given. |
| `BAUTA_NOTIFY_URL` | The webhook, when `--notify-url` isn't given. |
| `BAUTA_MANIFEST_KEY` | Sign manifests, and verify their signatures. |
| `BAUTA_MASKING_THREADS` | Threads the native masker uses per job: a number or `auto`. Overrides `jobs.yaml`'s `maskingThreads`; see [masking threads](../guides/make-it-faster.md#masking-threads). |
| `BAUTA_REQUIRE_NATIVE=1` or `=0` | Overrides `jobs.yaml`'s [`requireNative`](jobs.md#file-level): `=1` stops a masked run wherever masking would run in Python, `=0` never does. |
| `BAUTA_NATIVE=0` | Mask in Python even where the extension is installed. |
| `BAUTA_PIPELINE=0` or `=1` | Force reading, masking and writing to take turns, or to overlap. |
| `BAUTA_START_METHOD=spawn` | Start each job's process afresh rather than fork it from the forkserver, about 200 ms slower a job. |

The last three are for diagnosis. The two maskers are tested to agree, so a difference `BAUTA_NATIVE=0` reveals is a bug worth reporting. By default the stages overlap only with the native masker, since overlapping Python masking costs about 2%.
