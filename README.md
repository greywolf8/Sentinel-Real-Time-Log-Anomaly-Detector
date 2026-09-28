# Sentinel: Real-Time Log Anomaly Detector

A production-grade real-time log anomaly detection system for Medicaid claims platforms, built for the Acentra Health Innovation Challenge.

## Overview

Sentinel is a real-time monitoring system that tails unified logs from a simulated Medicaid claims platform, learns normal behavior patterns across 20 components, and detects error-rate deviations within seconds. It explains each alert, groups related alerts into incidents, scrubs PHI, and pushes alerts to live dashboards and AWS services (SNS, CloudWatch).

**Key Features:**
- **Real-time detection**: Sub-second latency from log line to alert
- **Multi-layered detection**: Error-rate spikes, code-mix drift, latency shifts, silence detection, new-code detection
- **Explainable alerts**: Every alert includes statistical evidence, baseline comparison, and suspected origin
- **Incident grouping**: Cascading failures collapse into single incidents with root cause identification
- **PHI redaction**: Built-in scrubbing of SSNs, dates of birth, emails, phones, and member IDs
- **AWS integration**: CloudWatch Logs, Embedded Metrics, and SNS with subscription filters
- **Interactive dashboards**: React-based detector dashboard and fault injection console
- **Robust handling**: Graceful processing of malformed, unknown, and future logs without crashes

## Architecture

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
    ground_truth.jsonl <----|  FastAPI WebSocket + REST            |
    (never read by detector) +---------+---------------+------------+
                                          |               |
                            Detector dashboard     AWS SNS + CloudWatch
                            (dashboard 1)
```

## Quick Start

### Prerequisites
- Python 3.12
- Node.js 18+ (for web dashboards)
- Docker (for LocalStack AWS testing)

### Installation

```bash
# Install Python dependencies
python3.12 -m pip install -e ".[dev,aws]"

# Install web dashboard dependencies
cd web/detector && npm install
cd ../console && npm install
```

### Running the System

```bash
# Terminal 1: Start the log generator (simulated Medicaid platform)
python3.12 -m sim.generator --seed 7 --speed 4

# Terminal 2: Start the control API (for fault injection)
python3.12 -m sim.control_api --log platform.log

# Terminal 3: Start the detector with API
python3.12 -m api.app

# Terminal 4: Start detector dashboard
cd web/detector && npm run dev

# Terminal 5: Start fault console
cd web/console && npm run dev
```

### Demo Scenarios

The Fault Console includes 8 pre-configured scenarios to demonstrate detection capabilities:

1. **db_timeout_spike** - Database timeout cascade across services
2. **slow_degradation** - Gradual error rate increase over 10 minutes
3. **bad_rule_deploy** - Code-mix drift without error rate change
4. **new_error_after_deploy** - Detection of unseen error codes
5. **upstream_cascade** - Fault propagation through dependency chain
6. **service_silent** - Detection when a component stops logging
7. **nightly_batch_surge** - Volume surge without errors (false positive control)
8. **log_format_drift** - Graceful handling of malformed logs
9. **phi_leak_canary** - PHI redaction verification

## Detection Capabilities

### Error-Rate Detection
- Fast z-score detection (10s window) for sudden spikes
- Slow z-score detection (60s window) for gradual drift
- CUSUM detection for slow-moving degradation
- Absolute threshold rules per component
- Service-level and system-level roll-ups

### Advanced Detectors
- **Code-mix drift**: Jensen-Shannon divergence on denial code distributions (catches bad rule deploys)
- **Latency shift**: p95 latency detection from log2-binned histograms
- **New-code detection**: Alerts on codes not in catalog or baseline
- **Silence detection**: Detects when components stop logging unexpectedly
- **Unidentified logs**: Classifies and samples malformed, unknown, and corrupted lines

### Incident Management
- Groups related alerts by time window, dependency edges, and deploy markers
- Identifies suspected origin using dependency graph
- Lifecycle: open → acknowledged → resolved with cooldown and deduplication
- Reduces alert noise by collapsing cascades into single incidents

## Log Format

Sentinel uses a fixed-width 46-byte header for high-performance parsing:

```
0         1         2         3         4
|0123456789012345678901234567890123456789012345
1790603412345|E|CLM|STR|E4410|003002|b2c7e0d9|CLAIM_DB_TIMEOUT table=claims after_ms=3000
```

| Bytes | Field | Description |
|-------|-------|-------------|
| 0-12 | timestamp | 13-digit epoch milliseconds |
| 14 | level | I (info), W (warn), E (error), F (fatal) |
| 16-18 | service | PAY, CLM, ELG, PRV, ADM |
| 20-22 | component | 3-character component code |
| 24-28 | code | 00000 (success) or error code |
| 30-35 | latency ms | Zero-padded 6 digits |
| 37-44 | trace id | 8 hex characters |
| 46+ | message | Free text (never parsed on hot path) |

## Security & Compliance

- **PHI Redaction**: Scrubs SSN-like patterns, DOBs, emails, phones, member IDs before any alert leaves the detector
- **Canary Test**: Automated test injects fake PHI and verifies zero leakage
- **Least-privilege IAM**: Control API bound to localhost, token when deployed
- **Audit Trail**: Append-only log for alerts, acknowledgments, and config changes
- **Design Philosophy**: "Designed with HIPAA and HITRUST-style controls in mind" - does not claim compliance

## Performance

- **Vectorized batch parser**: NumPy-based parsing processes thousands of lines per chunk
- **Incremental window sums**: Constant-time updates regardless of window size
- **Preallocated data structures**: No runtime allocation on hot path
- **Bounded queues**: Prevents backpressure from blocking detection
- **Benchmark tool**: `eval/bench.py` compares reference and batch parsers

## Project Structure

```
sentinel/
├── sim/              # Log generator and fault injection
│   ├── generator.py  # 20-component asyncio generator
│   ├── control_api.py # FastAPI control endpoints
│   ├── faults.py     # Fault layer and scenarios
│   ├── catalog.yaml  # Error code catalog
│   └── platform.yaml # Component config and dependencies
├── detector/         # Core detection pipeline
│   ├── tailer.py     # Chunked file reader
│   ├── parse_batch.py # NumPy vectorized parser
│   ├── parse_ref.py  # Reference parser (correctness)
│   ├── rings.py      # Preallocated data structures
│   ├── baseline.py   # EWMA baseline management
│   ├── detectors.py  # Error-rate detectors
│   ├── detectors_advanced.py # Drift, latency, silence detectors
│   ├── severity.py   # Severity scoring
│   ├── incidents.py  # Incident grouping
│   ├── redact.py     # PHI redaction
│   └── unidentified.py # Malformed log handling
├── api/              # FastAPI application
│   └── app.py        # WebSocket + REST endpoints
├── delivery/         # AWS integration
│   ├── outbox.py     # SQLite outbox with retry
│   └── cloudwatch.py # CloudWatch Logs + EMF metrics
├── web/              # React dashboards
│   ├── detector/     # Monitoring dashboard
│   └── console/      # Fault injection console
├── infra/            # Infrastructure as code
│   ├── terraform/    # CloudWatch resources
│   └── docker-compose.yml # LocalStack for local AWS
├── eval/             # Evaluation and benchmarking
│   ├── bench.py      # Parser benchmark
│   └── scenarios.yaml # Test scenarios
├── tests/            # Test suite (289+ passing)
└── docs/             # Documentation
    └── sentinel-plan.md # Complete specification
```

## Testing

```bash
# Run all tests
pytest

# Run specific test categories
pytest test_detectors_advanced.py  # Advanced detectors
pytest test_incidents.py          # Incident grouping
pytest test_redact.py             # PHI redaction
pytest test_e2e_smoke.py         # End-to-end smoke test
```

Test coverage includes:
- Log format validation (46-byte header, separators, ASCII)
- Determinism (same seed → same bytes)
- Parser equality (reference vs batch parser)
- Per-detector tests with known faults
- Cascade and incident grouping tests
- PHI canary test (zero leakage assertion)

## Configuration

Key configuration files:
- `sim/platform.yaml` - Component rates, dependencies, propagation rules
- `sim/catalog.yaml` - Error codes, levels, weights, message templates
- `detector/config.yaml` - Thresholds, criticality weights, burn-rate targets

## Dependencies

**Python:**
- pyyaml - Configuration loading
- fastapi, uvicorn - API server
- httpx - HTTP client for control API
- numpy - Vectorized batch parsing
- boto3 - AWS SDK (optional)

**Dev:**
- pytest, pytest-asyncio - Testing
- ruff - Linting and formatting

**Web:**
- React 18 - UI framework
- Vite - Build tool

## Development Status

**Completed (Phases 0-3):**
- ✅ Log generator with 5 services, 20 components, 46 error codes
- ✅ Fixed-width 46-byte log format
- ✅ Control API with 8 fault scenarios
- ✅ Vectorized batch parser (NumPy)
- ✅ Core detectors (error-rate, threshold, CUSUM)
- ✅ Advanced detectors (code-mix drift, latency shift, silence, new-code)
- ✅ Incident engine with grouping and root cause
- ✅ Severity scoring with burn-rate and payment-cycle impact
- ✅ PHI redaction with canary test
- ✅ FastAPI with WebSocket and REST
- ✅ SQLite outbox with retry logic
- ✅ CloudWatch integration with EMF metrics
- ✅ React dashboards (detector + fault console)
- ✅ 289+ passing tests

**Remaining (Phase 4):**
- ⏳ Evaluation runner with ground-truth comparison
- ⏳ Baseline comparison against static threshold and z-score
- ⏳ Performance benchmarking and metrics

## Documentation

- **Spec**: `docs/sentinel-plan.md` - Complete system specification (source of truth)
- **Conventions**: `AGENTS.md` - Development conventions and ground rules
- **Progress**: `PROGRESS.md` - Implementation status and deviations

## License

Internal project for Acentra Health Innovation Challenge.

## Acknowledgments

Designed for the Acentra Health Innovation Challenge, inspired by real-world Medicaid claims processing challenges and Acentra's evoBrix X platform architecture.
