# Benchmark Outputs

Generated results are intentionally ignored by Git. Store them on scratch,
project storage, or CANFAR `/arc` storage, then copy only selected summaries to
a publication location such as the shared Google Sheet.

## Slurm Outputs

`run-location-matrix` and `submit` create one timestamped session directory.
The most useful files are:

- `manifest.tsv`: one row per submitted Slurm job, including source, cutout,
  job ID, output directory, memory label, CPU count, and notes.
- `manifest_enriched.tsv`: `manifest.tsv` joined with parsed `seff`, `sacct`,
  CPU model, and runner metadata.
- `location_matrix_summary.csv`: compact table for spreadsheet upload.
- `location_matrix_summary.md`: the same compact table for quick terminal or
  GitHub preview.
- `error_scan.csv`: one row per run with error category and log excerpt.
- `<row>/result.json`: raw benchmark timing summary for that condition.

## CANFAR Outputs

`benchmarks/canfar/run_canfar_matrix.py` creates one session directory under
`--output-root` with:

- `manifest.tsv`: one row per local benchmark command executed in the headless
  container.
- `summary.csv`: compact timing summary parsed from each row `result.json`.
- `<row>/result.json`: raw timing summary written by the common runner.
- `<row>/controller.stdout` and `<row>/controller.stderr`: stdout/stderr from
  the command that launched the row inside the CANFAR container.

## Recommended Spreadsheet Columns

For quick charting, start with:

- engine or tool
- mode (`subfits`, `imcopy`, `imcopydav`)
- source kind (`local`, `remote`)
- input file size or label
- cutout size or target MiB
- cutout location
- cutout dimensions (`xy`, `xyz`)
- CPU count or CANFAR cores
- parallel worker count
- requested memory
- rows per chunk, for VLKB-SODA
- median seconds
- median MiB/s
- peak memory, where Slurm `seff`/`sacct` is available
