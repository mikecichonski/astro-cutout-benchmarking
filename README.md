# Astro Cutout Benchmarking

Repeatable benchmark harnesses for astronomical FITS cutout tools on Slurm
clusters and CANFAR. The repo focuses on runnable benchmark code and
documentation; generated results stay out of Git.

Read-only result charts live here:
[Google Sheets](https://docs.google.com/spreadsheets/d/1fqtih-dpWI-U3thO6vFlC2-0xWzZt9RkxW_pbnnUV74/edit?usp=sharing).

## What Is Here

- `astro_bench.py`: easy top-level entry point.
- `benchmarks/slurm/subfits/`: Slurm runner for `subfits`.
- `benchmarks/slurm/vlkb-soda/`: Slurm runner for VLKB-SODA `vlkb imcopy` and
  `vlkb imcopydav`.
- `benchmarks/canfar/`: CANFAR headless-session API submitter and in-container
  matrix runner.
- `patches/`: local VLKB-SODA patch needed for `rows-per-chunk` and local-only
  builds.
- `docs/outputs.md`: short guide to generated output files.

The local `subfits/`, `vlkb-soda/`, and `dug-results/` directories from the
original working copy are intentionally ignored. They are useful local
development material, but they include upstream repos, generated results, and
site/private config examples that should not be pushed to a public repo.

## Quick Start

List the available entry points:

```bash
python3 astro_bench.py --list
```

Inspect the underlying runner options:

```bash
python3 astro_bench.py slurm subfits --help
python3 astro_bench.py slurm vlkb run-location-matrix --help
python3 astro_bench.py canfar submit --help
```

Every benchmark writes to an explicit results directory. Keep that directory on
scratch/project storage and leave it untracked.

## Engines

This repo benchmarks cutout engines; it does not vendor full upstream engine
trees.

### subfits

Use an existing `subfits` executable, or clone/build it separately:

```bash
git clone https://github.com/Staveley-Smith/subfits.git
cd subfits
chmod +x subfits
realpath subfits
```

The resulting path is the value for `--binary`.

### VLKB-SODA

Use an existing `vlkb` binary if your cluster already has one. For a fresh
checkout with the local benchmarking changes:

```bash
git clone https://www.ict.inaf.it/gitlab/ViaLactea/vlkb-soda.git
cd vlkb-soda
git apply /path/to/astro-cutout-benchmarking/patches/vlkb-soda-rows-per-chunk-and-local-only.patch
```

On a Slurm cluster with AST and CFITSIO available:

```bash
make -C data-access/engine/src/common
make -C data-access/engine/src/vlkb NO_DAVIX=1 NO_CSV=1
realpath data-access/engine/src/vlkb/bin/vlkb
```

Build without `NO_DAVIX=1` when you want to benchmark `imcopydav` against HTTP
or object-storage URLs.

## Slurm Or Dug

The Slurm runners submit jobs with `sbatch`, wait for completion, and collect
`seff`/`sacct` metrics. Dug previously rejected submissions from `$HOME`, so
use scratch/project paths for `--submit-dir`, `--stage-dir`, and
`--results-root`.

Set up a cluster environment script first. For `subfits`, start from:

```bash
cp benchmarks/slurm/subfits/subfits-env.example.sh ~/subfits-env.sh
source ~/subfits-env.sh
```

For VLKB-SODA, create a similar script that loads your compiler, Python, AST,
CFITSIO, and optionally Davix modules.

### subfits Location Matrix

This runs start/middle/end cutouts and writes the same compact output format as
the VLKB-SODA runner:

```bash
python3 astro_bench.py slurm subfits run-location-matrix \
  --tool subfits \
  --binary /path/to/subfits \
  --source /data/aussrc_cutouts/datafiles/SB75060.fits \
  --stage-dir /scratch/$USER/astro-cutout-bench/staged \
  --results-root /scratch/$USER/astro-cutout-bench/results/subfits \
  --submit-dir /scratch/$USER/astro-cutout-bench/submit \
  --locations start middle end \
  --env-script ~/subfits-env.sh \
  --time 08:00:00 \
  --partition <partition> \
  --account <account> \
  --mem-big 8G \
  --mem-small 4G \
  --sbatch-arg=--exclusive \
  --sbatch-arg=--hint=nomultithread
```

### VLKB-SODA Location Matrix

This exercises `vlkb imcopy` on local FITS files:

```bash
python3 astro_bench.py slurm vlkb run-location-matrix \
  --binary /path/to/vlkb \
  --source /data/aussrc_cutouts/datafiles/SB75060.fits \
  --stage-dir /scratch/$USER/astro-cutout-bench/staged \
  --results-root /scratch/$USER/astro-cutout-bench/results/vlkb \
  --submit-dir /scratch/$USER/astro-cutout-bench/submit \
  --locations start middle end \
  --env-script ~/vlkb-env.sh \
  --time 08:00:00 \
  --partition <partition> \
  --account <account> \
  --mem-big 8G \
  --mem-small 4G \
  --sbatch-arg=--exclusive \
  --sbatch-arg=--hint=nomultithread
```

The default matrix covers 27 cases: 3 cutout locations crossed with 9 CPU,
memory, input-size, and cutout-size combinations. To make your own grid, add:

```bash
--matrix-cpus 1 2 4 8 16 32 64 \
--matrix-input-gib 10 20 40 80 160 \
--matrix-cut-fractions 0.1 0.25 0.5 0.75 \
--matrix-mem 8G \
--matrix-memory-label 8G
```

### VLKB-SODA Local Versus Remote Storage

Use `run-storage-chunk-matrix` to compare local `imcopy` and remote
`imcopydav`, including rows-per-chunk tuning:

```bash
python3 astro_bench.py slurm vlkb run-storage-chunk-matrix \
  --binary /path/to/vlkb \
  --local-source /data/aussrc_cutouts/datafiles/SB75060.fits \
  --remote-source 'https://ingest.pawsey.org.au/cutoutpublic/SB75060.fits' \
  --results-root /scratch/$USER/astro-cutout-bench/results/vlkb-storage \
  --submit-dir /scratch/$USER/astro-cutout-bench/submit \
  --cpus 64 \
  --mem 8G \
  --local-rows-per-chunk 10 100 1000 \
  --remote-rows-per-chunk 1 10 100 1000 \
  --cut-fractions 0.01 0.1 0.5 \
  --dimensions xyz \
  --locations start \
  --env-script ~/vlkb-env.sh \
  --time 08:00:00 \
  --partition <partition> \
  --account <account> \
  --sbatch-arg=--exclusive \
  --sbatch-arg=--hint=nomultithread
```

### Resume And Collect

If you disconnect, rerun the same command with `--session-dir` pointing at the
existing results session. Rows with a complete `result.json` are skipped.

To rebuild summaries from an existing Slurm manifest:

```bash
python3 astro_bench.py slurm vlkb collect \
  --manifest /scratch/$USER/astro-cutout-bench/results/vlkb/YYYYMMDD_HHMMSS_locxy/manifest.tsv
```

For failed-row diagnostics:

```bash
python3 astro_bench.py slurm vlkb scan-errors \
  --session-dir /scratch/$USER/astro-cutout-bench/results/vlkb/YYYYMMDD_HHMMSS_locxy
```

## CANFAR API

CANFAR headless sessions are useful for non-interactive batch runs. The
[CANFAR Python client](https://www.opencadc.org/canfar/latest/client/home/)
documents
[`canfar.sessions.Session.create`](https://www.opencadc.org/canfar/latest/client/session/)
for launching sessions, and the
[headless examples](https://www.opencadc.org/canfar/latest/client/examples/#headless)
show the command-and-exit workflow used here. A headless session runs a command
without a GUI while preserving data on `/arc` storage.

Install and authenticate locally:

```bash
python3 -m pip install --upgrade -r requirements-canfar.txt
canfar login cadc
```

Put this repo, the engine binary, and any local FITS inputs somewhere visible
inside CANFAR, for example:

```bash
/arc/projects/<project>/astro-cutout-benchmarking
/arc/projects/<project>/bin/subfits
/arc/projects/<project>/data/SB75060.fits
```

### Submit subfits With The API

```bash
python3 astro_bench.py canfar submit \
  --repo-dir /arc/projects/<project>/astro-cutout-benchmarking \
  --engine subfits \
  --binary /arc/projects/<project>/bin/subfits \
  --source /arc/projects/<project>/data/SB75060.fits \
  --target-mib 256 1024 4096 \
  --parallel-workers 1 2 4 8 \
  --cores 8 \
  --ram 32 \
  --wait \
  --print-logs
```

### Submit VLKB-SODA With The API

Local file benchmark:

```bash
python3 astro_bench.py canfar submit \
  --repo-dir /arc/projects/<project>/astro-cutout-benchmarking \
  --engine vlkb-soda \
  --binary /arc/projects/<project>/bin/vlkb \
  --mode imcopy \
  --source /arc/projects/<project>/data/SB75060.fits \
  --target-mib 256 1024 4096 \
  --parallel-workers 1 2 4 8 \
  --rows-per-chunk 10 100 1000 \
  --cores 8 \
  --ram 32 \
  --wait
```

Remote URL benchmark:

```bash
python3 astro_bench.py canfar submit \
  --repo-dir /arc/projects/<project>/astro-cutout-benchmarking \
  --engine vlkb-soda \
  --binary /arc/projects/<project>/bin/vlkb \
  --mode imcopydav \
  --source 'https://ingest.pawsey.org.au/cutoutpublic/SB75060.fits' \
  --target-mib 256 1024 \
  --parallel-workers 1 4 \
  --rows-per-chunk 1 10 100 \
  --cores 8 \
  --ram 32 \
  --wait
```

To test the exact command without creating a CANFAR session, add `--dry-run`.

You can also run the CANFAR matrix runner directly inside an existing CANFAR
terminal/notebook:

```bash
python3 benchmarks/canfar/run_canfar_matrix.py \
  --engine subfits \
  --binary /arc/projects/<project>/bin/subfits \
  --source /arc/projects/<project>/data/SB75060.fits \
  --target-mib 256 1024 \
  --parallel-workers 1 2 4 \
  --output-root /arc/projects/<project>/astro-cutout-benchmarking/results/canfar
```

## Expected Outputs

Slurm session outputs:

- `manifest.tsv`
- `manifest_enriched.tsv`
- `location_matrix_summary.csv`
- `location_matrix_summary.md`
- `error_scan.csv`, when `scan-errors` is run
- one `<condition>/result.json` per job

CANFAR session outputs:

- `manifest.tsv`
- `summary.csv`
- one `<condition>/result.json` per matrix row
- `<condition>/controller.stdout` and `<condition>/controller.stderr`

See [docs/outputs.md](docs/outputs.md) for columns and spreadsheet guidance.

## What To Vary

Useful independent variables:

- engine: `subfits`, `vlkb imcopy`, `vlkb imcopydav`
- source: local filesystem path or HTTP/object-storage URL
- cutout size: `--target-mib`, `--pixel-filter`, or matrix cut fractions
- cutout location: `start`, `middle`, `end`
- dimensions: x/y only versus x/y/z
- Slurm allocation: CPUs, memory, partition, account, node constraints
- in-job concurrency: `--parallel-workers`
- VLKB chunking: `--rows-per-chunk`

The runners record enough provenance in manifests and `result.json` files to
rebuild summaries later.

## Public Repo Hygiene

Ignored by default:

- generated result folders
- FITS inputs and staged FITS cutouts
- Slurm stdout/stderr and collection artifacts
- credentials and private keys
- local upstream checkouts

Before publishing, check:

```bash
git status --short
git ls-files
```

No result files, credentials, FITS cubes, or local upstream working trees should
appear in the tracked file list.

## License

This benchmark repository is distributed under GPL-3.0-or-later. Upstream
engines keep their own licenses: `subfits` is BSD-3-Clause, and VLKB-SODA is
GPL-3.0-or-later.
