# Patches

These patches are included so the benchmark repo can be re-run without
committing a full private working copy of `vlkb-soda`.

- `vlkb-soda-rows-per-chunk-and-local-only.patch` is the complete local diff
  from the `vlkb-soda` checkout at the time this repository was prepared. It
  adds the `rows-per-chunk` argument used by the remote-storage benchmark and
  makes a local-only `NO_DAVIX=1 NO_CSV=1` build possible.
- `vlkb-local-only-build.patch` is the earlier local-only build patch that was
  already present in the working tree. Keep it for provenance; prefer the
  complete patch above for fresh reruns.

Apply the complete patch from the root of a fresh `vlkb-soda` checkout:

```bash
git apply /path/to/astro-cutout-benchmarking/patches/vlkb-soda-rows-per-chunk-and-local-only.patch
```
