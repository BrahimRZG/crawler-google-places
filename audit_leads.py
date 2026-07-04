# .github/workflows/audit.yml
# Sharded lead audit: 8 parallel jobs, each ~400 leads (~75 min vs 10h serial).
# Results survive cancellation (if: always) and resume across re-runs (cache).

name: Lead Audit (Sharded)

on:
  workflow_dispatch:

jobs:
  audit:
    runs-on: ubuntu-latest
    timeout-minutes: 300
    strategy:
      fail-fast: false           # one shard dying must not kill the others
      matrix:
        shard: [0, 1, 2, 3, 4, 5, 6, 7]
    env:
      PSI_API_KEY: ${{ secrets.PSI_API_KEY }}
      SHARD_INDEX: ${{ matrix.shard }}
      SHARD_TOTAL: 8

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.10"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      # Restore partial results from a previous (canceled/failed) attempt.
      # The script must skip any lead whose URL/place_id already exists
      # in results_shard_${SHARD_INDEX}.csv.
      - name: Restore partial results
        uses: actions/cache@v4
        with:
          path: results_shard_${{ matrix.shard }}.csv
          key: audit-shard-${{ matrix.shard }}-${{ github.run_id }}
          restore-keys: |
            audit-shard-${{ matrix.shard }}-

      - name: Run audit shard
        run: python audit_leads.py

      # Runs even if the job is canceled or times out — partial CSV is saved.
      - name: Upload shard results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: results-shard-${{ matrix.shard }}
          path: results_shard_${{ matrix.shard }}.csv
          if-no-files-found: warn

  merge:
    runs-on: ubuntu-latest
    needs: audit
    if: always()                  # merge whatever shards produced, even partials
    steps:
      - uses: actions/download-artifact@v4
        with:
          pattern: results-shard-*
          merge-multiple: true

      - name: Merge shards
        run: |
          python - <<'EOF'
          import csv, glob
          rows, header = [], None
          for f in sorted(glob.glob("results_shard_*.csv")):
              with open(f, newline="", encoding="utf-8") as fh:
                  r = list(csv.reader(fh))
                  if not r: continue
                  if header is None: header = r[0]
                  rows.extend(r[1:])
          with open("audit_results_merged.csv", "w", newline="", encoding="utf-8") as fh:
              w = csv.writer(fh)
              w.writerow(header or [])
              w.writerows(rows)
          print(f"Merged {len(rows)} rows")
          EOF

      - name: Upload merged results
        uses: actions/upload-artifact@v4
        with:
          name: audit-results-merged
          path: audit_results_merged.csv
