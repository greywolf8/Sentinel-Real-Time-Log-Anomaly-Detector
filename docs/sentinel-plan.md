# Sentinel: Real-Time Log Anomaly Detector

Plan for the Acentra Health hackathon problem statement "Real-Time Log Anomaly Detector with Alert Feed" (Python).

Status: final plan, v1. All service names, component names, error codes and IDs below are synthetic and only inspired by Acentra's public descriptions. No real PHI or Acentra internals are used.

---

## 1. One-paragraph pitch

Medicaid platforms cannot afford silent failures. Sentinel tails a growing, unified log from a simulated Medicaid claims platform (five services, twenty components), learns what normal looks like for every component, detects error-rate deviations within seconds, explains each alert, groups related alerts into incidents, scrubs PHI, and pushes alerts to a live dashboard and to AWS SNS and CloudWatch. A separate Fault Console injects failures so the detector can be measured against ground truth.

## 2. Research summary (why this design)

Sources are listed in section 17. Findings that shaped the plan:

| Finding | Design consequence |
|---|---|
| Acentra was formed by the merger of CNSI and Kepro. It serves state and federal partners in all 50 states, and has engineering teams in Chennai. | Frame the demo as a Medicaid claims platform. Chennai teams are a likely audience for the hackathon. |
| Its core platform (evoBrix X) does high-volume claims and encounter processing, with real-time adjudication and 800+ configurable edits. Marketing stresses that payment cycles must not be missed. | Severity is tied to payment-cycle risk. Track the mix of adjudication edit/denial codes, not just error counts. |
| Acentra was the first AWS partner to move a core MMIS to AWS. | SNS and CloudWatch delivery is on-brand. Show real AWS depth (filter policies, EMF metrics, IaC). |
| Its cloud systems are HITRUST certified and marketed on secure architecture and continuous monitoring. | PHI redaction with a canary test, audit trail, least-privilege IAM. Never claim compliance, say "designed with these controls in mind". |
| Acentra runs a Safe AI in Medicaid Alliance and stresses human-in-the-lead AI. | Detection is explainable and statistical first. Every alert carries a reason. Optional LLM summaries are grounded, redacted and human-approved. |
| CloudWatch already offers log anomaly detection (pattern-based, evaluated on a schedule, works poorly on audit/access logs) and metric anomaly detection (trains on up to two weeks, cannot model one-time events). | Position Sentinel as an edge detector that feeds CloudWatch: seconds-level latency, PHI scrubbed before shipping, domain-aware severity. |
| Deep log models (DeepLog, LogBERT) can trail supervised baselines by 20+ F1 points in unified comparisons and are sensitive to concept drift. | Lead with lightweight, explainable statistics. Deep models are optional stretch only. |

Caveat: I found no public page for this specific hackathon. Confirm event duration, judging criteria, whether AWS credentials are provided, and any hiring angle with the organizers.

## 3. System overview

Six services, two dashboards, one shared log file.

```
  +------------------- demo platform (scripts) --------------------+
  |  PAY    CLM     ELG     PRV     ADM      5 services x 4 comps  |
  |   \      |       |       |      /                              |
  |    +-----+-------+-------+-----+                               |
  |          single writer appends to  platform.log                |
  +-------------^------------------------------|-------------------+
                | control API (rates, faults)  |
        +-------+-------+                      v
        | Fault Console |      +--------------------------------------+
        | (dashboard 2) |      | 6th service: Sentinel                |
        +---------------+      |  tailer -> batch parser -> rings     |
                               |  -> detectors -> incidents -> outbox |
   ground_truth.jsonl <--------|  FastAPI WebSocket + REST            |
   (never read by detector)    +---------+---------------+------------+
                                         |               |
                               Detector dashboard     AWS SNS + CloudWatch
                               (dashboard 1)
```

Key rules:

- The detector reads only `platform.log`. It never imports simulator code and never reads `ground_truth.jsonl`.
- The Fault Console talks only to the generator's control API.
- The evaluation script joins `platform.log` alerts with `ground_truth.jsonl` after the fact.

## 4. Demo platform: services and components

Service codes are 3 characters. Component codes are 3 characters. "rps" is requests per second in normal mode. "base err" is the fraction of lines at level E or F. Denials (level W) are business outcomes and are not counted as errors.

### 4.1 payment-remittance (PAY)

| Code | Component | rps | base err | Depends on | Success line |
|---|---|---|---|---|---|
| RMT | remit-generator | 60 | 0.006 | CLM.STR | `remit 835 built remit=RMT-{n}` |
| EXP | payment-file-export | 8 | 0.003 | PAY.RMT | `payment file written file=pay_{date}_{n}.ach` |
| LDG | ledger-writer | 120 | 0.002 | PAY.RMT | `ledger txn committed txn=LDG-{n}` |
| BNK | bank-gateway | 4 | 0.010 | PAY.EXP | `bank sftp upload ok host=bank-gw-{n}` |

| Comp | Code | Lvl | Message template |
|---|---|---|---|
| RMT | E5101 | E | `REMIT_835_BUILD_FAILED remit=RMT-{n} reason=segment_count_mismatch` |
| RMT | E5102 | E | `PAYEE_ADDRESS_MISSING provider=PRV-{n}` |
| RMT | W5103 | W | `REMIT_BATCH_SLOW batch=B-{n} ms={ms}` |
| EXP | E5201 | E | `ACH_FILE_WRITE_FAILED file=pay_{date}_{n}.ach errno=ENOSPC` |
| EXP | E5202 | E | `FILE_CHECKSUM_MISMATCH file=pay_{date}_{n}.ach` |
| LDG | E5301 | E | `LEDGER_TXN_ROLLBACK txn=LDG-{n} reason=deadlock` |
| LDG | E5302 | E | `FUND_ACCOUNT_LOOKUP_TIMEOUT fund=F-{n} after_ms={ms}` |
| BNK | E5401 | E | `BANK_SFTP_TIMEOUT host=bank-gw-{n} after_ms={ms}` |
| BNK | E5402 | E | `BANK_ACK_REJECTED ack=R{nn} file=pay_{date}_{n}.ach` |

### 4.2 claims-adjudication (CLM)

| Code | Component | rps | base err | Depends on | Success line |
|---|---|---|---|---|---|
| EDT | edit-engine | 250 | 0.008 (+ denials 0.06) | ELG.MBR, PRV.PST | `edits applied claim=CLM-{n} edits={k} outcome=paid` |
| PRC | pricing-engine | 220 | 0.003 | CLM.EDT | `priced claim=CLM-{n} schedule=FS-{yy}` |
| DUP | duplicate-check | 250 | 0.002 | CLM.STR | `dup check clear claim=CLM-{n}` |
| STR | claim-store | 300 | 0.002 | none (leaf DB) | `claim persisted claim=CLM-{n}` |

| Comp | Code | Lvl | Message template |
|---|---|---|---|
| EDT | E4110 | E | `EDIT_ENGINE_RULESET_LOAD_FAILED ruleset=v{n}` |
| EDT | E4111 | E | `EDIT_EVAL_TIMEOUT claim=CLM-{n} edit=EDIT_{nnnn} after_ms={ms}` |
| EDT | W4101 | W | `EDIT_0101 member_not_eligible claim=CLM-{n}` (25% of denials) |
| EDT | W4102 | W | `EDIT_0102 provider_not_enrolled claim=CLM-{n}` (15%) |
| EDT | W4103 | W | `EDIT_0103 procedure_not_covered claim=CLM-{n} proc={cpt}` (30%) |
| EDT | W4104 | W | `EDIT_0104 prior_auth_missing claim=CLM-{n}` (20%) |
| EDT | W4105 | W | `EDIT_0105 timely_filing_exceeded claim=CLM-{n}` (10%) |
| PRC | E4210 | E | `FEE_SCHEDULE_NOT_FOUND proc={cpt} dos={date}` |
| PRC | E4211 | E | `PRICING_CALC_OVERFLOW claim=CLM-{n}` |
| DUP | E4310 | E | `DUP_INDEX_UNAVAILABLE shard={n}` |
| DUP | W4301 | W | `DUPLICATE_CLAIM_SUSPECTED claim=CLM-{n} orig=CLM-{m}` |
| STR | E4410 | E | `CLAIM_DB_TIMEOUT table=claims after_ms={ms}` |
| STR | E4411 | E | `CLAIM_DB_CONN_POOL_EXHAUSTED pool=adj-rw` |

The W4101 to W4105 shares are the baseline denial-code mix that the drift detector watches.

### 4.3 member-eligibility (ELG)

| Code | Component | rps | base err | Depends on | Success line |
|---|---|---|---|---|---|
| X12 | x12-270-271-gateway | 180 | 0.004 | ELG.MBR | `271 response sent isa=ISA-{n}` |
| MBR | member-lookup | 320 | 0.002 | ELG.CCH | `member lookup ok member=MBR-{n}` |
| CCH | eligibility-cache | 400 | 0.001 | none | `cache hit key=K-{n}` |
| CVR | coverage-rules | 150 | 0.003 | ELG.CCH | `coverage resolved member=MBR-{n}` |

| Comp | Code | Lvl | Message template |
|---|---|---|---|
| X12 | E2101 | E | `X12_270_PARSE_ERROR isa=ISA-{n} segment=NM1` |
| X12 | E2102 | E | `X12_271_RESPONSE_TIMEOUT payer=ST{nn} after_ms={ms}` |
| MBR | E2201 | E | `MEMBER_INDEX_TIMEOUT after_ms={ms}` |
| MBR | W2202 | W | `MEMBER_NOT_FOUND member=MBR-{n}` |
| CCH | E2301 | E | `CACHE_NODE_UNREACHABLE node=cache-{n}` |
| CCH | W2302 | W | `CACHE_MISS_RATE_HIGH pct={n}` |
| CVR | E2401 | E | `COVERAGE_RULES_STALE version=v{n}` |
| CVR | E2402 | E | `SPAN_OVERLAP_UNRESOLVED member=MBR-{n}` |

### 4.4 provider-enrollment (PRV)

| Code | Component | rps | base err | Depends on | Success line |
|---|---|---|---|---|---|
| INT | enrollment-intake | 25 | 0.012 | PRV.CRD | `application accepted app=APP-{n}` |
| CRD | credential-verify | 30 | 0.010 | PRV.NPI | `credentials verified npi={npi}` |
| NPI | npi-registry-client | 40 | 0.008 | none (external) | `npi lookup ok npi={npi}` |
| PST | provider-store | 60 | 0.002 | none (leaf DB) | `provider record saved prov=PRV-{n}` |

| Comp | Code | Lvl | Message template |
|---|---|---|---|
| INT | E3101 | E | `ENROLL_FORM_VALIDATION_FAILED app=APP-{n} field=taxonomy` |
| INT | W3102 | W | `ENROLL_DOC_MISSING app=APP-{n} doc=W9` |
| CRD | E3201 | E | `LICENSE_BOARD_API_TIMEOUT state={XX} after_ms={ms}` |
| CRD | E3202 | E | `EXCLUSION_CHECK_FAILED npi={npi}` |
| NPI | E3301 | E | `NPI_LOOKUP_5XX status=503 endpoint=npi-registry` |
| NPI | E3302 | E | `NPI_RATE_LIMITED retry_after_s={n}` |
| PST | E3401 | E | `PROVIDER_DB_DEADLOCK txn=PTX-{n}` |
| PST | E3402 | E | `PROVIDER_INDEX_REBUILD_FAILED index=idx_npi` |

### 4.5 admin-integrity (ADM)

| Code | Component | rps | base err | Depends on | Success line |
|---|---|---|---|---|---|
| AUT | auth-service | 90 | 0.003 (+ bad-password 0.02) | none | `token validated user=U-{n}` |
| AUD | audit-trail | 200 | 0.0005 | ADM.AUT | `audit event stored event=EVT-{n}` |
| RPT | report-export | 5 | 0.015 | CLM.STR | `report exported report=R-{n}` |
| FWA | fwa-scorer | 100 | 0.004 | CLM.STR | `fwa scored claim=CLM-{n} score=0.{nn}` |

| Comp | Code | Lvl | Message template |
|---|---|---|---|
| AUT | E1101 | E | `AUTH_TOKEN_VALIDATE_FAILED user=U-{n} reason=expired` |
| AUT | W1102 | W | `AUTH_BAD_PASSWORD user=U-{n} ip=10.20.{a}.{b}` |
| AUT | E1103 | E | `IDP_UNREACHABLE idp=idp-{n} after_ms={ms}` |
| AUD | E1201 | E | `AUDIT_WRITE_FAILED event=EVT-{n}` |
| RPT | E1301 | E | `REPORT_TIMEOUT report=R-{n} after_ms={ms}` |
| RPT | E1302 | E | `EXPORT_OBJECT_PUT_FAILED bucket=rpt-exports` |
| FWA | E1401 | E | `FWA_MODEL_LOAD_FAILED model=v{n}` |
| FWA | W1402 | W | `FWA_SCORE_HIGH claim=CLM-{n} score=0.{nn}` |

Total: about 48 error and warning codes plus the success code `00000`. Normal-mode volume is roughly 2,800 lines per second across the platform.

### 4.6 Dependencies and fault propagation

When an upstream component's error rate rises above its base, downstream components rise after a lag:

```
downstream_err = base + coupling * max(0, upstream_err - upstream_base)   (after lag_s)
```

```yaml
propagation:
  - {from: ELG.CCH, to: ELG.MBR, coupling: 0.5, lag_s: 2}
  - {from: ELG.MBR, to: ELG.X12, coupling: 0.6, lag_s: 2}
  - {from: ELG.MBR, to: CLM.EDT, coupling: 0.6, lag_s: 3}
  - {from: PRV.PST, to: CLM.EDT, coupling: 0.4, lag_s: 3}
  - {from: CLM.EDT, to: CLM.PRC, coupling: 0.5, lag_s: 2}
  - {from: CLM.STR, to: CLM.DUP, coupling: 0.6, lag_s: 2}
  - {from: CLM.STR, to: PAY.RMT, coupling: 0.5, lag_s: 4}
  - {from: PAY.RMT, to: PAY.EXP, coupling: 0.5, lag_s: 3}
  - {from: PAY.EXP, to: PAY.BNK, coupling: 0.5, lag_s: 3}
  - {from: PRV.NPI, to: PRV.CRD, coupling: 0.6, lag_s: 2}
  - {from: PRV.CRD, to: PRV.INT, coupling: 0.5, lag_s: 2}
  - {from: CLM.STR, to: ADM.RPT, coupling: 0.5, lag_s: 3}
  - {from: CLM.STR, to: ADM.FWA, coupling: 0.4, lag_s: 3}
```

## 5. Log format (designed for speed)

One line per event, ASCII, newline-terminated, with a fixed-width 46-byte header followed by a free-text message. Fixed positions mean the detector can slice fields without splitting or regex, and can parse thousands of lines at once with numpy.

```
0         1         2         3         4
0123456789012345678901234567890123456789012345
1790603412345|E|CLM|STR|E4410|003002|b2c7e0d9|CLAIM_DB_TIMEOUT table=claims after_ms=3000
```

| Bytes | Field | Notes |
|---|---|---|
| 0-12 | timestamp | 13-digit epoch milliseconds (event time) |
| 14 | level | `I` info, `W` warn or business denial, `E` error, `F` fatal |
| 16-18 | service | `PAY CLM ELG PRV ADM` |
| 20-22 | component | e.g. `EDT`, `STR`, `MBR` |
| 24-28 | code | `00000` for success, else letter + 4 digits from the code catalog |
| 30-35 | latency ms | zero-padded to 6 digits |
| 37-44 | trace id | 8 hex characters, shared across a request chain |
| 46 to end | message | free text, never parsed on the hot path |
| 13, 15, 19, 23, 29, 36, 45 | separators | always the pipe character |

Design choices and why:

- Bytes 16-22 (`CLM|STR`) are directly usable as the component key. No split, one dictionary lookup.
- Success lines start their code with `0`, so the hot path can skip code handling for them.
- Event-time timestamps in epoch ms avoid date parsing and let the demo run in accelerated or replayed time.
- Message text is only decoded when an alert needs evidence.
- Only one writer appends to the file, using whole-line writes, so lines never interleave.
- A code catalog file (`catalog.yaml`) maps `service+component+code` to level, weight and message template, and is loaded by both generator and detector.
- PHI rule: messages contain synthetic IDs only. A canary scenario deliberately injects fake PHI to test redaction (section 12).

### 5.1 Sample log excerpt

Normal traffic, then a claim-store failure with cascade, then examples of unidentifiable lines:

```
1790603412345|I|ELG|MBR|00000|000014|a91f03c2|member lookup ok member=MBR-104233
1790603412351|I|CLM|EDT|00000|000041|a91f03c2|edits applied claim=CLM-88213 edits=12 outcome=paid
1790603412358|W|CLM|EDT|W4103|000038|b2c7e0d9|EDIT_0103 procedure_not_covered claim=CLM-88214 proc=97110
1790603412370|I|PAY|LDG|00000|000009|c4d81f77|ledger txn committed txn=LDG-55120
1790603412377|E|PRV|NPI|E3301|000210|d5e2a6b1|NPI_LOOKUP_5XX status=503 endpoint=npi-registry
1790603412384|W|ADM|AUT|W1102|000006|e6f3b7c2|AUTH_BAD_PASSWORD user=U-2041 ip=10.20.4.17
1790603430120|I|CLM|EDT|00000|000044|f0a1b2c3|DEPLOY ruleset=v42 applied
1790603432004|E|CLM|STR|E4410|003002|0d1e2f30|CLAIM_DB_TIMEOUT table=claims after_ms=3000
1790603432011|E|CLM|STR|E4411|000005|1a2b3c4d|CLAIM_DB_CONN_POOL_EXHAUSTED pool=adj-rw
1790603433390|E|CLM|DUP|E4310|001200|2b3c4d5e|DUP_INDEX_UNAVAILABLE shard=3
1790603436210|E|PAY|RMT|E5101|000850|3c4d5e6f|REMIT_835_BUILD_FAILED remit=RMT-7712 reason=segment_count_mismatch
1790603440005|E|ADM|RPT|E1301|005000|4d5e6f70|REPORT_TIMEOUT report=R-311 after_ms=5000
```

Unidentifiable examples (all handled gracefully, see 8.8):

```
1790603412399|I|ZZZ|QQQ|00000|000012|a1b2c3d4|hello from a new service
1790603412402|E|CLM|EDT|E9999|000020|a1b2c3d4|SOMETHING_NEW unmapped
1790603412405|X|CLM|PRC|00000|000018|a1b2c3d4|invalid level character
garbage line without a header
```

## 6. Generator and Fault Console

### 6.1 Generator

- One Python process, one asyncio task per component, one writer task appending to `platform.log`.
- Config in `platform.yaml`: per component `rps`, `err`, `p50_ms`, code weights, `deps`.
- Error draw: binomial from the current rate, with small jitter and a daily traffic multiplier. Errors use the component's code shares.
- Fault layer: overrides rate, code weights, silence, or volume for a component, with optional ramp and hold time.
- Virtual clock: `--speed N` runs faster than real time by scaling timestamps. The detector uses event time only.
- Load mode: `--rps-scale K` multiplies volume for speed benchmarks. `--dump N` pre-generates N lines to a file for offline replay.
- Real deploy markers (like the `DEPLOY` line above) are allowed in the log because a real monitor would see them. Rate changes are never written to the log.

### 6.2 Control API (localhost only, token if deployed)

```
GET  /rates
PUT  /rates/{svc}/{cmp}        {rate, ramp_s?, hold_s?}
PUT  /mix/{svc}/{cmp}          {code_weights, hold_s?}
POST /silence/{svc}/{cmp}      {hold_s}
POST /scenarios/{name}/run
POST /reset
GET  /truth                    ground-truth events
```

Every control action is appended to `ground_truth.jsonl` with timestamp, target and new setting. The detector never reads it.

### 6.3 Scenarios (also the evaluation runner)

| Scenario | Action | What it tests |
|---|---|---|
| db_timeout_spike | CLM.STR err 0.2% to 25% for 90s | Detection delay, cascade to PAY.RMT, ADM.RPT, ADM.FWA |
| slow_degradation | ELG.MBR err 0.2% to 8% over 10 min | Slow-window and CUSUM detection |
| bad_rule_deploy | Deploy marker, then EDT denial mix shifts W4103 from 30% to 55%+, error rate flat | Code-mix drift detection |
| new_error_after_deploy | PRV.CRD starts emitting unseen code E3299 | New-code detector |
| upstream_cascade | ELG.MBR fails, downstream follows | Incident grouping and origin hint |
| service_silent | PAY.BNK stops logging | Silence detection |
| nightly_batch_surge | PAY.EXP volume x20 for 60s, normal error rate | False-positive control |
| brute_force_login | ADM.AUT W1102 burst | Warning-level rate detection |
| log_format_drift | A component emits malformed and unknown-code lines | Unidentified-log handling |
| phi_leak_canary | ELG.MBR occasionally logs a fake SSN string | Redaction and no leakage to UI or AWS |

### 6.4 Fault Console (dashboard 2)

- Grid of 20 components, each with configured rate, recently observed rate, and a slider.
- Preset row for each scenario, plus reset.
- Dependency view showing downstream services of the one being broken.
- Simple, built after the control API and CLI work.

## 7. Detector: pipeline overview

```
tailer -> chunk -> batch parser -> ring update -> per-second detectors
      -> severity -> incident engine -> bounded queue -> WebSocket / outbox (SNS, CloudWatch)
```

Everything runs on event time. A watermark advances as newer timestamps arrive, and a second is evaluated once the watermark is 2 seconds past it (reorder slack).

## 8. Detector design (speed-focused)

### 8.1 Data structures

Preallocate for capacity so new components can be added without reallocation: `NCAP = 64` components, `NCODE = 256` codes, `R = 900` one-second slots (15 minutes), `NB = 20` latency bins, `NS = 8` services.

| Structure | Type and shape | Purpose |
|---|---|---|
| `KNOWN_KEYS` | sorted `uint64[NCAP]` | 7-byte `SVC` + `CMP` key packed into 8 bytes; searchsorted maps a key to a component index |
| `KEY_TO_COMP` | `int16[NCAP]` | index lookup aligned to `KNOWN_KEYS` |
| `KNOWN_CODES`, `CODE_TO_IDX` | sorted `uint64`, `int16` | same trick for the 5-byte code |
| `LVL_LUT` | `uint8[256]` | level byte to class (0 info, 1 warn, 2 error, 3 fatal, 255 invalid) |
| `ring_tot`, `ring_err`, `ring_warn` | `uint32[NCAP, R]` | per component per second counts |
| `ring_code` | `uint32[NCODE, R]` | per code per second counts (drives mix drift) |
| `ring_lat` | `uint16[NCAP, R, NB]` | log2-binned latency histogram per second |
| `sum10`, `sum60`, `sum300` | `int64[NCAP]` x (tot, err, warn) | rolling window sums, updated incrementally: add the new second, subtract the one leaving the window |
| `p0`, `var0` | `float64[NCAP]` | EWMA baseline error rate and variance |
| `season` | `float32[NCAP, 168]` | per hour-of-week baseline (median and MAD), optional |
| `state`, `consec_bad`, `consec_ok`, `open_since` | `int8`, `int16`, `int16`, `int64` per component | hysteresis state machine |
| `SVC_MAT` | `uint8[NS, NCAP]` | 0/1 matrix, so service roll-ups are one matrix multiply |
| `unk` | small dict of counters plus deque(200) | unidentified-log accounting (8.8) |

Memory is a few megabytes in total.

### 8.2 Hot path and cold path

- Hot path (per line or per batch): parse header, map to component and code index, increment counts. No detection logic, no allocation per line.
- Cold path (once per completed second): vectorized numpy work over all components at once: update window sums, evaluate detectors, update hysteresis.

### 8.3 Reader

- Open the file in binary mode and keep the offset. Read large chunks (for example 1 to 4 MB) with `os.read`.
- Keep a carry buffer for a partial last line. Only process bytes up to the final newline.
- Detect rotation or truncation by checking the inode and comparing size to the offset. Reopen from the start when needed.
- When caught up, sleep briefly (5 to 20 ms adaptive) or use inotify. Never busy-spin.
- Start with a backlog read, then follow.

### 8.4 Vectorized batch parser (default path)

Because the header is fixed-width, a whole chunk can be parsed with numpy. This is the main speed lever.

```python
HDR = 46
OFFS = np.arange(HDR)
SEPS = [13, 15, 19, 23, 29, 36, 45]

def parse_chunk(data: bytes):
    a = np.frombuffer(data, dtype=np.uint8)
    nl = np.flatnonzero(a == 10)
    starts = np.concatenate(([0], nl[:-1] + 1))
    lens = nl - starts
    ok = lens >= HDR                         # short lines go to the unidentified path
    st = starts[ok]
    H = a[st[:, None] + OFFS]                # (n, 46) header matrix, one gather
    sep_ok = (H[:, SEPS] == 124).all(axis=1)
    d = H[:, :13].astype(np.int64) - 48
    dig_ok = ((d >= 0) & (d <= 9)).all(axis=1)
    ts = d @ POW10_13                        # 13-digit epoch ms
    lvl = LVL_LUT[H[:, 14]]
    K = np.zeros((len(H), 8), np.uint8); K[:, :7] = H[:, 16:23]
    key = K.view('<u8').ravel()
    pos = np.minimum(np.searchsorted(KNOWN_KEYS, key), NKEYS - 1)
    known = KNOWN_KEYS[pos] == key
    comp = np.where(known, KEY_TO_COMP[pos], -1)
    good = sep_ok & dig_ok & (lvl != 255) & known
    # bad = ~good rows (plus short lines) are sliced out of `data` and passed
    # to unidentified.record(); they never touch the rings
    sec = ts[good] // 1000
    rel = sec - sec.min()                    # guard: huge span means clock skew, route to unidentified
    flat = rel * NCAP + comp[good]
    tot = np.bincount(flat, minlength=(rel.max() + 1) * NCAP)
    err = np.bincount(flat[lvl[good] >= 2], minlength=len(tot))
    # ...same for warn, code counts (skip rows whose code starts with '0'),
    # and latency bins (np.frexp exponent of latency gives the log2 bin)
    return sec.min(), tot, err, ...          # merged into the rings by second
```

Notes:

- `np.bincount` is used instead of `np.add.at`, which is much slower.
- A slower per-line reference parser (plain Python slicing) exists for correctness tests. Property test: both parsers must produce identical counts on generated logs, including malformed lines.
- Do not quote speed numbers until measured. Report both parsers in the benchmark (section 8.10).

### 8.5 Windows and baseline

- Windows: 10 s (fast), 60 s (medium), 300 s (slow). Sums are maintained incrementally, so each second costs a few vector operations regardless of window size.
- Baseline error rate per component: EWMA over the 300 s window, updated only while the component is in the OK state (frozen during an incident so anomalies do not become normal). A floor prevents zero-baseline blowups.
- Optional seasonal layer: per hour-of-week median and MAD, used when enough history exists. Warm-up period (for example 2 minutes in demo, longer in production) suppresses alerts while learning.

### 8.6 Detectors (all vectorized over components)

Error-rate deviation and threshold (the core requirement, tracked per component, rolled up per service and for the whole system):

```python
p    = err10 / np.maximum(tot10, 1)
z    = (err10 - tot10 * p0) / np.sqrt(np.maximum(tot10 * p0 * (1 - p0), 1e-9))
fast = (tot10 >= MIN_N) & (z > Z_FAST) & (p > p0 + MIN_DELTA)     # high threshold, short window
slow = (tot60 >= MIN_N) & (z60 > Z_SLOW) & (p60 > p0 + MIN_DELTA) # lower threshold, longer window
```

- Small-volume components (for example PAY.BNK) use a minimum sample size and a Wilson lower bound, so a handful of errors does not page anyone.
- Absolute threshold rule from config, so "past a rate" works even without a baseline:

```yaml
thresholds:
  default:  {err_rate: 0.05, window_s: 10, min_n: 20}
  PAY.BNK:  {err_rate: 0.10, window_s: 30, min_n: 5}
  ADM.AUD:  {err_rate: 0.01, window_s: 10, min_n: 20}   # audit trail is compliance-critical
```

- CUSUM on the 60 s rate for slow drift.
- Code-mix drift: for each component with meaningful codes (EDT denials first), compare the current 60 s code distribution to the baseline distribution with Jensen-Shannon divergence. The alert names the code that moved most. This catches a bad rule deploy while the overall error rate stays flat.
- Latency shift: p95 from the log2 histogram versus baseline p95.
- New-code detector: a code not seen in the catalog or in the baseline period appears at a meaningful rate.
- Silence: `sum10 == 0` where the baseline expects volume. Zero errors from a silent component looks healthy to a rate detector, so this is separate.
- Hysteresis: open after K consecutive breaching seconds (K=2 for fast, K=5 for slow), resolve after M quiet seconds (for example 30).

### 8.7 Severity

Score combines statistical strength, duration, component criticality and business impact.

| Input | Source |
|---|---|
| Deviation strength | z-score and rate ratio versus baseline |
| Duration | seconds in breach |
| Criticality weight | config, e.g. `PAY.LDG`, `PAY.BNK`, `ADM.AUD` high; `ADM.RPT` low |
| Burn rate | `burn = window_err_rate / (1 - slo_target)`; fast burn (both 1 h and 5 min windows over 14.4x) pages, slow burn (6 h and 30 min over 6x) tickets. In the demo, windows are scaled down proportionally. |
| Payment-cycle proximity | configured cutoff (accelerated in demo); closer cutoff raises severity |

Levels: INFO, WARNING, HIGH, CRITICAL, with hysteresis so severity does not flap.

### 8.8 Incident engine

- Group alerts by time window, dependency edge (from the propagation config) and shared deploy marker into one incident.
- Suspected origin = the earliest-breaching component in the dependency chain.
- Lifecycle: open, acknowledged, resolved, with cooldown and deduplication to prevent alert storms.

### 8.9 Handling unidentifiable logs (graceful)

Principle: the detector never crashes, never silently drops, and never lets a bad line corrupt a count. Every problem line is classified, counted and sampled.

| Class | How it is detected | Handling |
|---|---|---|
| malformed_header | line shorter than 46 bytes, or a separator byte is wrong | Not counted toward any component. Recorded in `unk`. |
| bad_timestamp | non-digit in bytes 0-12, or far from the watermark (clock skew) | Not counted. If slightly late (within reorder slack), placed into its own second. Otherwise recorded as `late` or `future`. |
| bad_level | level byte outside `I W E F` | Recorded. Line is not counted as an error. |
| unknown_component | 7-byte key not in `KNOWN_KEYS` | Counted per unknown key (dict capped at about 1,000 keys, overflow goes to one bucket). |
| unknown_code | known component, code not in catalog | Counted in the total. If level is E or F, it still counts toward the error rate as `unmapped`. Feeds the new-code detector. |
| oversized_line | more than about 8 KB | Truncated for sampling. Counted. |
| non_utf8 message | bytes that do not decode | The detector works on bytes, so this is only visible when decoding evidence. Decode with replacement. |

Surfacing:

- A dashboard panel "Unidentified logs" shows counts by class, the top unknown keys, and a sample ring of recent lines (first 3 per unique key plus the last 200 overall).
- `unidentified_ratio_60s` is a health metric. Above 0.5% raises WARNING "log format drift". Above 5% raises HIGH, because monitoring quality is degraded.
- An unknown component that sends 20 or more lines in 30 seconds raises an INFO "new component discovered" alert with an Adopt action. Adopting assigns a preallocated slot, starts baseline warm-up, and suppresses alerts until warm-up ends.
- The `log_format_drift` scenario in section 6.3 exercises all of this.

Other robustness:

- Partial lines at chunk boundaries are carried over, not parsed.
- CRLF endings are tolerated by stripping a trailing carriage return.
- Late events within 2 seconds still update their own second. Older ones are counted as `late_dropped`.
- The detector emits a heartbeat and self-metrics (queue depth, lag in seconds, lines per second, dropped counts) so the monitor's own health is visible.

### 8.10 Concurrency, backpressure and speed budget

- One asyncio loop: reader task, parse and update, per-second evaluation, then a bounded `asyncio.Queue` to the WebSocket broadcaster.
- The outbox worker for AWS runs separately (executor or thread), because boto3 is blocking.
- The reader never blocks on the UI. If the queue is full, drop the oldest UI messages and count them. Alerts always go to the outbox first.
- Avoid per-line Python objects, regex, JSON parsing and logging in the hot path.
- Build the reference per-line parser first for correctness, then the vectorized parser, and benchmark both.

Measure and report on your own machine (do not estimate):

| Metric | How |
|---|---|
| Throughput (lines/s) | replay a pre-generated file of many millions of lines as fast as possible, per parser |
| Detection delay (s) | alert event time minus fault start in `ground_truth.jsonl`, across severities |
| Processing latency (ms) | wall-clock alert emission minus the event time of the triggering line, at real-time replay |
| CPU and memory | at normal load and at load mode |
| Headroom | highest sustained `--rps-scale` with lag staying near zero |

Chart to show in the demo: detection delay versus fault severity.

## 9. Alert schema

```json
{
  "incident_id": "inc_0142",
  "alert_id": "a_0391",
  "type": "error_rate_spike",
  "key": "CLM.STR",
  "service": "claim-adjudication",
  "severity": "HIGH",
  "observed": 0.27,
  "baseline": {"mean": 0.002, "band": [0.0, 0.006]},
  "z": 38.1,
  "window_s": 10,
  "burn_rate": 21.4,
  "cycle_cutoff_in_min": 340,
  "suspected_origin": "CLM.STR",
  "reason": "CLM.STR error rate 27% vs baseline 0.2%, sustained 6 s; top code E4410",
  "evidence": [{"offset": 8812345, "line": "…scrubbed…"}],
  "opened_at_ms": 1790603432000,
  "status": "open"
}
```

Alert types: `error_rate_spike`, `error_rate_threshold`, `code_mix_drift`, `latency_shift`, `silence`, `new_code`, `log_format_drift`, `new_component`, `detector_health`.

## 10. Delivery

Dashboard:

- FastAPI WebSocket for push, `GET /alerts?since=` for polling fallback.
- On connect the client receives a snapshot, then a live stream.

AWS (through an outbox so failures never lose alerts):

- SQLite outbox table: `id, payload, idempotency_key, attempts, next_attempt_at, status`. Retry with exponential backoff.
- SNS topic with message attributes `severity`, `service`, `type`, `incident_id`. Subscription filter policies route CRITICAL to SMS or a webhook and lower severities to email.
- CloudWatch Logs group `/sentinel/alerts`: one structured JSON event per alert (PHI-scrubbed).
- CloudWatch custom metrics via Embedded Metric Format (namespace `Sentinel`, for example `ErrorRate` per service and component) plus one CloudWatch alarm on the metric, to show the two systems working together.
- `--dry-run` mode, and LocalStack via docker-compose for offline demos.
- Terraform module for the topic, log group, alarm and a least-privilege IAM policy.

## 11. Dashboards

### 11.1 Detector dashboard (dashboard 1)

- Live per-service health grid, drill down to components.
- Error-rate chart with baseline band and threshold line.
- Alert and incident feed with severity colors, acknowledge action and an "explain this alert" panel (baseline, z-score, top codes, evidence lines, suspected origin).
- Code-mix view for EDT denials.
- Unidentified logs panel and detector health (lag, throughput, drops).
- Replay: time-machine scrubber over the virtual clock.
- Optional, off by default: overlay of ground-truth fault markers, clearly labelled, to show detection delay.

### 11.2 Fault Console (dashboard 2)

See section 6.4. One React app with two routes and a shared design system.

## 12. Security, PHI and compliance framing

- Redaction runs before anything is stored, shown, or exported. Rules cover SSN-like patterns, dates of birth, emails, phone numbers and member-ID formats.
- Canary test: the `phi_leak_canary` scenario injects fake identifiers. An automated test asserts that none appear in the UI payloads, the SQLite outbox, SNS payloads or CloudWatch events.
- Append-only audit log for alerts, acknowledgments and config changes.
- Least-privilege IAM, control API bound to localhost, token when deployed.
- Say "designed with HIPAA and HITRUST-style controls in mind". Do not claim compliance.
- Optional Bedrock incident summaries only if time allows, with: redacted template-level inputs only, evidence links for every statement, no autonomous actions, human approval, and a one-page model card with a risk tier, inputs, limits and human override.

## 13. Evaluation plan

Run every scenario in section 6.3 against three detectors: a static threshold, a plain z-score, and Sentinel. Report:

| Metric | Notes |
|---|---|
| Detection delay | median and p95, per scenario |
| Precision and recall | against `ground_truth.jsonl` |
| False alerts per hour | especially on `nightly_batch_surge` and normal traffic |
| Alerts per incident | shows the value of grouping |
| Throughput and processing latency | section 8.10 |
| Unidentified-log handling | ratio reported, no crashes, no count corruption |
| PHI canary | zero leaks |

Use only measured numbers in the README and pitch.

## 14. Repo layout

```
sentinel/
  sim/          generator.py  faults.py  clock.py  control_api.py  platform.yaml  catalog.yaml
  detector/
    tailer.py        # chunked reader, rotation, carry buffer
    parse_ref.py     # per-line reference parser (tests)
    parse_batch.py   # numpy batch parser
    rings.py         # preallocated arrays, window sums
    baseline.py  detectors.py  severity.py  incidents.py
    unidentified.py  redact.py
  delivery/     outbox.py  sns.py  cloudwatch.py
  api/          app.py  ws.py
  eval/         scenarios.yaml  runner.py  report.py  bench.py
  web/          detector/  console/   (React + Vite)
  infra/        terraform/  iam-policy.json  docker-compose.yml
  tests/        test_parsers_equal.py  test_windows.py  test_unidentified.py  test_phi_canary.py
  docs/         architecture.md  model-card.md
```

## 15. Build order and priorities

| Priority | Deliverable |
|---|---|
| Must | Catalog, generator with per-component rates and dependencies, unified log, tailer, reference parser, rings and windows, baseline, error-rate detector and threshold rule, severity, WebSocket dashboard, SNS and CloudWatch delivery, Fault Console API and CLI, basic unidentified-log counters |
| Win | Vectorized batch parser and benchmark, code-mix drift, silence, new-code, incident grouping, unidentified-logs panel with adopt action, Fault Console UI, replay scrubber, PHI canary test, evaluation table |
| Stretch | Business-impact and payment-cycle severity refinements, latency detector, seasonal baseline, Terraform, Bedrock summaries with model card, Isolation Forest |

Phases:

1. Catalog, generator, log format, reference parser, tailer, tests.
2. Rings, windows, baseline, error-rate detector, severity, hysteresis.
3. Incident engine, WebSocket and REST, detector dashboard, outbox with SNS and CloudWatch (LocalStack first).
4. Fault Console API, scenarios, evaluation runner, ground truth.
5. Vectorized parser and benchmark, drift and silence detectors, unidentified-log handling, PHI canary.
6. Polish: replay, Fault Console UI, README, demo script, fallback video.

If time runs short, cut from the bottom of the priority table. Never cut the baseline, the error-rate detection per component and service, severity, or AWS delivery.

## 16. Demo script (about 4 minutes)

1. Show live traffic across 5 services, the baseline bands, and the per-component grid. Fault Console open beside it.
2. Push `bad_rule_deploy`: error rate stays flat, the code-mix detector names W4103 as the drifting edit code.
3. Push `db_timeout_spike` on CLM.STR: CRITICAL alert with burn rate and payment-cycle impact. Show the SNS message arriving and the same alert in CloudWatch.
4. Show the cascade (PAY.RMT, ADM.RPT, ADM.FWA) collapse into one incident with CLM.STR as suspected origin.
5. Silence `PAY.BNK`: silence alert.
6. Trigger `log_format_drift`: the Unidentified logs panel fills, the format-drift warning fires, nothing crashes.
7. Scrub back in time and show the explanation panel.
8. Show the evaluation table, the benchmark chart, and the PHI canary test passing.

Fallbacks: LocalStack instead of real AWS, a pre-recorded video, and a pre-generated log dump for offline replay.

## 17. Risks and open questions

- Speed claims: measure before stating numbers. If the vectorized parser is not clearly faster, present the reference parser and explain why.
- Scope: 20 components and 10 scenarios is a lot. The generator config and scenario runner should be data-driven, so adding a component is a YAML change.
- Baseline contamination and warm-up: freeze baselines during incidents, and give the demo a warm-up period or a pre-seeded baseline.
- Naming: "admin" is a platform service here, so the control UI is called the Fault Console.
- Unknowns to confirm with organizers: duration, team size, judging rubric, AWS credentials, whether the evaluation uses a provided log file (support arbitrary files by making the format adapter pluggable: a small adapter converting an external format into this one).

## 18. References

- Acentra Health: solutions https://acentra.com/solutions/ ; about https://acentra.com/about-us ; claims and encounters https://acentra.com/solutions/claims-and-encounters ; Innovation Challenge https://acentra.com/news/acentra-health-drives-unrelenting-focus-on-technology-advancement-and-process-improvement-with-innovation-challenge ; HITRUST recertification https://acentra.com/news/hitrust-recertification-eqsuite-imedecs-platforms/ ; Chennai brand event https://acentra.com/news/acentra-health-celebrates-u-s-india-technology-partnership-at-brand-celebration
- AWS and Acentra: https://aws.amazon.com/government-education/aws-champions/acentra-health
- Safe AI in Medicaid Alliance: https://hitconsultant.net/2025/09/09/acentra-health-launches-safe-ai-in-medicaid-alliance-sama-to-develop-ai-governance-frameworks/
- CloudWatch log anomaly detection: https://docs.aws.amazon.com/console/cloudwatch/logs/anomalies ; https://aws.amazon.com/blogs/aws/amazon-cloudwatch-logs-now-offers-automated-pattern-analytics-and-anomaly-detection/
- CloudWatch metric anomaly detection: https://aws.amazon.com/blogs/mt/operationalizing-cloudwatch-anomaly-detection/
- Log anomaly detection comparison (DeepLog, LogBERT): https://dl.ifip.org/db/conf/cnsm/cnsm2025/1571164872.pdf
- Drain online log parsing (He et al., ICWS 2017) and Drain3 production fork