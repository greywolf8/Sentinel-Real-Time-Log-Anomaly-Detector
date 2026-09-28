import React, { useState, useEffect, useCallback } from 'react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';

function App() {
  const [health, setHealth] = useState(null);
  const [components, setComponents] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [incidents, setIncidents] = useState({});
  const [unidentified, setUnidentified] = useState({});
  const [selectedComponent, setSelectedComponent] = useState(null);
  const [selectedAlert, setSelectedAlert] = useState(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [error, setError] = useState(null);
  const [usingPolling, setUsingPolling] = useState(false);

  // WebSocket connection
  useEffect(() => {
    let ws = null;
    let pollingInterval = null;

    const connectWebSocket = () => {
      try {
        ws = new WebSocket('ws://localhost:8000/ws');

        ws.onopen = () => {
          console.log('WebSocket connected');
          setWsConnected(true);
          setUsingPolling(false);
          setError(null);
        };

        ws.onmessage = (event) => {
          const data = JSON.parse(event.data);
          if (data.type === 'snapshot') {
            setAlerts(data.data.alerts || []);
          } else if (data.alert) {
            setAlerts(prev => [data.alert, ...prev].slice(0, 100));
          }
        };

        ws.onerror = (err) => {
          console.error('WebSocket error:', err);
          setError('WebSocket connection failed, falling back to polling');
          setWsConnected(false);
          startPolling();
        };

        ws.onclose = () => {
          console.log('WebSocket closed');
          setWsConnected(false);
          startPolling();
        };
      } catch (err) {
        console.error('Failed to create WebSocket:', err);
        setError('Failed to create WebSocket, falling back to polling');
        startPolling();
      }
    };

    const startPolling = () => {
      setUsingPolling(true);
      if (pollingInterval) clearInterval(pollingInterval);
      pollingInterval = setInterval(fetchAlerts, 5000);
    };

    connectWebSocket();

    // Initial data fetch
    fetchHealth();
    fetchComponents();
    fetchIncidents();
    fetchUnidentified();

    // Regular health updates
    const healthInterval = setInterval(fetchHealth, 10000);
    const componentInterval = setInterval(fetchComponents, 5000);

    return () => {
      if (ws) ws.close();
      if (pollingInterval) clearInterval(pollingInterval);
      if (healthInterval) clearInterval(healthInterval);
      if (componentInterval) clearInterval(componentInterval);
    };
  }, []);

  const fetchHealth = async () => {
    try {
      const response = await fetch('/api/health');
      const data = await response.json();
      setHealth(data);
    } catch (err) {
      console.error('Failed to fetch health:', err);
    }
  };

  const fetchComponents = async () => {
    try {
      const response = await fetch('/api/components');
      const data = await response.json();
      setComponents(data.components || []);
    } catch (err) {
      console.error('Failed to fetch components:', err);
    }
  };

  const fetchIncidents = async () => {
    try {
      const response = await fetch('/api/incidents');
      const data = await response.json();
      setIncidents(data);
    } catch (err) {
      console.error('Failed to fetch incidents:', err);
    }
  };

  const fetchUnidentified = async () => {
    try {
      const response = await fetch('/api/unidentified');
      const data = await response.json();
      setUnidentified(data.unidentified || {});
    } catch (err) {
      console.error('Failed to fetch unidentified:', err);
    }
  };

  const fetchAlerts = async () => {
    try {
      const since = alerts.length > 0 ? alerts[0].alert?.opened_at_ms : 0;
      const response = await fetch(`/api/alerts?since=${since}`);
      const data = await response.json();
      if (data.alerts && data.alerts.length > 0) {
        setAlerts(prev => [...data.alerts, ...prev].slice(0, 100));
      }
    } catch (err) {
      console.error('Failed to fetch alerts:', err);
    }
  };

  const acknowledgeIncident = async (incidentId) => {
    try {
      await fetch(`/api/incidents/${incidentId}/acknowledge`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ incident_id: incidentId }),
      });
      fetchIncidents();
    } catch (err) {
      console.error('Failed to acknowledge incident:', err);
    }
  };

  const resolveIncident = async (incidentId) => {
    try {
      await fetch(`/api/incidents/${incidentId}/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ incident_id: incidentId }),
      });
      fetchIncidents();
    } catch (err) {
      console.error('Failed to resolve incident:', err);
    }
  };

  const getSeverityClass = (severity) => {
    return severity?.toLowerCase() || 'info';
  };

  const getHealthClass = (component) => {
    if (component.state === 'breaching') {
      return component.z > 10 ? 'critical' : 'high';
    }
    if (component.z > 3) return 'warning';
    return 'ok';
  };

  const selectedComponentData = components.find(c => c.key === selectedComponent);

  // Generate chart data for selected component
  const chartData = selectedComponentData ? [
    { time: '10s', rate: selectedComponentData.rate_10s, baseline: selectedComponentData.baseline },
    { time: '60s', rate: selectedComponentData.rate_60s, baseline: selectedComponentData.baseline },
  ] : [];

  return (
    <div className="container">
      <div className="header">
        <h1>Sentinel Detector Dashboard</h1>
        <div className="status">
          {wsConnected ? '🟢 WebSocket Connected' : usingPolling ? '🟡 Polling Mode' : '🔴 Disconnected'}
          {health && ` | Lines/s: ${health.pipeline?.lines_per_second || 0} | Lag: ${health.pipeline?.lag_seconds || 0}s`}
        </div>
      </div>

      {error && (
        <div className="error">
          <div className="error-message">{error}</div>
        </div>
      )}

      {/* Component Health Grid */}
      <div className="card">
        <div className="card-header">Component Health</div>
        <div className="health-grid">
          {components.map(comp => (
            <div
              key={comp.key}
              className={`health-item ${getHealthClass(comp)}`}
              onClick={() => setSelectedComponent(comp.key)}
            >
              <div className="health-item-name">{comp.key}</div>
              <div className="health-item-rate">
                Rate: {(comp.rate_10s * 100).toFixed(2)}% | Z: {comp.z.toFixed(1)}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="grid grid-2">
        {/* Selected Component Details */}
        {selectedComponentData && (
          <div className="card">
            <div className="card-header">
              {selectedComponentData.key} Details
              <button className="btn btn-secondary" onClick={() => setSelectedComponent(null)}>
                Close
              </button>
            </div>
            <div className="stats-grid">
              <div className="stat-item">
                <div className="stat-label">10s Rate</div>
                <div className="stat-value">{(selectedComponentData.rate_10s * 100).toFixed(3)}%</div>
              </div>
              <div className="stat-item">
                <div className="stat-label">60s Rate</div>
                <div className="stat-value">{(selectedComponentData.rate_60s * 100).toFixed(3)}%</div>
              </div>
              <div className="stat-item">
                <div className="stat-label">Baseline</div>
                <div className="stat-value">{(selectedComponentData.baseline * 100).toFixed(3)}%</div>
              </div>
              <div className="stat-item">
                <div className="stat-label">Z-Score</div>
                <div className="stat-value">{selectedComponentData.z.toFixed(2)}</div>
              </div>
            </div>
            <div style={{ marginTop: '16px', height: '200px' }}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                  <XAxis dataKey="time" stroke="#94a3b8" />
                  <YAxis stroke="#94a3b8" />
                  <Tooltip
                    contentStyle={{ backgroundColor: '#1e293b', border: '1px solid #334155' }}
                  />
                  <Line type="monotone" dataKey="rate" stroke="#3b82f6" strokeWidth={2} />
                  <Line type="monotone" dataKey="baseline" stroke="#22c55e" strokeWidth={2} strokeDasharray="5 5" />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}

        {/* Alert Feed */}
        <div className="card">
          <div className="card-header">Alert Feed</div>
          <div className="alert-feed">
            {alerts.length === 0 ? (
              <div className="loading">No alerts yet</div>
            ) : (
              alerts.map((alert, idx) => (
                <div
                  key={idx}
                  className={`alert-item ${getSeverityClass(alert.alert?.severity)}`}
                  onClick={() => setSelectedAlert(alert)}
                >
                  <div className="alert-header">
                    <span className={`alert-severity ${getSeverityClass(alert.alert?.severity)}`}>
                      {alert.alert?.severity}
                    </span>
                    <span className="alert-time">
                      {new Date(alert.alert?.opened_at_ms).toLocaleTimeString()}
                    </span>
                  </div>
                  <div className="alert-key">{alert.alert?.key}</div>
                  <div className="alert-reason">{alert.alert?.reason}</div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>

      {/* Incidents */}
      <div className="card">
        <div className="card-header">Incidents</div>
        <div className="grid grid-3">
          {Object.entries(incidents.open_incidents || {}).map(([id, incident]) => (
            <div key={id} className="card" style={{ borderColor: '#3b82f6' }}>
              <div className="card-header" style={{ fontSize: '14px' }}>
                {id} - {incident.suspected_origin}
              </div>
              <div style={{ fontSize: '12px', marginBottom: '8px' }}>
                Status: {incident.status} | Alerts: {incident.alert_count}
              </div>
              <div style={{ display: 'flex', gap: '8px' }}>
                {incident.status === 'open' && (
                  <button
                    className="btn btn-primary"
                    onClick={() => acknowledgeIncident(id)}
                  >
                    Acknowledge
                  </button>
                )}
                {(incident.status === 'open' || incident.status === 'acknowledged') && (
                  <button
                    className="btn btn-secondary"
                    onClick={() => resolveIncident(id)}
                  >
                    Resolve
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Unidentified Logs */}
      <div className="card">
        <div className="card-header">Unidentified Logs</div>
        <div className="unidentified-panel">
          {Object.entries(unidentified.by_class || {}).map(([cls, count]) => (
            <div key={cls} className="unidentified-item">
              {cls}: {count}
            </div>
          ))}
        </div>
      </div>

      {/* Alert Detail Modal */}
      {selectedAlert && (
        <div style={{
          position: 'fixed',
          top: 0,
          left: 0,
          right: 0,
          bottom: 0,
          backgroundColor: 'rgba(0, 0, 0, 0.8)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          zIndex: 1000,
        }}>
          <div className="card" style={{ maxWidth: '600px', width: '90%', maxHeight: '80vh', overflowY: 'auto' }}>
            <div className="card-header">
              Alert Details
              <button className="btn btn-secondary" onClick={() => setSelectedAlert(null)}>
                Close
              </button>
            </div>
            <div style={{ fontSize: '14px', lineHeight: '1.6' }}>
              <div><strong>Component:</strong> {selectedAlert.alert?.key}</div>
              <div><strong>Severity:</strong> {selectedAlert.alert?.severity}</div>
              <div><strong>Type:</strong> {selectedAlert.alert?.type}</div>
              <div><strong>Reason:</strong> {selectedAlert.alert?.reason}</div>
              <div><strong>Z-Score:</strong> {selectedAlert.alert?.z?.toFixed(2)}</div>
              <div><strong>Observed Rate:</strong> {(selectedAlert.alert?.observed * 100).toFixed(2)}%</div>
              <div><strong>Baseline:</strong> {(selectedAlert.alert?.baseline * 100).toFixed(2)}%</div>
              <div><strong>Window:</strong> {selectedAlert.alert?.window_s}s</div>
              <div style={{ marginTop: '16px' }}>
                <strong>Evidence:</strong>
                {selectedAlert.alert?.evidence?.map((ev, i) => (
                  <div key={i} style={{ fontSize: '12px', color: '#94a3b8', marginTop: '4px' }}>
                    {ev.line}
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default App;
