# Procurement Guide — equipping the cricAI physical rig (India)

**Research date: 12–13 Jul 2026.** Prices are verified India street prices as seen on the
named retailers on that date — treat them as a snapshot; re-verify before ordering.
Companion to `docs/runbooks/physical_setup.md` (build order) — this document answers
*what to buy, why it meets the spec, and what it costs*.

## Executive summary

| Tier | Total | What it is |
|---|---|---|
| **Recommended** | **₹11.2–11.8 lakh** | 7× DJI Osmo Action 5 Pro fleet, Leverage Master Digi Pro + feeder, RTX 5060 Ti 16GB build, mirrored 12TB storage, evening lighting kit, full calibration/field kit |
| Budget | ₹5.9–6.4 lakh | 7× Osmo Action 4, Master Digi Plus (no feeder), refurb tower + used RTX 3060 12GB, 8TB storage (stock risk), **daytime-only** (no lights) |
| Premium | ₹17–19 lakh | GoPro Hero 13 + Labs fleet, Leverage Yantra 3-wheel, RTX 5080 + 64GB, 2×16TB Pro, permanent lighting, iPad — buys speed and margin, not new capability |

**Recommended purchase order (phased — matches the backlog's own V1→V3 phasing):**

- **Buy now — V1 MVP, ~₹7.6–7.9 lakh**: 4 cameras (C1–C4) + mounts/protection/cards/power,
  bowling machine bundle, full server, **full storage** (see market warning), calibration
  essentials, electrician-installed pitch-side RCD circuit. Run daytime-only (₹0 lighting,
  fully spec-compliant).
- **Buy later — leg-spin + evening, ~₹3.5–3.9 lakh**: 3 more cameras (C5–C7) + second
  protection window + accessories (~₹1.3L), optional spare body (₹25k), evening lighting
  kit (~₹1.8L — skip entirely if daytime scheduling holds).

**2026 market conditions that change the usual advice:**

- **HDD shortage**: retail HDD prices rose ~46% in 4 months; WD NAS drives largely sold out
  through CY2026. **Do NOT defer storage** — it is the one deferral that costs money.
- **DRAM/NAND shortage**: DDR5 kit prices ~tripled from Oct-2025. Buy the server's RAM and
  SSD first within the build.

## Purchase gates & cross-check warnings (read before ordering)

1. **C7 240fps gate**: the Osmo Action 5 Pro verifiably records 1080p240, but manual
   shutter *inside the dedicated slow-motion mode* is unconfirmed on DJI's spec sheet.
   **Buy ONE body first**, verify you can hold ≥1/1000s shutter at 240fps (and at 120fps),
   then order the rest of the fleet. If 240fps shutter is auto-only, the fallback is
   GoPro Hero 13 + Labs firmware for C7 only.
2. **Set cameras to ~80 Mbps** (or budget for the 16TB storage tier): the storage sizing
   assumes ~80 Mbps encode; DJI's max 120 Mbps overshoots the 90-day window on 12TB drives.
   One codec setting reconciles the two categories.
3. **microSD→SD adapters ×2 (~₹200)**: the UGREEN readers take one microSD + one SD each;
   two adapters let 4 cards offload at once. Many Indian SanDisk microSD SKUs ship without
   adapters.
4. **One RCD point, not two**: the machine category's RCCB reel and the lighting category's
   RCCB box overlap — the electrician-installed pitch-side IP65 RCD box (~₹12–15k, pulled
   into the V1 buy) is the single source of protected outdoor power for machine, lights and
   charging. Skip the reel's built-in RCCB tier if the box exists.
5. **Evening peak load** (~2–2.5 kW: lights + machine + charging) fits one dedicated 16A
   circuit — but only the electrician-installed circuit. The electrician is mandatory the
   moment lights arrive.
6. **UPS/PSU pairing**: the APC BVX1600 outputs simulated sine; the Corsair RM750 is
   active-PFC. Works in practice at ~55% load, but if the server ever hard-resets on mains
   dropout, upgrade to a pure-sinewave UPS (~₹15–20k).
7. **Lights stay outside the netting**: the studio COBs are not impact-rated; a 130 kph ball
   carries ~100 J. Stands sit outside the net corridor, always.
8. **C7 sync**: its tight 1–1.5 m framing may not see a mid-pitch flash — use the chained
   double-flash procedure (second flash inside C7's field, visible also to one fleet camera).

---

## 1. Cameras, mounts, protection — recommended ₹3.05L (₹3.3–3.5L with spare)

**Verdict**: a homogeneous **7× DJI Osmo Action 5 Pro** fleet meets every hard spec,
verified against DJI's own spec pages: 1920×1080 @ 120/240 fps, electronic shutter to
1/8000 s with Pro-mode manual exposure (set 1/1000–1/2000 for the ≤2-ball-diameter smear
spec), 120 Mbps MP4, records while USB-C powered, no overheat concern at 1080p120 outdoors
(unlike GoPro). Record-then-upload + LED-flash sync is exactly what action cams are built
for. The honest C7 finding: **nothing affordable does sustained 1080p240 through optical
zoom** (camcorders top out ≤120fps; RX100-class HFR is a 4–7 s buffered burst; iPhone 240fps
is wide-lens-only) — so C7 is the same body close-mounted 1–1.5 m from release behind
polycarbonate, which the spec explicitly allows.

| Item | Pick | Qty | Price (seen Jul 2026) | Where |
|---|---|---|---|---|
| C1/C5 side-on | DJI Osmo Action 5 Pro (Standard Combo) | 2 | ₹35,990 ea | Reliance Digital, Flipkart, DJI India |
| C2/C3/C6 pitch-axis | DJI Osmo Action 5 Pro | 3 | ₹35,990 ea | same |
| C4 overhead | DJI Osmo Action 5 Pro + SmallRig 2164 clamp on net-frame top rail + steel tether | 1 | ₹35,990 + ₹1,300 + ₹300 | Flipkart/Amazon.in |
| C7 wrist (240fps) | DJI Osmo Action 5 Pro, close-mounted behind 10mm PC window | 1 | ₹35,990 | same |
| Spare/C8 (optional) | DJI Osmo Action 4 | 1 | ₹24,990 | Flipkart |
| Clamp mounts | SmallRig 2164 crab-clamp + ballhead (15–40mm jaws) | 5 | ₹1,300 ea | Flipkart, Amazon.in |
| Safety tethers | stainless camera lanyards | 7 | ~₹280 ea | Amazon.in |
| Ball-corridor protection | DIY: 10mm **solid** polycarbonate (Lexan-type) ~30×30cm angled windows on aluminium frames, lens 5–10cm behind, sheet angled 10–15° | 2 | ~₹280/sq ft + frame ≈ ₹2,500–4,000 total | IndiaMART PC-sheet suppliers, local Lexan dealers |
| microSD | SanDisk Extreme 256GB V30 A2 (≈4 sessions/card at ~55GB/hr) | 7 | ₹4,200 ea | Flipkart, Amazon.in |
| Session power | Ambrane Stylo 10K 20W PD powerbank + right-angle USB-C cable, velcroed below each clamp | 7 | ₹829–1,599 ea | Flipkart, Blinkit |
| Card readers | UGREEN 80888 2-in-1 USB-C (SD+microSD, 5Gbps) | 2 | ₹929 ea | ugreenindia.com, Amazon.in |

- **Budget alt**: 7× DJI Osmo Action 4 @ ₹24,990 — identical fps/shutter spec sheet, older
  sensor, shorter battery (more powerbank dependence). Saves ~₹77k. *Avoid* SJCam/Akaso: no
  verifiable manual shutter in HFR modes; some interpolate frames.
- **Premium alt**: GoPro Hero 13 Black fleet (₹37–45k ea) + Labs firmware — exact numeric
  shutter steps and QR timecode sync, at the cost of GoPro's known heat throttling on long
  static recordings. Don't mix brands within the tracking set.
- Protection premium: 12mm PC + steel mesh deflector hood (~₹7–9k) if C7 sits within 2 m of
  release.

## 2. Bowling machine — recommended ₹2.10–2.15L

**Verdict**: **Leverage Master Digi Pro** (2-wheel, Hyderabad-made) — verified 60–160 kph in
1-kph digital increments (maps 1:1 onto the software's `machine_settings.speed_kph` logging),
10 swing/spin levels each way for the `variation` field, head tilt for `length`, leather +
dimpled machine balls, auto-feeder compatible, 1-year all-parts warranty with domestic
service (vs BOLA's Bristol import lead times). Honest leg-spin caveat: a 2-wheel machine
*simulates* googly by reversing differential — a 3-wheel Yantra (₹2.65L+GST) genuinely
produces top-spinner/googly with independent overspin; that is the premium upgrade when
V3 leg-spin work intensifies.

| Item | Pick | Qty | Price | Where |
|---|---|---|---|---|
| Machine | Leverage Master Digi Pro (bundle with feeder) | 1 | ₹1,47,000 (₹1.4L+GST); bundle ≈ ₹1,88,000 | bowlingmachine.co.in, sportswing.in |
| Auto-feeder | Leverage 36-ball Auto Feeder (≈6 overs unattended) | 1 | in bundle (₹33–41k standalone) | same |
| Machine balls | Leverage Dimple 12×120g + 12×140g (OEM protects wheels + warranty) | 24 | ~₹380–450/ball ≈ ₹10,500 | Leverage direct, Amazon.in |
| Power + e-stop | 16A 25m 3-core IP54 reel with switched socket at the operator position (documented e-stop); RCD from the pitch box (gate #4) | 1 | ₹3,500–6,500 | IndiaMART industrial reels |
| Spares reserve | post-warranty wheel pair + controller budget | — | ₹10,000 | Leverage Hyderabad |

- **Budget**: Master Digi Plus ₹1.15L+GST, hand-fed — loses the feeder (needs a dedicated
  feeder-person) and app remote. **Premium**: Leverage Yantra 3-wheel ₹2.65L+GST (true
  spin variations) or Yantra e3 ₹3.5L with built-in feeder/app.
- Optional: 12V deep-cycle battery (Exide 60–80Ah, ₹6–9k) — the Digi runs battery-only,
  removing the mains run entirely for daytime sessions.

## 3. Lab server + UPS — recommended ₹1.66–1.76L

**Verdict**: custom AM5 build. The spec's "RTX 4060 Ti 16GB / 4070" is superseded in
Jul-2026 India by the **RTX 5060 Ti 16GB** — same-or-cheaper (₹62–66k live at PrimeABGB),
strictly better. 16GB VRAM matches ultralytics guidance (8GB min, 16GB+ preferred; YOLO11
trains at ~2× YOLOv5 memory) for yolo-m/l at imgsz 960–1280. MediaPipe is CPU-first —
the 8C/16T Ryzen carries it plus 7 parallel ffmpeg jobs.

| Item | Pick | Price | Where |
|---|---|---|---|
| GPU | Gigabyte RTX 5060 Ti 16GB GDDR7 (Eagle/Windforce OC) | ₹62–66k | PrimeABGB (in stock), MDComputers |
| CPU | AMD Ryzen 7 7700 (8C/16T, 65W, cooler incl.) | ₹27–31k | TLG, PC Studio, Amazon.in |
| Motherboard | MSI B650 Gaming Plus WiFi (2× M.2, 4 DIMM, 2.5GbE) | ₹17–23k | Amazon.in, MDComputers |
| RAM | 32GB (2×16) DDR5-5600 — **buy first** (shortage pricing) | ₹15–19k | MDComputers, EliteHubs |
| NVMe | Crucial P3 Plus 2TB Gen4 — **buy first** | ₹15k | Computech, MDComputers |
| PSU + case | Corsair RM750 Gold + mesh-airflow ATX (45% headroom; covers a future 5080) | ₹10k + ~₹4k | MDComputers |
| UPS | APC BVX1600LI-IN (1600VA/900W; server ~420W peak + network at ~55% load) | ₹10.5–11k | Amazon.in, IndiaMART |
| Console | Lenovo 21.5" FHD + Logitech combo (or ₹0: any TV + borrowed keyboard, then headless SSH) | ~₹9.5k | Amazon.in |

- **Budget**: refurb tower + used RTX 3060 12GB (₹12–20k used market) ≈ ₹60–80k total —
  trains yolo-s/m fine. **Premium**: RTX 5080 (₹1.24L+) — 2.5–3× throughput, same 16GB.
- NVMe reality check (from cross-audit): at real DJI bitrates the 2TB NVMe holds ~4–5
  sessions — the pipeline archives to the HDD tier; that is by design.
- BIOS: enable auto-power-on after outage (pairs with the UPS).

## 4. Network + storage — recommended ₹1.87L (storage is 94% — shortage artifact)

**Sizing math (verified)**: ~80 Mbps × 3600 s = 36 GB/camera-hour → 7 cams × 60 min ≈
**250 GB/session** → 4.5 sessions/week × 13 weeks (90-day raw window) ≈ **14.5 TB raw**
+ 2.4 TB clips ≈ 17 TB full-spec. The 12TB mirrored pair covers the full spec **if cameras
are set to ~80 Mbps** (gate #2); the 16TB pair covers it at any bitrate.

| Item | Pick | Qty | Price | Where |
|---|---|---|---|---|
| Switch | TP-Link TL-SG108E 8-port gigabit Easy Smart (2.5GbE rejected: ingest is card-reader-bound at ~118MB/s parity) | 1 | ₹2,039 | Moglix, Amazon.in |
| Pitch WiFi | TP-Link EAP225-Outdoor AC1200 IP65, PoE injector incl. (one Cat6 run = power + uplink) | 1 | ₹5,609 | Moglix |
| Cabling | D-Link outdoor UV-rated Cat6, 50m cut + 25mm HDPE conduit + RJ45s + drip loop | 1 | ~₹3,500–5,500 | local dealer, IndiaMART |
| Server HDDs | 2× Seagate IronWolf Pro 12TB CMR (A: XFS object store; B: nightly rsync mirror) | 2 | ₹61,256 ea | PrimeABGB (3 left at research time) |
| Offsite backup | Seagate Expansion Desktop 16TB USB (rotate offsite weekly; 250GB session mirrors in ~25 min) | 1 | ₹52,499 (₹33k seen on one Amazon listing — verify) | Flipkart, Amazon.in |
| Labels | self-laminating waterproof cable labels + marker | 1 | ₹300–600 | Amazon.in |
| NTP + naming | chrony serving the LAN + static DHCP + mDNS (`lab.local`) | — | ₹0 (config) | — |

- **Budget**: 2× WD Red Plus 8TB @ ₹27k *if they restock* (30–40 day raw retention) + 12TB
  external ≈ ₹1.0L. **Premium**: 2×16TB Pro (₹85k ea) + 20TB external ≈ ₹2.6L.

## 5. Lighting + site power — recommended ₹1.95–2.10L (evening kit) or **₹25–30k daytime-only**

**The honest budget answer is daylight**: 9am–4pm outdoor sessions get 10,000–100,000 lux —
10–100× the ~610–1,225 lux the exposure math needs (E = 250×N²/(t×ISO) at f/2.8, 1/1000s,
ISO 1600–3200) — with zero flicker by physics. Buy lights only when evening training is real.

| Item | Pick | Qty | Price | Where |
|---|---|---|---|---|
| Crease keys | Godox SL150III 160W daylight COB (CRI96; DC-driven constant-current — no 100Hz mains ripple at 240fps; run at 100%) | 4 | ₹25,900–27,900 ea | Flipkart, IndiaMART |
| Corridor fill | Godox SL150III (same SKU = interchangeable spares) | 2 | same | same |
| Stands | 13ft HD air-cushioned stands + 2 sandbags each (strike lights indoors after sessions — not IP-rated) | 6 | ₹2,100–4,000 ea | Digitek, Amazon.in |
| RCD point | Havells 40A 30mA DP RCCB + 2×16A MCB in IP65 box + 2×IP66 16A sockets | 1 | ~₹5,000 parts | Moglix, IndiaMART |
| Extension runs | 2× 16A 30m thermal-cutout reels (split: 3 lights/side vs lights+charging) | 2 | ₹3,000–5,000 ea | Amazon.in, cablereeldrum.com |
| Electrician | dedicated 16A outdoor circuit from the main DB, 2.5mm² FR copper in conduit, earthing verified — **mandatory with lights; pulled into V1 buy as the machine's e-stop point** | 1 | ₹8,000–15,000 all-in | local licensed electrician |

Kit delivers ~1,200–1,500 lux at the creases, ~800–1,000 lux corridor → 1/1000s at
ISO 1600–3200 everywhere including the C7 zone. Budget: skip corridor fill (mid-pitch runs
ISO 6400). Premium: 8 lights, fabricated 4m GI poles on the net frame, or a 2–3kVA
stabilizer for rural grids.

## 6. Calibration + field kit — recommended ₹35–38k

| Item | Pick | Qty | Price | Where |
|---|---|---|---|---|
| ChArUco board | calib.io free generator PDF → A1 matte print on 5mm ACP (₹80–110/sq ft print + mounting). **Discipline: measure the printed square size with the steel tape and feed the true value to `calibrate_cameras.py`** — print scaling is the dominant DIY error | 1 | ₹1,500–2,500 | local print shop (factory calib.io import fails value test: ~₹35–55k landed) |
| Laser measurer | Bosch GLM 40 (40m, ±1.5mm, IP54) | 1 | ₹4,579 | Moglix, Amazon.in |
| Steel tape | Freemans TN30 30m steel (steel ≠ fibreglass: no stretch in survey data) | 1 | ₹949 | Flipkart |
| Marking | 5kg line chalk + 2 rolls white 48mm PVC/cloth tape (taped crosses for surveyed targets) | 1 | ₹300–500 | hardware store |
| Sync flasher | ~1000-lumen rechargeable LED torch, hard clicky switch (sub-ms rise; chained double-flash for C7 per gate #8) | 1 | ₹500–1,100 | Amazon.in |
| Target kit | 2× 12" marker-cone 10-packs (cone-top holds a ball = elevated targets; ≥12 needed for the R3 rig test) | 20 | ₹1,200–1,600 | Amazon.in, Flipkart |
| Tablet | Xiaomi Redmi Pad 2 11" 2.5K (dashboard at the net) — **deferred in the V1 buy; use a laptop first** | 1 | ₹16,999 | Flipkart, mi.com |
| Tablet armor | rugged case + floor tripod stand outside the netting | 1 | ~₹2,500 | Amazon.in |
| Helmet | SG Pro Shield (BSI-style, steel grille) — only if not already owned; machine sessions require it | 1 | ₹1,869 | crowncricketer.com, Amazon.in |
| Rigging | gaffer ×2 + velcro straps + zip ties + silica gel + microfiber | 1 | ~₹2,000–2,700 | Amazon.in |
| Storage tub | Aristo 50L wheeled (silica gel inside; splash-resistant, not IP — store indoors) | 1 | ₹1,200–1,800 | Amazon.in |

Premium: iPad 10th gen ₹27,399 (smoother multi-video review), Shrey Masterclass Air helmet
(₹17k), Arduino-triggered LED flasher (₹500 DIY, microsecond edge).

---

## Grand totals

| Category | Budget | **Recommended** | Premium |
|---|---|---|---|
| Cameras + mounts | ₹2.30–2.50L | **₹3.05L** (+₹25k spare) | ₹3.80–4.20L |
| Bowling machine | ₹1.38L | **₹2.10–2.15L** | ₹3.25–3.60L |
| Server + UPS | ₹0.60–0.80L | **₹1.66–1.76L** | ₹2.70–3.00L |
| Network + storage | ~₹1.00L | **₹1.87L** | ~₹2.60L |
| Lighting + power | ₹0.25–0.30L (daytime) | **₹1.95–2.10L** | ₹2.40L+ |
| Calibration + field | ~₹0.33L | **₹0.35–0.38L** | ₹0.95–1.10L |
| **Total** | **₹5.9–6.4L** | **₹11.2–11.8L** | **₹17–19L** |

**V1 buy-now subset (recommended tier, daytime): ~₹7.6–7.9 lakh** — 4 cameras + kit
(₹1.72–1.77L, gated on the one-body 240fps test), machine bundle (₹2.10L), server
(₹1.66–1.76L, RAM/SSD first), full storage (₹1.87L, do not defer), calibration essentials
minus tablet (~₹15.5k), electrician RCD circuit (₹12–15k).
