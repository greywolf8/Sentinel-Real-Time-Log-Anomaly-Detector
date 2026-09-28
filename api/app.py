"""FastAPI application for Sentinel detector (docs/sentinel-plan.md section 10).

Runs the detector in the same asyncio loop, bounded queue to WebSocket broadcaster,
snapshot on connect then live stream, GET /alerts?since= polling fallback, acknowledge
endpoint, unidentified-log and health endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from detector.incidents import IncidentEngine
from detector.parse_ref import Vocabulary, load_vocabulary
from detector.pipeline import Pipeline, PipelineOptions, build_pipeline
from detector.redact import Redactor
from detector.severity import ScoredAlert
from delivery.cloudwatch import CloudWatchDelivery, CloudWatchConfig, create_cloudwatch_delivery
from delivery.outbox import Outbox, OutboxWorker
from sim.config import load_catalog, load_platform

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Section 10: FastAPI app
app = FastAPI(title="Sentinel Detector API", version="1.0.0")

# Global state
pipeline: Pipeline | None = None
incident_engine: IncidentEngine | None = None
redactor: Redactor | None = None
vocab: Vocabulary | None = None
catalog: dict[str, Any] | None = None
platform: dict[str, Any] | None = None
stop_event: asyncio.Event | None = None
outbox: Outbox | None = None
outbox_worker: OutboxWorker | None = None
cloudwatch: CloudWatchDelivery | None = None

# WebSocket broadcaster state
class ConnectionState:
    """Track WebSocket connections and broadcasting."""

    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()
        self.alert_history: list[dict[str, Any]] = []
        self.max_history = 1000
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self.connections.discard(websocket)

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Broadcast to all connected WebSockets, drop oldest UI messages if needed."""
        async with self._lock:
            # Add to history
            self.alert_history.append(message)
            if len(self.alert_history) > self.max_history:
                self.alert_history.pop(0)

            # Broadcast to all connections
            disconnected = set()
            for ws in self.connections:
                try:
                    await ws.send_json(message)
                except Exception:
                    disconnected.add(ws)

            # Clean up disconnected
            for ws in disconnected:
                self.connections.discard(ws)

    async def get_snapshot(self) -> dict[str, Any]:
        """Get current state snapshot for new connections."""
        async with self._lock:
            return {
                "alerts": list(self.alert_history),
                "connection_count": len(self.connections),
            }


connection_state = ConnectionState()


# Pydantic models for requests/responses
class AcknowledgeRequest(BaseModel):
    incident_id: str


class HealthResponse(BaseModel):
    status: str
    pipeline: dict[str, Any]
    incidents: dict[str, Any]
    unidentified: dict[str, Any]
    connections: int


# Startup and shutdown
@app.on_event("startup")
async def startup() -> None:
    """Initialize detector pipeline on startup."""
    global pipeline, incident_engine, redactor, vocab, catalog, platform, stop_event, outbox, outbox_worker, cloudwatch

    logger.info("Starting Sentinel detector API")

    # Load configuration
    catalog = load_catalog()
    platform = load_platform()
    vocab = load_vocabulary()

    # Initialize redactor
    redactor = Redactor()

    # Create pipeline
    log_path = Path("platform.log")
    options = PipelineOptions(
        log_path=log_path,
        catalog=catalog,
        vocab=vocab,
        follow=True,
        start_at_end=False,
        warmup_s=120.0,
    )
    pipeline = build_pipeline(log_path, catalog=catalog, vocab=vocab, follow=True, start_at_end=False)

    # Create incident engine with propagation edges
    propagation_edges = platform.get("propagation", [])
    incident_engine = IncidentEngine(propagation_edges=propagation_edges)

    # Initialize outbox
    outbox = Outbox()

    # Initialize CloudWatch delivery (dry-run by default for local dev)
    dry_run = os.environ.get("CLOUDWATCH_DRY_RUN", "true").lower() == "true"
    cloudwatch = create_cloudwatch_delivery(dry_run=dry_run)

    # Create outbox worker with CloudWatch delivery function
    def deliver_to_cloudwatch(payload: dict[str, Any]) -> bool:
        try:
            return cloudwatch.put_log_event(payload)
        except Exception as e:
            logger.error(f"CloudWatch delivery failed: {e}")
            return False

    outbox_worker = OutboxWorker(outbox, deliver_to_cloudwatch)
    await outbox_worker.start()

    # Start detector task
    stop_event = asyncio.Event()
    asyncio.create_task(run_detector())

    logger.info("Detector pipeline started")


@app.on_event("shutdown")
async def shutdown() -> None:
    """Clean up on shutdown."""
    global stop_event, pipeline, outbox_worker

    logger.info("Shutting down Sentinel detector API")
    if stop_event:
        stop_event.set()
    if pipeline:
        pipeline.close()
    if outbox_worker:
        await outbox_worker.stop()


# Background detector task
async def run_detector() -> None:
    """Run the detector pipeline and broadcast alerts."""
    global pipeline, incident_engine, redactor, stop_event, outbox

    if not pipeline or not incident_engine or not redactor or not stop_event:
        return

    logger.info("Detector task running")

    while not stop_event.is_set():
        try:
            # Follow the log
            await pipeline.follow(stop=stop_event)

            # Process any pending alerts
            alerts = pipeline.drain()
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

            for alert in alerts:
                # Redact evidence
                redacted_alert = redact_alert(alert, redactor)

                # Process through incident engine
                incident = incident_engine.process_alert(alert, now_ms)

                # Broadcast alert
                alert_json = {
                    "alert": redacted_alert,
                    "incident": incident.to_json(),
                }
                await connection_state.broadcast(alert_json)

                # Add to outbox for CloudWatch delivery
                if outbox:
                    idempotency_key = f"{alert.alert.alert_id}_{alert.alert.opened_at_ms}"
                    outbox.add(redacted_alert, idempotency_key)

            # Auto-resolve stale incidents
            incident_engine.auto_resolve_stale(now_ms)

            # Small sleep to prevent busy loop
            await asyncio.sleep(0.1)

        except Exception as e:
            logger.error(f"Detector task error: {e}")
            await asyncio.sleep(1)


def redact_alert(alert: ScoredAlert, redactor: Redactor) -> dict[str, Any]:
    """Redact PHI from alert evidence and messages."""
    alert_json = alert.to_json()

    # Redact reason
    if "reason" in alert_json:
        alert_json["reason"] = redactor.redact(alert_json["reason"])

    # Redact evidence lines
    if "evidence" in alert_json:
        alert_json["evidence"] = [
            {"offset": e["offset"], "line": redactor.redact(e["line"])}
            for e in alert_json["evidence"]
        ]

    return alert_json


# HTTP endpoints
@app.get("/health")
async def get_health() -> HealthResponse:
    """Get detector health status."""
    if not pipeline or not incident_engine:
        return HealthResponse(
            status="initializing",
            pipeline={},
            incidents={},
            unidentified={},
            connections=len(connection_state.connections),
        )

    return HealthResponse(
        status="running",
        pipeline=pipeline.health(),
        incidents=incident_engine.to_json(),
        unidentified=pipeline.unk.to_json(),
        connections=len(connection_state.connections),
    )


@app.get("/alerts")
async def get_alerts(since: int | None = Query(None)) -> JSONResponse:
    """Get alerts since a given timestamp (polling fallback)."""
    if since is None:
        # Return all recent alerts
        async with connection_state._lock:
            alerts = list(connection_state.alert_history)
    else:
        # Filter by timestamp
        async with connection_state._lock:
            alerts = [
                a
                for a in connection_state.alert_history
                if a.get("alert", {}).get("opened_at_ms", 0) > since
            ]

    return JSONResponse({"alerts": alerts})


@app.get("/components")
async def get_components() -> JSONResponse:
    """Get per-component rate, baseline and state for dashboard grid."""
    if not pipeline:
        return JSONResponse({"components": []})

    return JSONResponse({"components": pipeline.component_table()})


@app.get("/incidents")
async def get_incidents() -> JSONResponse:
    """Get all incidents."""
    if not incident_engine:
        return JSONResponse({"incidents": {}})

    return JSONResponse(incident_engine.to_json())


@app.post("/incidents/{incident_id}/acknowledge")
async def acknowledge_incident(incident_id: str, request: AcknowledgeRequest) -> JSONResponse:
    """Acknowledge an incident."""
    if not incident_engine:
        return JSONResponse({"success": False, "error": "Incident engine not initialized"})

    success = incident_engine.acknowledge(incident_id)
    return JSONResponse({"success": success})


@app.post("/incidents/{incident_id}/resolve")
async def resolve_incident(incident_id: str, request: AcknowledgeRequest) -> JSONResponse:
    """Resolve an incident."""
    if not incident_engine:
        return JSONResponse({"success": False, "error": "Incident engine not initialized"})

    success = incident_engine.resolve(incident_id)
    return JSONResponse({"success": success})


@app.get("/unidentified")
async def get_unidentified() -> JSONResponse:
    """Get unidentified log statistics and samples."""
    if not pipeline:
        return JSONResponse({"unidentified": {}})

    return JSONResponse({"unidentified": pipeline.unk.to_json()})


@app.get("/stats")
async def get_stats() -> JSONResponse:
    """Get detector statistics."""
    if not pipeline:
        return JSONResponse({"stats": {}})

    return JSONResponse({"stats": pipeline.stats.to_json()})


# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket endpoint for live alert streaming."""
    await connection_state.connect(websocket)

    try:
        # Send snapshot on connect
        snapshot = await connection_state.get_snapshot()
        await websocket.send_json({"type": "snapshot", "data": snapshot})

        # Keep connection alive and handle client messages
        while True:
            try:
                message = await websocket.receive_text()
                # Handle client messages if needed (e.g., acknowledgments)
                logger.debug(f"Received WebSocket message: {message}")
            except WebSocketDisconnect:
                break
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        await connection_state.disconnect(websocket)


# Run with: uvicorn api.app:app --reload --host 0.0.0.0 --port 8000
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
