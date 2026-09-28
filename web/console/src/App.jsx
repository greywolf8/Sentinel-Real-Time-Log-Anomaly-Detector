import React, { useState, useEffect } from 'react';

// Component configuration from the spec
const COMPONENTS = [
  // PAY service
  { key: 'PAY.RMT', service: 'PAY', component: 'RMT', default_rate: 60 },
  { key: 'PAY.EXP', service: 'PAY', component: 'EXP', default_rate: 8 },
  { key: 'PAY.LDG', service: 'PAY', component: 'LDG', default_rate: 120 },
  { key: 'PAY.BNK', service: 'PAY', component: 'BNK', default_rate: 4 },
  // CLM service
  { key: 'CLM.EDT', service: 'CLM', component: 'EDT', default_rate: 250 },
  { key: 'CLM.PRC', service: 'CLM', component: 'PRC', default_rate: 220 },
  { key: 'CLM.DUP', service: 'CLM', component: 'DUP', default_rate: 250 },
  { key: 'CLM.STR', service: 'CLM', component: 'STR', default_rate: 300 },
  // ELG service
  { key: 'ELG.MBR', service: 'ELG', component: 'MBR', default_rate: 180 },
  { key: 'ELG.VER', service: 'ELG', component: 'VER', default_rate: 170 },
  // PRV service
  { key: 'PRV.PST', service: 'PRV', component: 'PST', default_rate: 100 },
  { key: 'PRV.NPI', service: 'PRV', component: 'NPI', default_rate: 60 },
  { key: 'PRV.CRD', service: 'PRV', component: 'CRD', default_rate: 80 },
  // ADM service
  { key: 'ADM.AUT', service: 'ADM', component: 'AUT', default_rate: 50 },
  { key: 'ADM.AUD', service: 'ADM', component: 'AUD', default_rate: 30 },
  { key: 'ADM.RPT', service: 'ADM', component: 'RPT', default_rate: 20 },
  { key: 'ADM.FWA', service: 'ADM', component: 'FWA', default_rate: 10 },
];

// Scenarios from the spec
const SCENARIOS = [
  { name: 'db_timeout_spike', target: 'CLM.STR', err_rate: 0.25, hold_s: 90 },
  { name: 'slow_degradation', target: 'ELG.MBR', err_rate: 0.08, hold_s: 600 },
  { name: 'bad_rule_deploy', target: 'CLM.EDT', type: 'mix', hold_s: 60 },
  { name: 'new_error_after_deploy', target: 'PRV.CRD', type: 'new_code', hold_s: 60 },
  { name: 'upstream_cascade', target: 'ELG.MBR', err_rate: 0.15, hold_s: 120 },
  { name: 'service_silent', target: 'PAY.BNK', type: 'silence', hold_s: 60 },
  { name: 'nightly_batch_surge', target: 'PAY.EXP', type: 'volume', hold_s: 60 },
  { name: 'brute_force_login', target: 'ADM.AUT', type: 'warn_burst', hold_s: 30 },
];

// Dependency edges from the spec
const DEPENDENCIES = [
  { from: 'CLM.STR', to: 'PAY.RMT', coupling: 0.8, lag_s: 2 },
  { from: 'PAY.RMT', to: 'PAY.EXP', coupling: 0.9, lag_s: 1 },
  { from: 'PAY.RMT', to: 'PAY.LDG', coupling: 0.95, lag_s: 1 },
  { from: 'PAY.EXP', to: 'PAY.BNK', coupling: 0.85, lag_s: 2 },
  { from: 'ELG.MBR', to: 'CLM.EDT', coupling: 0.9, lag_s: 1 },
  { from: 'ELG.VER', to: 'CLM.EDT', coupling: 0.8, lag_s: 1 },
  { from: 'PRV.PST', to: 'CLM.EDT', coupling: 0.85, lag_s: 1 },
  { from: 'CLM.EDT', to: 'CLM.PRC', coupling: 0.95, lag_s: 1 },
  { from: 'CLM.STR', to: 'CLM.DUP', coupling: 0.7, lag_s: 1 },
  { from: 'CLM.STR', to: 'ADM.RPT', coupling: 0.6, lag_s: 5 },
  { from: 'CLM.STR', to: 'ADM.FWA', coupling: 0.5, lag_s: 5 },
  { from: 'ADM.AUT', to: 'ADM.AUD', coupling: 0.9, lag_s: 1 },
];

function App() {
  const [rates, setRates] = useState({});
  const [observedRates, setObservedRates] = useState({});
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState(null);
  const [selectedComponent, setSelectedComponent] = useState(null);
  const [activeScenario, setActiveScenario] = useState(null);

  useEffect(() => {
    // Initialize rates from defaults
    const initialRates = {};
    COMPONENTS.forEach(comp => {
      initialRates[comp.key] = comp.default_rate;
    });
    setRates(initialRates);

    // Fetch current rates from control API
    fetchRates();
    const interval = setInterval(fetchRates, 5000);

    return () => clearInterval(interval);
  }, []);

  const fetchRates = async () => {
    try {
      const response = await fetch('/api/rates');
      if (response.ok) {
        const data = await response.json();
        setObservedRates(data);
        setConnected(true);
        setError(null);
      } else {
        setConnected(false);
      }
    } catch (err) {
      console.error('Failed to fetch rates:', err);
      setConnected(false);
    }
  };

  const setComponentRate = async (key, rate) => {
    try {
      const [service, component] = key.split('.');
      await fetch(`/api/rates/${service}/${component}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rate, ramp_s: 0, hold_s: 300 }),
      });
      setRates(prev => ({ ...prev, [key]: rate }));
      fetchRates();
    } catch (err) {
      console.error('Failed to set rate:', err);
      setError('Failed to set rate');
    }
  };

  const runScenario = async (scenario) => {
    try {
      setActiveScenario(scenario.name);
      await fetch(`/api/scenarios/${scenario.name}/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      });
      // Refresh rates after scenario
      setTimeout(fetchRates, 1000);
    } catch (err) {
      console.error('Failed to run scenario:', err);
      setError('Failed to run scenario');
    }
  };

  const resetAll = async () => {
    try {
      await fetch('/api/reset', { method: 'POST' });
      setActiveScenario(null);
      // Reset to defaults
      const defaultRates = {};
      COMPONENTS.forEach(comp => {
        defaultRates[comp.key] = comp.default_rate;
      });
      setRates(defaultRates);
      fetchRates();
    } catch (err) {
      console.error('Failed to reset:', err);
      setError('Failed to reset');
    }
  };

  const silenceComponent = async (key) => {
    try {
      const [service, component] = key.split('.');
      await fetch(`/api/silence/${service}/${component}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hold_s: 60 }),
      });
    } catch (err) {
      console.error('Failed to silence component:', err);
      setError('Failed to silence component');
    }
  };

  const getDependencies = (key) => {
    return DEPENDENCIES.filter(d => d.from === key || d.to === key);
  };

  const selectedComponentData = COMPONENTS.find(c => c.key === selectedComponent);

  return (
    <div className="container">
      <div className="header">
        <h1>Sentinel Fault Console</h1>
        <div className="status">
          <span className={`status-indicator ${connected ? 'connected' : 'disconnected'}`}></span>
          {connected ? 'Connected to Control API' : 'Disconnected from Control API'}
        </div>
      </div>

      {error && (
        <div className="error">
          <div className="error-message">{error}</div>
        </div>
      )}

      {/* Scenario Presets */}
      <div className="card">
        <div className="card-header">Scenario Presets</div>
        <div className="scenario-buttons">
          {SCENARIOS.map(scenario => (
            <button
              key={scenario.name}
              className={`scenario-btn ${activeScenario === scenario.name ? 'active' : ''}`}
              onClick={() => runScenario(scenario)}
            >
              {scenario.name}
            </button>
          ))}
          <button className="scenario-btn" onClick={resetAll}>
            Reset All
          </button>
        </div>
      </div>

      {/* Component Grid */}
      <div className="card">
        <div className="card-header">Component Control</div>
        <div className="component-grid">
          {COMPONENTS.map(comp => (
            <div
              key={comp.key}
              className="component-item"
              onClick={() => setSelectedComponent(comp.key)}
              style={{ cursor: 'pointer' }}
            >
              <div className="component-name">{comp.key}</div>
              <div className="component-rates">
                Configured: {rates[comp.key] || comp.default_rate} RPS
              </div>
              <div className="component-rates">
                Observed: {observedRates[comp.key] || '-'} RPS
              </div>
              <input
                type="range"
                className="component-slider"
                min="0"
                max={comp.default_rate * 5}
                value={rates[comp.key] || comp.default_rate}
                onChange={(e) => setComponentRate(comp.key, parseInt(e.target.value))}
                onClick={(e) => e.stopPropagation()}
              />
              <button
                className="btn btn-danger"
                style={{ fontSize: '12px', padding: '4px 8px' }}
                onClick={(e) => {
                  e.stopPropagation();
                  silenceComponent(comp.key);
                }}
              >
                Silence
              </button>
            </div>
          ))}
        </div>
      </div>

      {/* Selected Component Details */}
      {selectedComponentData && (
        <div className="card">
          <div className="card-header">
            {selectedComponentData.key} Details
            <button className="btn btn-secondary" onClick={() => setSelectedComponent(null)}>
              Close
            </button>
          </div>
          <div style={{ fontSize: '14px' }}>
            <div><strong>Service:</strong> {selectedComponentData.service}</div>
            <div><strong>Component:</strong> {selectedComponentData.component}</div>
            <div><strong>Default Rate:</strong> {selectedComponentData.default_rate} RPS</div>
            <div><strong>Current Rate:</strong> {rates[selectedComponentData.key]} RPS</div>
            <div style={{ marginTop: '16px' }}>
              <strong>Dependencies:</strong>
              <div className="dependency-view">
                {getDependencies(selectedComponentData.key).length === 0 ? (
                  <div>No dependencies</div>
                ) : (
                  getDependencies(selectedComponentData.key).map((dep, i) => (
                    <div key={i} className="dependency-item">
                      {dep.from === selectedComponentData.key ? (
                        <>
                          {selectedComponentData.key}
                          <span className="dependency-arrow">→</span>
                          {dep.to} (coupling: {dep.coupling}, lag: {dep.lag_s}s)
                        </>
                      ) : (
                        <>
                          {dep.from}
                          <span className="dependency-arrow">→</span>
                          {selectedComponentData.key} (coupling: {dep.coupling}, lag: {dep.lag_s}s)
                        </>
                      )}
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default App;
