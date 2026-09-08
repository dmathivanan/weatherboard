# nowcast/

MRMS radar ingest and a pysteps nowcast for the house pixel.

## Setup

`eccodes` has no Python 3.14 wheel, so the library comes from `ecmwflibs`.
`findlibs` looks for a `lib`-prefixed name that ecmwflibs does not ship, so:

```
pip install numpy scipy pandas requests eccodes cfgrib ecmwflibs pysteps opencv-python-headless
mkdir -p nowcast/lib
cp <site-packages>/ecmwflibs/eccodes.dll nowcast/lib/libeccodes.dll
```

`mrms.bootstrap()` sets `ECCODES_HOME` to `nowcast/` and adds the ecmwflibs
directory to the DLL search path. On Linux/macOS the stock `eccodes` wheel works
and none of this is needed.

## Use

```
python nowcast/mrms.py              # print grid geometry, station pixel, basin mask
python nowcast/ingest.py once       # one live poll   (4 min wet / 20 min dry)
python nowcast/ingest.py loop       # continuous
python nowcast/backtest.py fetch    # download last winter's five storms
python nowcast/backtest.py report   # MRMS vs the Ambient gauge
python nowcast/nowcast.py run       # one nowcast from the live store
python nowcast/nowcast.py replay Nov16 Dec24
```

## Notes that cost time to learn

- **ecCodes is not thread-safe.** Decoding in a thread pool segfaults. Use
  processes (`backtest.py` uses 6).
- **The store holds disjoint periods**, so "the last 8 frames" can splice across
  months. `load_crops` refuses non-contiguous stacks.
- **The nowcast domain is smaller than the ingest crop** (+/-125 km vs 150 miles).
  The full crop does not finish inside the 4-minute cadence, and storms only
  travel ~75 km in the 90-minute lead anyway.
- `mrms.sqlite` is gitignored: it reaches hundreds of MB. Rebuild with
  `backtest.py fetch`, which skips frames already stored.
