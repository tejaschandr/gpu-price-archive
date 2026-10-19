# gpu-price-archive

Point-in-time archive of GPU rental price indices and Kalshi compute markets: scheduled collection,
content-addressed raw snapshots, deterministic table rebuilds, and a hash-chained run manifest
timestamped on Bitcoin with OpenTimestamps.

**Stack:** Python 3.11+ (standard library only), GitHub Actions, OpenTimestamps, unittest.

```bash
python -m unittest discover -s tests      # offline; a test that touches the network fails
python -m cim.run --data-dir /tmp/cim     # collect once into a scratch directory
python -m cim.verify --data-dir /tmp/cim  # hashes, manifest chain, byte-identical rebuild
```

The collected data lives in a private repository. Test fixtures are synthetic, except
`tests/fixtures/getdeploying__nvidia-h100.csv` (GetDeploying, CC BY 4.0).
