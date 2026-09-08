# 52 Woodland flood board — what last winter actually told us

*Analysis of November 2025 – April 2026. Written September 2026.*

This is the write-up of five layers of analysis on last winter's data. The short
version: **the alert rules the board shipped with would have stayed silent
through the biggest storm of the winter.** They have been rebuilt, and the
rebuilt rules would have given roughly two and a half days of warning on that
same storm. Along the way the data confirmed some things about the sump that are
reassuring, and exposed one assumption that everything else rests on.

---

## 1. What the data is

Four sources, aligned onto a common 10-minute clock covering 1 November 2025 to
30 April 2026 — 26,064 rows.

| Source | What it measures | How often it reports |
|---|---|---|
| Ambient weather station (DMATStation) | rain at the house | every 5 minutes |
| PumpFuse | each time the sump pump runs, and for how long | one record per run |
| Bridge Street gauge | creek level in downtown San Anselmo | when it changes |
| USGS 11460000 | creek level and flow at Ross, downstream | every 15 minutes |

Two of these are genuinely continuous. The other two are not, and it matters:

- **Ambient** and **USGS** are near-complete. Ambient had one gap over an hour
  in six months (1.6 h). USGS had three, all under 1.5 h.
- **Bridge Street reports only when the creek moves.** In dry weather it sends a
  reading every twelve hours; during storms it drops to every few minutes. Over
  the whole winter only 3.9% of ten-minute slots contain a real reading, but
  during wet periods that rises to 15.6%, and the median wait for a real reading
  falls from 173 minutes (dry) to 27 minutes (wet). The gauge densifies exactly
  when you need it, which is good design, but it means any "Bridge Street level"
  between storms is an interpolation, not an observation.
- **PumpFuse gaps are not missing data.** A gap means the pump did not run.

### Rainfall had to be reconstructed

The weather station does not report "how much rain fell in the last ten
minutes." It reports running totals that reset — daily at midnight, per-event
after a dry spell, yearly on 1 January. Rainfall per interval is the difference
between consecutive readings, with a reset treated as "counter went to zero,
then climbed to the current value."

Because that reset handling could easily be wrong, it was checked against an
independent counter. The daily counter and the yearly counter, differenced the
same way, disagree by **0.22 inches over six months — 0.61%**. The
reconstruction is sound.

Season total: **34.47 inches** across the window.

---

## 2. The storm catalog

Rain was cut into storms wherever six hours passed with none. That gives **40
events**; 24 of them exceed a tenth of an inch, the rest are traces.

The winter's shape, by month:

| Month | Rain (in) | Pump runs | Gallons | Bridge St peak (ft) | Ross peak (ft) |
|---|--:|--:|--:|--:|--:|
| Nov 2025 | 5.96 | 335 | 8,256 | 3.35 | 7.15 |
| Dec 2025 | 7.09 | 755 | 17,541 | 5.29 | 9.42 |
| **Jan 2026** | **8.80** | **1,278** | **30,003** | **7.38** | **12.12** |
| Feb 2026 | 7.89 | 904 | 21,211 | 6.90 | 11.51 |
| Mar 2026 | 0.04 | 1 | 21 | 1.76 | 4.78 |
| Apr 2026 | 4.69 | 280 | 6,175 | 2.72 | 6.07 |

March looked at first like a broken export — one pump run in a whole month. It
isn't: **March 2026 received four hundredths of an inch of rain.** The pump had
nothing to do.

The whole winter's peak was **7.38 ft at Bridge Street on 6 January 2026**, which
is 55% of the 13.3 ft "minor flood" line, and the Town's 6.5 ft "notify the
public" line was crossed exactly twice. Everything below is therefore calibrated
on a winter that was wet but never close to flooding.

---

## 3. The sump: what the run lengths mean

### The physics, which turns out to be simple

The pump empties a fixed slug of water from the pit each time it runs. If
nothing were flowing in, that would take a fixed time — and the shortest run
ever recorded is **14 seconds**. When water *is* flowing in, the pump has to
fight it, so the run takes longer.

Written out, with `V` the slug of water, `Qp` the pump's capacity and `Qin` the
inflow:

```
run length = V / (Qp − Qin)
```

The shortest run is the case where nothing is coming in, so `14 s = V / Qp`.
Substituting that back gives a genuinely useful result:

```
stress  =  1 − 14/run_length  =  Qin / Qp
```

**The "stress" number is simply what fraction of the pump's capacity the inflow
is using.** Stress 0.6 means water is arriving at 60% of what the pump can move.
Stress 1.0 is not a number the pump can reach — it is the point where inflow
equals capacity and the run never ends.

Three independent checks agree on this:

1. **Gallons per cycle.** Predicted ~22 from the pit's size; measured **22.4
   average** across 30 storms.
2. **Pit geometry.** A 36-inch pit holds 4.41 gallons per inch of depth, so a
   4.5-inch float travel is 19.8 gallons. The 14-second floor implies 19.6.
3. **Power draw.** The pump averages **1,394 watts against a 1,380 W rating**
   across 3,674 runs, with a standard deviation of only 2%. It is doing the same
   thing, at its rated duty point, every single time.

### How hard did the pump actually work?

Barely. Out of **3,553 runs last winter, six** reached stress 0.6, and those six
fall in just two episodes (16 November and 25 December). The worst single run —
134 seconds — implies inflow at **75 GPM, 90% of capacity**, but the worst
*sustained* fifteen minutes only reached 42.6 GPM, about half of capacity.

A real operational finding sits in that gap: when inflow rises, the pump does
**not** start more often. Starts per fifteen minutes flatten out at twelve
across every large storm. What changes is how long each run lasts — from the
usual 15–18 seconds up to 38, 63, 100, 134 seconds. **Run length is the signal;
run count saturates and will mislead you.**

### What we could not model

An attempt was made to predict stress from rainfall. It works in bulk and fails
where it matters. Rain explains stress reasonably across all runs, but if you
look only at the runs that were actually stressful, the relationship
disappears — statistically, essentially nothing. The model under-predicts those
six critical runs by a factor of two. The worst run of the winter followed only
0.08 inches of rain in the preceding ten minutes.

Consequently the "what rainfall rate would overwhelm the pump" question **cannot
be answered from this data**. The arithmetic says roughly 2.0–2.9 inches per
hour, but that is 1.3 to 1.9 times more intense than the heaviest ten minutes
ever recorded here, produced by a model that is known to under-read exactly this
regime. If anything the true figure is lower. Treat it as "we have never come
close, and we do not know where the edge is."

---

## 4. The creek

### Rain to creek level

Fitting Bridge Street's level against rainfall — using only readings taken while
the creek was rising, since a falling creek is just draining — gives a clear
answer about which rainfall window matters:

- **Six-hour rainfall is the dominant driver.** Twelve-hour is the best single
  window on its own, but in combination six-hour carries the most weight.
- The lag is **under an hour**. The creek responds almost immediately.
- Antecedent wetness matters (about a third the weight of the rain term).
- **Season-to-date rainfall does not matter at all.** It added essentially
  nothing in every layer it was tested in, and has been dropped everywhere.

A simpler model — predicting each storm's *peak* from its *total* rain plus how
wet the ground was at the start — reaches 82% of the variance with two inputs,
against 87% for the complicated version with five. The simple one is preferred.

### Ross predicts Bridge Street almost exactly

This is the most useful single result in the analysis.

```
Bridge Street level  =  0.767 × Ross level  −  1.96
```

Across 24 storms this fits with **R² = 0.9965** and a typical error of **0.10
feet**. It is straight — a curved fit adds nothing.

Why it matters: Bridge Street's gauge is event-based, sparse, occasionally
offline, and scraped from a county web page. Ross is USGS — real-time, every
fifteen minutes, reliable, and already on the dashboard. So the downtown creek
level can be read off a gauge that never goes quiet.

Converting the Town's action lines onto the Ross gauge:

| Town line | Bridge St | Equivalent Ross | 95% band | Reliable? |
|---|--:|--:|---|---|
| Stage 2 — notify public | 6.5 ft | **11.03 ft** | 10.74 – 11.32 | **Yes — inside observed range** |
| Stage 5 — flood horn | 13.0 ft | 19.50 ft | 19.07 – 19.93 | No — 1.6× beyond the data |
| NWS minor flood | 13.3 ft | 19.89 ft | 19.46 – 20.33 | No — 1.6× beyond the data |

Ross ran between 4.54 and 12.12 ft last winter, so **the notify line is a number
we have actually seen the creek approach**. The flood lines are extrapolation.

One reassuring cross-check on that extrapolation: applied to the January 1982
flood of record (Ross 19.81 ft, the highest in 59 years of USGS peaks), the
formula predicts Bridge Street at **13.24 ft** — almost exactly the 13.0 ft where
the Town says water reaches the building at 730 San Anselmo Avenue. The
extrapolation lands where independent local knowledge says it should.

### Early warning from the creek itself

Does the creek's rate of rise in the first three hours tell you where it will
end up? **On its own, no** — that relationship is weak. Where the creek *starts*
matters considerably more than how fast it is climbing. The rise rate does
predict how much *further* it will climb, which is useful once you know the
starting point. Only 9 of 24 storms even had enough early readings to test this,
because the gauge is quiet until the creek starts moving.

---

## 5. How good are the rain forecasts? (Not very)

Open-Meteo's forecasts were compared against what the station actually caught.

| | Captured | Correlation |
|---|--:|--:|
| Day-ahead, all days | **75.5%** | 0.81 |
| Three days ahead, all days | **49.6%** | 0.67 |
| Day-ahead, wet days only | 75.3% | 0.53 |
| Three days ahead, wet days only | 49.3% | **0.21** |

**The forecast runs dry, and worse the further out you look.** At three days on
wet days the correlation of 0.21 is close to no skill at all.

For the five wettest storms specifically, the day-ahead forecast under-called
them by **41%** and the three-day by **68%**. The single worst miss was 5 January
2026 — the day the creek crested — where 2.81 inches fell against a day-ahead
forecast of 0.62 inches and a three-day forecast of 0.25 inches. **It saw nine
percent of what arrived.**

This is why every forecast threshold in the rebuilt rules is multiplied by a
correction factor (1.35 for day-ahead, 2.0 for three-day) before being compared
to anything.

---

## 6. The rules: before and after

### Before

The board's original thresholds were replayed against the winter. Of fifteen
storms:

- **Eight fired no alert of any kind.**
- The largest storm of the winter — 5.54 inches, creek to 7.38 feet, past the
  Town's notify line — **was one of them.** Its heaviest hour reached 0.51
  inches against a 0.6-inch threshold; its rain rate never touched 0.75 in/hr;
  and the forecast was far too dry to trip the 2- and 3-inch forecast rules.
- When rules did fire, the typical warning was **0.7 to 2.2 hours** — enough to
  move a car, not to stage sandbags.
- The sump rules never fired at all (thresholds of 50% and 80% duty, against a
  season maximum of 50.8%).
- The creek rules never fired (they were set at flood stages the creek came
  nowhere near).

### After

The rebuilt ladder has five levels and draws on the creek gauge, the pump's run
lengths, and bias-corrected forecasts.

| Storm (by creek peak) | Rain | Peak ft | Highest tier | Warning before peak |
|---|--:|--:|---|--:|
| 2 Jan 2026 | 5.54 | 7.38 | **Act** | **63 h** (Watch at 98 h) |
| 14 Feb 2026 | 3.76 | 6.90 | Act | 1.3 h (Watch at 24 h) |
| 24 Dec 2025 | 1.15 | 5.29 | Act | 24 h |
| 20 Dec 2025 | 3.30 | 4.81 | Prepare | 24 h |
| 31 Dec 2025 | 3.23 | 4.46 | Prepare | 7 h |
| 18 Feb 2026 | 1.45 | 3.95 | Prepare | 45 h |
| 17 Feb 2026 | 0.95 | 3.67 | Act | 3.7 h |
| 12 Nov 2025 | 2.46 | 3.35 | Act | 0.3 h |
| 4 more storms | ≤1.9 | ≤2.72 | none | — |

**Eleven of fifteen storms now reach Prepare, eight reach Act.** The four that
still trigger nothing all peaked below 2.72 feet — well under any level worth
acting on, so silence is correct there.

False alarm rates, judged by whether the creek actually did anything that day:
**Prepare 8%** (one quiet day, and it reached 2.99 ft — a near miss, not a
blunder). **Act 44%**, which is high and worth watching. One rule as originally
drafted — "three consecutive lengthening pump runs" — fired on 2.6% of all runs
and produced a 62% false-alarm rate, because a 16→17→18 second ripple satisfied
it. Requiring the third run to reach 25 seconds cut it from 94 triggers to 16
while keeping every real storm.

Three rules could not be replayed and are flagged rather than quietly skipped:
the CW3E atmospheric-river outlook and NWS Flood Watch have no local archive, and
**the backup pump has never been instrumented at all** — every one of the 3,674
exported runs sits in one power mode matching the primary. Backup-pump start is
the top of the ladder and nothing can currently raise it.

---

## 7. December 2005, and what the sump is not for

**Water entered this basement from the north side in the 2005 flood, to roughly
two inches. That was before the sump existed. The defense against creek flooding
on that side is physical barriers, not the pump.** Nothing in this analysis
changes that. The sump handles groundwater and drain inflow; it is not a flood
barrier and its capacity is irrelevant to a creek that comes overland.

We tried to put a number on that event and largely could not:

- **USGS 11460000 has no reading for 31 December 2005.** The gauge was not
  operating: its annual peak record skips 1998–2009 entirely, and continuous
  data only resumes in November 2009. So the Ross→Bridge Street conversion cannot
  be applied to 2005 — there is no input to convert.
- For scale, the two nearest reference points on record are the **1982 flood of
  record (Ross 19.81 ft, 7,200 cfs)**, which converts to about 13.2 ft at Bridge
  Street, and **2017 (Ross 17.66 ft)** at about 11.6 ft. Published accounts
  describe 2005 as slightly below 1982.
- Independent accounts of 31 December 2005 put roughly **5,980 cfs** arriving at
  San Anselmo (about 4,105 cfs in the channel and 1,875 cfs over the streets),
  with **more than four feet of water on San Anselmo Avenue**, 140 businesses and
  290 homes damaged, and roughly $30 million in losses.
- **No public source gives a water-surface elevation or depth near Woodland
  Avenue.** The FEMA high-water-mark project summary contains no elevations and
  does not mention Woodland. The authoritative document is the FEMA Flood
  Insurance Study flood profile for San Anselmo Creek (Marin County FIS, current
  FIRM effective 17 March 2014), which has to be pulled from the FEMA Map Service
  Center; the Town also holds physical high-water-mark plaques from 2005 on
  buildings around town, and FEMA's estimated-BFE viewer may give a
  property-level number.

Getting the FIS profile for the reach near Woodland Ave is the one outstanding
piece that would turn "roughly two inches from the north" into a design figure
for the barriers.

---

## 8. What everything rests on, and what to measure

### The one assumption that moves everything

Every gallon, every inflow rate and every stress number in this report scales
directly with **how far the float travels before the pump switches off**. That
distance is recorded as 4.5 inches and is **assumed, not measured.**

| If the float travel is… | Pump delivers | Effect |
|---|--:|---|
| 3.0 in | 56.7 GPM | every inflow figure drops a third |
| 4.0 in | 75.5 GPM | drops 10% |
| **4.5 in (assumed)** | **85.0 GPM** | matches the 84 GPM nameplate |
| 5.0 in | 94.4 GPM | rises 11% |

The 4.5-inch assumption produces 84.98 GPM against a nameplate of 84 — agreement
so close it is either confirmation or coincidence. **Measuring the actual float
travel is the highest-value thing left to do**, and it takes a tape measure.

### Everything else worth measuring

1. **Instrument the backup pump.** It is the top alert tier and currently cannot
   fire. Either it never ran last winter or nothing was watching.
2. **Get the FEMA FIS flood profile** for San Anselmo Creek near Woodland Ave,
   for a real water-surface elevation at the house.
3. **Log rival rain forecasts.** The board now records NWS gridded QPF, CNRFC
   6-hourly QPF, and ECMWF / GFS / HRRR alongside Open-Meteo every run, so next
   spring can rank them instead of trusting a blend that captured 75% of the rain
   at one day and 50% at three.
4. **Re-check the Ross→Bridge Street relation after another winter.** R² of
   0.997 between two separate gauges is tight enough to be worth confirming
   before it is relied on.
5. **Watch the Act-tier false alarm rate** (currently 44%) and tighten if it
   becomes noise.

### Two things not to forget

- Every relationship here was fitted on a winter that **never approached
  flooding** — peak 55% of minor flood stage. Extrapolating to a real flood is
  not supported by this data.
- The pump had **at least half its capacity spare in every storm.** The thing
  that flooded this basement in 2005 was not inflow the sump could have pumped.
  It was the creek, from the north, and the answer to that is barriers.
