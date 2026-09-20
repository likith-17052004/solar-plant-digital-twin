# Solar Plant Digital Twin

**A physics-based digital twin of a 100 MW photovoltaic solar plant — it doesn't
just predict output, it tells you which block is underperforming and why.**

Built in pure Python standard library. No NumPy, no pandas, no pvlib, no
dependencies at all.

![The plant, 20 inverter blocks and 200,100 modules, rendered in 3D](docs/images/01-aerial.jpg)

---

## What makes it a *twin* rather than a simulator

A simulator answers *"what should this plant produce?"*. A twin answers
*"what is this plant producing that it shouldn't be?"*

Every block carries its own state — how dirty it is, how old it is, what's
wrong with it. The model runs **twice** on every timestep: once assuming a
perfectly healthy plant, once under the plant's real declared state. The
difference between them, block by block, is the product.

![A tripped inverter found and quantified](docs/images/02-fault-detection.jpg)

Here BLK-007's inverter has tripped. The twin didn't need telling: it compared
each block's measured output against the **fleet median** and found one reading
100% below its peers, worth 3.99 MW. BLK-013 and BLK-019 are flagged amber for
a string outage and a patch of soiling.

Comparing against the *median* rather than a fixed threshold is the whole
trick. If the irradiance input is 12% low, every block reads 12% low — a fixed
threshold would flag all twenty. Dividing by the median cancels anything common
to the plant and leaves only what's specific to one block.

## Every megawatt is accounted for

![The loss waterfall](docs/images/03-loss-waterfall.jpg)

From nameplate irradiance down to the grid, each loss attributed to a named
cause — glass reflection, cell temperature, soiling, degradation, string
outages, inverter clipping, transformers, cables, auxiliaries. Nothing
disappears into an unexplained "derate factor". **The bars close exactly**, to
within 1e-9 MW, and there's a test that proves it.

## It runs on real weather

![Volumetric clouds over the site](docs/images/06-sky-and-clouds.jpg)

Real hourly weather for Pavagada, Karnataka from the Open-Meteo archive, and
real rainfall driving a soiling model — so the array gets dirty through the dry
season and washes clean in the monsoon, exactly as the actual site does:

| Date | Fleet soiling | Cleaning storms in the last 45 days |
| --- | --- | --- |
| 20 Feb | 6.89% | 0 |
| 15 May | 4.07% | 2 |
| 10 Sep | 0.00% | 6 |

![Ground level, between the rows](docs/images/05-panel-rows.jpg)

---

## How it works

The physics pipeline. Every stage is a published, citable model — nothing here
is a fitted curve or an invented fudge factor:

```mermaid
flowchart TD
    W["Open-Meteo archive<br/>DNI · DHI · GHI · air temp · wind · rainfall"]
    S["Solar position<br/>PSA algorithm"]
    P["Plane-of-array irradiance<br/>Liu and Jordan transposition"]
    G["Row shading and glass reflection<br/>Passias-Kallback · ASHRAE IAM"]
    ST["Per-block state<br/>soiling · age · declared faults"]
    D["Per-block DC power<br/>PVWatts v5 · Sandia cell temperature"]
    I["Inverter AC<br/>clipping · thermal derating"]
    N["Transformers · cables · auxiliaries"]
    E["Net export at the point of interconnection"]

    W --> P
    S --> P
    P --> G
    G --> D
    ST --> D
    D --> I
    I --> N
    N --> E
```

And the twin loop — the bit that turns a model into a diagnosis:

```mermaid
flowchart LR
    subgraph believes["What the twin believes"]
        direction TB
        NOM["Nominal state<br/>every block healthy"] --> MOD["Model run"]
    end
    subgraph doing["What the plant is actually doing"]
        direction TB
        DEC["Declared state<br/>plus injected faults"] --> ACT["Actual run"]
        ACT --> MEA["Measurement<br/>plus sensor noise"]
    end
    MOD --> CMP{"Peer comparison<br/>against the fleet median"}
    MEA --> CMP
    CMP --> OUT["Flagged blocks<br/>estimated MW shortfall"]
```

---

## Run it

Python 3.10 or newer. Nothing to install.

```bash
python3 -m solar_twin.server
```

Then open <http://127.0.0.1:8000>. Drag to orbit, click any block to inspect
it, and press **Load weather** for a real day at the real site.

Run the test suite — 236 tests, no test dependencies either:

```bash
python3 -m unittest discover -s tests
```

---

## What it models — and what it doesn't

This is the part most simulation projects leave out, so it goes near the top
instead. **Every result carries explicit `warnings` and `assumptions` fields**
describing its own limits, and those propagate all the way to the browser.

**Modelled:** PVWatts v5 DC · Sandia cell temperature · PSA solar position ·
Liu and Jordan transposition · row-to-row shading · ASHRAE incidence-angle
losses · Kimber soiling from real rainfall · module warranty degradation ·
inverter clipping and thermal derating · transformer, cable and auxiliary
losses · export curtailment · energy, specific yield and IEC 61724-1
performance ratio · per-block fault detection · SQLite history.

**Deliberately not claimed:** the plant is fictional and this is not a
certified engineering tool. Clouds are visual only and do not attenuate the
irradiance driving the physics. Every block sees identical weather — no cloud
shadow crosses the site. A block is the finest resolution, so string-level
mismatch and MPPT shifts aren't modelled. Sub-hourly values are interpolated,
which adds resolution but not information. Measurement is synthetic, generated
from a declared fault state, and never presented as real telemetry.

**Full details:** [docs/technical-reference.md](docs/technical-reference.md) —
the complete build, step by step, with the paper behind every model and an
honest account of the bugs found along the way.

---

## Contact

Built by **Likith Muninarakala**.

- Email — <likithmuni.2004@gmail.com>
- GitHub — [@likith-17052004](https://github.com/likith-17052004)

Questions, feedback, or interested in licensing it for commercial use? Get in
touch — I'd genuinely like to hear from you.

## Licence

Copyright © 2026 Likith Muninarakala. **All rights reserved.** See
[LICENSE](LICENSE).

Published for evaluation and reference. It is *not* open source: reading it and
running it locally are fine, anything else needs a written licence. Commercial
licences are available — contact me.

The scientific models implemented here are published, public science and are
not claimed by that copyright; only this implementation of them is. three.js
and Google Fonts load from third-party CDNs at runtime under their own
licences.
