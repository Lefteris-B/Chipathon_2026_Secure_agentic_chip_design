# Export Control Form for GLOBALFOUNDRIES — Rev 8.2
## Answer sheet for `Export_Control_GF_Rev8.2.docx`

Design of record: **SPI-slave register file** (`D10_D`), gf180mcuD, IEEE SSCS PICO
Open-Source Chipathon 2026, Track D. Facts drawn from `info.yaml`, `Readme.md`,
`specs/spi_slave_regs.md`.

> `«TODO»` marks a field only you can supply.

---

## Header

| Field | Value |
|---|---|
| Customer Name | Eleftherios Batzolis (individual contributor) |
| Device Name | `D10_D` — SPI-Slave Register File |
| Project Name | SPI-Slave Register File — SSCS PICO Chipathon 2026, Track D (team SystemsGenesys) |
| Phone | «TODO» |
| Contact Person | Eleftherios Batzolis |
| Email ID | th3magist3r@gmail.com |

---

## 1. Product description and end use

**1 a)** Digital CMOS ASIC: an SPI-slave peripheral (Mode 0, CPOL=0/CPHA=0) exposing an
8-bit-addressed register file of seven registers (ID, VERSION, CTRL, four scratch bytes).
249 standard-cell instances on gf180mcuD, 550 × 550 µm die in a fixed multi-project
padframe slot, 9 user I/O. **Final end use: education and research only** — a
non-commercial university-shuttle demonstration vehicle for an open-source agentic
RTL-to-GDSII design flow. Returned die are exercised on a lab bench with a USB-SPI dongle
(read register `0x00`, expect `0x5A`; write and read back a scratch byte). The part is not
sold, not incorporated into any product, and has no application function beyond
demonstrating that the design flow produced working silicon.

**1 b)**
- (i) Peak saturated power output: **N/A** — digital logic only; no RF, no amplifier, no power output stage.
- (ii) Operating frequency: **≤ 76.9 MHz** (13 ns clock period, the closed timing target; core spec targets 10 ns / 100 MHz).
- (iii) Fractional bandwidth: **N/A** — baseband digital device, not a bandwidth-defined RF part.
- (iv) Planar side length: **0.55 mm** (550 µm × 550 µm die, fixed by the padframe slot).

**1 c)** mmWave product? **No.** Sub-items (i)–(iv) are therefore N/A:
- (i) No graph — no RF power output or S21 to plot.
- (ii) Integrated phase shifter? **No.**
- (iii) "Specially designed" for telecom / radar / automotive? **No.**
- (iv) Controlled under USML XI(c)(4), XV(e)(14), ECCN 3A001.b.2, 3A001.b.12, or 5E001.d? **No.**

---

## 2–9. Parties, destinations, end use

| Q | Answer | Basis |
|---|---|---|
| **2.** Restricted-party involvement | **No** | Parties are the individual submitter (Greece), the IEEE SSCS PICO programme, and GlobalFoundries. None is listed, 50%-owned by, or acting for a listed party. |
| **3.** Party in a comprehensively sanctioned country/region | **No** | Submitter is resident in Greece (EU / EAR Country Group A:1). |
| **4.** More than one chip design in this tapeout | **No** — *see note* | This submission is a single design occupying one padframe slot (`D10_D`). The shuttle reticle as a whole carries other teams' independent designs; that is the organisers' aggregation, not part of this submission. Flag to the organisers if GF expects the shuttle-level answer here. |
| **5.** Subject to non-U.S. export controls | **No** | The submitter is EU-based, so Reg. (EU) 2021/821 applies as a regime, but this item is not listed in its Annex I. It is an unlisted, publicly-documented digital logic device. |
| **6.** Variants not changing end use / export outcome | **No** | Single configuration; no variants. |
| **7.** ASIC or unalterably programmed for a specific function | **Yes** | It is an ASIC with no programmable fabric and no stored-program processor. Specific function: **an SPI (Mode 0) slave interface to a small register file** — it accepts a 16-bit frame (command byte `{RW, addr[6:0]}` then data byte) and reads or writes one of seven 8-bit registers. Equipment: none — it is a stand-alone bench demonstrator, not designed into any other equipment. |
| **8.** WMD / nuclear / missile / UAV end use | **No** | |
| **9.** Entity List (Footnote 1/3/4/5) supply-chain knowledge | **No** | The submitter has no knowledge of any such export, re-export, or in-country transfer. |

---

## 10. Military or intelligence application

**No.** The product, its design data (GDSII/RTL), and associated software are not specially
designed, modified, or adapted for military or intelligence application, and are not
intended for incorporation or end use in any ITAR, EAR 500-/600-series, or other Munitions
article. Sub-items (I), (i)–(v): **N/A** — no CJ determination, CCATS, advisory opinion, or
classification memo exists or is required.

---

## 11. Encryption

**No.** The product, its design data, and its associated software are **not** specially
designed or modified to use encryption. The device contains no cipher, no hash, no key
storage, no random-number generator, and no authentication logic — it is a 16-bit SPI shift
register, a frame FSM, a 7-entry register file, and a read multiplexer, in 249 standard
cells. The SPI bus carries plaintext register reads and writes.

- (i)–(viii), (xi): **N/A** — no encryption functionality to describe, classify, or exclude.
- (ii) Reviewed under a CCATS? **No** → (iii) N/A, (v)/(vi) N/A.
- (iii) Self-classified? **No** (not an encryption item; see Q12 for the EAR99 self-classification of the item itself).
- (iv) CCATS number: **N/A**
- (vii) ENC § 740.17 sub-paragraph: **N/A — Not applicable**
- (viii) Deemed-export technology ECCN setting aside encryption: **EAR99**
- (ix) Cryptanalytic / network-penetration / vulnerability-analysis item? **1. None**
- (x) Will it affect confidentiality, integrity, or availability of information systems without the owner's authorization? **No**
- (xii) U.S. ECCN at sub-paragraph level — Product: **EAR99**; Design File/Technology: **EAR99**; Software: **N/A** (no software is delivered with the product)

> **Note on the repository.** Earlier development in this repo used PRESENT-80, a lightweight
> block cipher, as an internal benchmark design. **It is not part of this submission and is
> not being manufactured.** The design of record taped out in slot `D10_D` is the SPI-slave
> register file described above. If GF asks about cipher material visible in the public
> repository, that is the correct and complete answer.

---

## 12. Subject to the EAR

**Yes.** Per the Foreign Direct Product Rule statement in Q16 of this form, any item GF
produces must be assumed subject to the EAR under 15 C.F.R. § 734.9. The item is classified
**EAR99** — it meets no technical parameter of any ECCN on the CCL.

- (i) Supporting documents: **Self-classification by the submitter.** No CJ determination,
  CCATS, or outside-counsel memo exists. Basis: a 249-cell digital SPI-slave register file
  with no encryption, no RF, no processing performance, and no listed function falls outside
  every Category 3 and Category 5 entry, and is therefore EAR99.
- (ii) U.S. ECCN at sub-paragraph level — Product (wafer/die/module): **EAR99**;
  Design File/Technology (GDSII, RTL): **EAR99**; Software: **N/A**

---

## 13. ECCN 3A090

**No** — the item does not meet or exceed 3A090 parameters, by a very wide margin.
It contains no digital processing unit: total processing performance is effectively **0**
(no MAC units, no arithmetic datapath wider than an 8-bit register file, no parallelism),
against the 1600 threshold in 3A090.a.2. It is not high-bandwidth memory. Sub-item (i)
and the Authorized-IC-Designer follow-up are therefore **N/A**.

---

## 14–16

| Q | Answer | Basis |
|---|---|---|
| **14.** Meets technical criteria of any other ECCN on the CCL | **No** — classification is **EAR99** | No encryption (Cat. 5 Part 2), no RF or mmWave (3A001.b), no processing performance (3A090), no listed logic function. |
| **15.** Qubit device or circuit | **No.** Sub-items (i)/(ii): N/A | Conventional CMOS digital logic on gf180mcuD. |
| **16.** Non-U.S. jurisdiction classification | **No** | Q12 is answered **Yes** (EAR99 under the FDPR), so the Q16 condition — "no" to 10, 11 *and* 12 — is not met. The FDPR note is acknowledged and is precisely why Q12 is Yes. |

---

## 17. Harmonized System classification

Select **☐ 8542.31.0050 — Other**.

Heading 8542.31 covers processors and controllers, optionally combined with memory,
converters, logic circuits, amplifiers, or clock and timing circuits. This part is an
interface controller with an on-chip register file, but it is **not** a microprocessor,
microcontroller, DSP, GPU, CPU, CPLD, FPGA, or SoC, so none of the named breakouts fits and
the residual "Other" applies.

> **Alternative worth a moment's thought:** `8542.31.0015` (8-bit) if GF's broker reads the
> bit-width breakouts as covering any 8-bit controller rather than only stored-program
> microcontrollers. `.0050` is the better-supported reading — the part executes no
> instructions. Free-text box: *"8-bit SPI-slave interface controller with integrated
> register file; not a stored-program processor or microcontroller."*

---

## 18–20

| Q | Answer | Basis |
|---|---|---|
| **18a.** Military / military-intelligence end use connected with Belarus, Burma, Cambodia, China, Iraq, Nicaragua, Russia, Venezuela | **No.** Sub-item (i): **No** | |
| **18b.** Microprocessor (≥5 GFLOPS, ≥32-bit ALU) for military end use in Country Group D:1 | **No** | Not a microprocessor: no FLOPS, 8-bit datapath. Condition does not apply. |
| **19.** Advanced chip and computing end-use restrictions (i)–(iv) | **No** | No knowledge of any export, re-export, or in-country transfer for advanced-node IC production, 3A090/4A090/.z items, or supercomputer use. The part is EAR99 and returns to a lab bench in Greece. |

**20. End user of the manufactured IC:**

```
Eleftherios Batzolis
«TODO: full postal address — street, city, postal code, Greece»
```

Sole end user. Die are received for laboratory bring-up, functional verification, and
demonstration of the design flow. No onward transfer, sale, or incorporation into other
equipment.

---

## Certification block

| Field | Value |
|---|---|
| Organization (both occurrences, and the "duly authorized to bind" line) | «TODO» — if filing as an individual, enter your own name, or the exact legal name of the entity if a university or employer is the signatory |
| I, _______, certify | Eleftherios Batzolis |
| Name | Eleftherios Batzolis |
| Title | «TODO» — e.g. *Project Lead, team SystemsGenesys* |
| Signature | Electronic signature in the `.docx` signature field |

---

## Before you sign

1. **Fill every `«TODO»`** — phone, postal address, organization name, title.
2. **Confirm Q4 with the organisers.** If GF expects the *shuttle-level* answer (many teams'
   designs on one reticle), Q4 becomes **Yes** and the organisers, not you, should own that
   answer. Your own submission is unambiguously one design.
3. **Confirm the Q17 HS code** with whoever handles GF's customs paperwork; `.0050` vs
   `.0015` is a defensible judgment call, not a settled fact.
4. **Read Q7 and Q11 once more yourself.** You are certifying these personally. The PRESENT-80
   note under Q11 exists because your public repository contains cipher code that is not in
   the tapeout — be ready to say so plainly if asked.
