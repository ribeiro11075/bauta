# Quickstart


Start from the configuration in [`example/starter/configuration/`](../../example/starter/configuration), from a clone or downloaded from GitHub:

```
mkdir configuration
cp example/starter/configuration/*.yaml configuration/
```

Edit `configuration/connections.yaml` and `configuration/jobs.yaml` for your databases, then supply the credentials they reference:

```
export SOURCE_DB_PASSWORD=...  TARGET_DB_PASSWORD=...  MASKING_KEY=...

bauta validate           # check the configuration, offline
bauta run --dry-run     # check connections and tables, moving nothing
bauta run                # run every job once
```

To see it work without any of that, the demos in a clone of this repository use throwaway SQLite databases:

```
git clone https://github.com/ribeiro11075/bauta.git && cd bauta
pip install -e ".[fpe]"

python example/walkthrough/demo.py       # the whole workflow: discover, subset, audit, mask, verify, synthesize
python example/incremental/demo.py       # streaming and incremental loads
python example/masking/demo.py           # masking, discovery and a subset, from Python
python example/native-masking/demo.py    # Python against Rust, and Rust on one core against all of them
```


The [examples](../../example/README.md) describe what each demo does, and the [guides](../guides/mask-a-table.md) take each task in turn.
