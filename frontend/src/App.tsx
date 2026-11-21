import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import {
  DASHBOARD_QUERY,
  FINISH_MUTATION,
  QUOTE_QUERY,
  RESERVE_MUTATION,
  SET_FLAG_MUTATION,
  TELEMETRY_QUERY,
  gql,
  type Dashboard,
  type Quote,
  type Reading,
  type Saga,
} from "./api";

const POLL_MS = 2000;

const FLAG_LABELS: Record<string, string> = {
  ENABLE_DYNAMIC_SURGE_PRICING: "Tarifa dinámica (surge)",
  ENABLE_FAST_CHARGE_BOOST: "Ultra-carga prioritaria",
  SIMULATE_CHARGER_HARDWARE_FAULT: "Simular falla de cargador",
};

function useDashboard() {
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);

  const refresh = useCallback(async () => {
    try {
      setData(await gql<Dashboard>(DASHBOARD_QUERY));
      setError(null);
      setLastUpdate(new Date());
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  return { data, error, refresh, lastUpdate };
}

export default function App() {
  const { data, error, refresh, lastUpdate } = useDashboard();
  const [selected, setSelected] = useState<{ stationId: string; connectorId: string; type: string } | null>(null);
  const [lastSaga, setLastSaga] = useState<Saga | null>(null);

  if (!data) {
    return (
      <div className="loading">
        {error ? <p className="error">No se pudo conectar con /graphql: {error}</p> : <p>Conectando con AegisVolt…</p>}
      </div>
    );
  }

  const env = data.environment;
  return (
    <div className={`app env-${env.name}`}>
      <header>
        <div className="brand">
          <span className="bolt">⚡</span>
          <div>
            <h1>AegisVolt</h1>
            <small>Gestión de carga para flotas eléctricas</small>
          </div>
        </div>
        <div className="env">
          <span className={`env-badge ${env.name}`}>{env.name.toUpperCase()}</span>
          <small>
            versión {env.version} · rev {env.revision}
            <br />
            {error ? <span className="error">sin conexión</span> : `actualizado ${lastUpdate?.toLocaleTimeString()}`}
          </small>
        </div>
      </header>

      <main>
        <section className="panel flags">
          <h2>Feature Flags · {env.name}</h2>
          <FlagsPanel data={data} onChange={refresh} />
        </section>

        <section className="panel stations">
          <h2>Electrolineras</h2>
          <div className="station-grid">
            {data.stations.map((s) => (
              <div key={s.stationId} className="station">
                <div className="station-head">
                  <strong>{s.name}</strong>
                  <span>
                    {s.city} · {s.availableConnectors}/{s.connectors.length} libres
                  </span>
                </div>
                <div className="connectors">
                  {s.connectors.map((c) => {
                    const isSel = selected?.stationId === s.stationId && selected.connectorId === c.connectorId;
                    return (
                      <button
                        key={c.connectorId}
                        className={`connector ${c.status.toLowerCase()} ${isSel ? "selected" : ""}`}
                        disabled={c.status !== "AVAILABLE"}
                        onClick={() => setSelected({ stationId: s.stationId, connectorId: c.connectorId, type: c.type })}
                        title={c.reservedBy ? `SAGA ${c.reservedBy}` : "Disponible"}
                      >
                        <span className="cid">{c.connectorId}</span>
                        <span className="ctype">
                          {c.type} {c.powerKw} kW
                        </span>
                        <span className="cstatus">{c.status}</span>
                        {c.fleetId && <span className="cfleet">{c.fleetId}</span>}
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </section>

        <section className="panel reserve">
          <h2>Reservar y activar carga</h2>
          <ReserveForm
            data={data}
            selected={selected}
            onDone={(s) => {
              setLastSaga(s);
              setSelected(null);
              refresh();
            }}
          />
          {lastSaga && <SagaCard saga={lastSaga} highlight />}
        </section>

        <section className="panel active">
          <h2>Sesiones de carga activas</h2>
          <ActiveSessions sagas={data.sagas.filter((s) => s.status === "CHARGING")} onChange={refresh} />
        </section>

        <section className="panel history">
          <h2>Historial de transacciones SAGA</h2>
          <SagaHistory sagas={data.sagas} />
        </section>

        <section className="panel wallets">
          <h2>Billeteras y movimientos</h2>
          <table>
            <tbody>
              {data.wallets.map((w) => (
                <tr key={w.fleetId}>
                  <td>
                    <strong>{w.fleetId}</strong>
                    <br />
                    <small>{w.name}</small>
                  </td>
                  <td className="num">
                    {w.balance.toFixed(2)} {w.currency}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>Últimos movimientos</h3>
          <table className="tx">
            <thead>
              <tr>
                <th>Tipo</th>
                <th>Flota</th>
                <th className="num">Monto</th>
                <th>Estado</th>
              </tr>
            </thead>
            <tbody>
              {data.transactions.map((t) => (
                <tr key={`${t.sagaId}-${t.type}`} className={`tx-${t.type.toLowerCase()}`}>
                  <td>
                    {t.type}
                    {t.surgeApplied && <span className="tag surge">surge</span>}
                  </td>
                  <td>{t.fleetId}</td>
                  <td className="num">{(t.type === "CHARGE" ? -t.amount : t.amount).toFixed(2)}</td>
                  <td>
                    {t.status}
                    {t.reason && <small className="block">{t.reason}</small>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </main>
    </div>
  );
}

function FlagsPanel({ data, onChange }: { data: Dashboard; onChange: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const admin = data.environment.flagAdminEnabled;

  async function toggle(name: string, enabled: boolean) {
    setBusy(name);
    setErr(null);
    try {
      await gql(SET_FLAG_MUTATION, { n: name, e: enabled });
      onChange();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <ul className="flag-list">
        {data.featureFlags.map((f) => (
          <li key={f.name} className={f.enabled ? "on" : "off"}>
            <label className="switch">
              <input
                type="checkbox"
                checked={f.enabled}
                disabled={!admin || busy === f.name}
                onChange={(e) => toggle(f.name, e.target.checked)}
              />
              <span className="slider" />
            </label>
            <div>
              <strong>{FLAG_LABELS[f.name] ?? f.name}</strong>
              <code>{f.name}</code>
              <small>{f.description}</small>
              {f.updatedBy && (
                <small className="muted">
                  cambiada por {f.updatedBy} {f.updatedAt ? `· ${new Date(f.updatedAt).toLocaleTimeString()}` : ""}
                </small>
              )}
            </div>
          </li>
        ))}
      </ul>
      {!admin && <p className="muted">Solo lectura en {data.environment.name}: las flags se cambian por proceso controlado.</p>}
      {err && <p className="error">{err}</p>}
    </>
  );
}

function ReserveForm({
  data,
  selected,
  onDone,
}: {
  data: Dashboard;
  selected: { stationId: string; connectorId: string; type: string } | null;
  onDone: (s: Saga) => void;
}) {
  const [fleetId, setFleetId] = useState(data.wallets[0]?.fleetId ?? "");
  const [kwh, setKwh] = useState(30);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const flagsKey = useMemo(() => data.featureFlags.map((f) => `${f.name}=${f.enabled}`).join(), [data.featureFlags]);

  useEffect(() => {
    if (!selected || kwh <= 0) return setQuote(null);
    gql<{ priceQuote: Quote }>(QUOTE_QUERY, { t: selected.type, k: kwh })
      .then((r) => setQuote(r.priceQuote))
      .catch(() => setQuote(null));
  }, [selected, kwh, flagsKey]);

  async function submit() {
    if (!selected) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await gql<{ reserveAndStartCharging: Saga }>(RESERVE_MUTATION, {
        i: { stationId: selected.stationId, connectorId: selected.connectorId, fleetId, energyKwh: kwh, idempotencyKey: crypto.randomUUID() },
      });
      onDone(r.reserveAndStartCharging);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="reserve-form">
      <p>
        Conector:{" "}
        {selected ? (
          <strong>
            {selected.stationId} / {selected.connectorId} ({selected.type})
          </strong>
        ) : (
          <em>selecciona un conector disponible en el panel de electrolineras</em>
        )}
      </p>
      <label>
        Flota
        <select value={fleetId} onChange={(e) => setFleetId(e.target.value)}>
          {data.wallets.map((w) => (
            <option key={w.fleetId} value={w.fleetId}>
              {w.fleetId} — {w.balance.toFixed(2)} USD
            </option>
          ))}
        </select>
      </label>
      <label>
        Energía (kWh)
        <input type="number" min={1} max={500} value={kwh} onChange={(e) => setKwh(Number(e.target.value))} />
      </label>
      {quote && (
        <div className={`quote ${quote.multiplier > 1 ? "surge" : ""}`}>
          <span>
            {quote.pricePerKwh.toFixed(4)} USD/kWh × {kwh} kWh = <strong>{quote.amount.toFixed(2)} USD</strong>
          </span>
          {quote.multiplier > 1 ? (
            <span className="tag surge">SURGE ×{quote.multiplier} (hora pico {quote.peakWindow})</span>
          ) : (
            <span className="tag">tarifa base {quote.basePricePerKwh.toFixed(2)}</span>
          )}
        </div>
      )}
      <button className="primary" disabled={!selected || busy} onClick={submit}>
        {busy ? "Ejecutando SAGA…" : "Reservar y activar carga"}
      </button>
      {err && <p className="error">{err}</p>}
    </div>
  );
}

const STATUS_CLASS: Record<string, string> = {
  CHARGING: "ok",
  FINISHED: "ok",
  COMPENSATED: "warn",
  FAILED: "bad",
  COMPENSATION_FAILED: "bad",
  COMPENSATING: "warn",
};

function SagaCard({ saga, highlight = false }: { saga: Saga; highlight?: boolean }) {
  return (
    <div className={`saga-card ${highlight ? "highlight" : ""}`}>
      <div className="saga-head">
        <span className={`status ${STATUS_CLASS[saga.status] ?? ""}`}>{saga.status}</span>
        <span>
          {saga.stationId}/{saga.connectorId} · {saga.fleetId} · {saga.energyKwh} kWh
          {saga.amount != null && ` · ${saga.amount.toFixed(2)} USD`}
        </span>
      </div>
      <code className="cid">correlation_id: {saga.correlationId}</code>
      {saga.errorCode && (
        <p className="error">
          Falló {saga.failedStep}: {saga.errorCode} — {saga.errorMessage}
        </p>
      )}
      <ol className="steps">
        {saga.steps.map((s, i) => (
          <li key={i} className={`${s.action.toLowerCase()} ${s.status.toLowerCase()}`}>
            <span className="step-icon">{s.action === "COMPENSATE" ? "↩" : s.status === "SUCCEEDED" ? "✓" : "✗"}</span>
            <strong>{s.name}</strong>
            <span className="muted">
              {s.action === "COMPENSATE" ? "compensación" : "paso"}
              {s.attempts > 1 && ` · ${s.attempts} intentos`}
            </span>
            <small>{s.detail}</small>
          </li>
        ))}
      </ol>
    </div>
  );
}

function ActiveSessions({ sagas, onChange }: { sagas: Saga[]; onChange: () => void }) {
  if (!sagas.length) return <p className="muted">No hay cargas en curso.</p>;
  return (
    <div className="sessions">
      {sagas.map((s) => (
        <SessionRow key={s.sagaId} saga={s} onChange={onChange} />
      ))}
    </div>
  );
}

function SessionRow({ saga, onChange }: { saga: Saga; onChange: () => void }) {
  const [r, setR] = useState<Reading | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () =>
      gql<{ telemetry: { current: Reading | null } }>(TELEMETRY_QUERY, { s: saga.sagaId })
        .then((d) => alive && setR(d.telemetry.current))
        .catch(() => undefined);
    load();
    const id = setInterval(load, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [saga.sagaId]);

  async function finish() {
    setBusy(true);
    try {
      await gql(FINISH_MUTATION, { s: saga.sagaId });
      onChange();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="session">
      <div>
        <strong>
          {saga.stationId}/{saga.connectorId}
        </strong>{" "}
        · {saga.fleetId}
      </div>
      <div className="progress">
        <div className="bar" style={{ width: `${r?.progressPct ?? 0}%` }} />
        <span>
          {r ? `${r.energyKwh.toFixed(1)} / ${saga.energyKwh} kWh (${r.progressPct.toFixed(0)}%)` : "…"}
        </span>
      </div>
      {r && (
        <small className="telemetry">
          {r.powerKw.toFixed(1)} kW · {r.voltageV} V · {r.currentA} A · {r.temperatureC} °C · {r.elapsedMin} min
        </small>
      )}
      <button disabled={busy} onClick={finish}>
        Finalizar carga
      </button>
    </div>
  );
}

function SagaHistory({ sagas }: { sagas: Saga[] }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!sagas.length) return <p className="muted">Aún no hay transacciones.</p>;
  return (
    <table className="history-table">
      <thead>
        <tr>
          <th>Hora</th>
          <th>Estado</th>
          <th>Conector</th>
          <th>Flota</th>
          <th>Compensaciones</th>
        </tr>
      </thead>
      <tbody>
        {sagas.map((s) => {
          const comps = s.steps.filter((x) => x.action === "COMPENSATE");
          return (
            <Fragment key={s.sagaId}>
              <tr onClick={() => setOpen(open === s.sagaId ? null : s.sagaId)} className="clickable">
                <td>{new Date(s.createdAt).toLocaleTimeString()}</td>
                <td>
                  <span className={`status ${STATUS_CLASS[s.status] ?? ""}`}>{s.status}</span>
                </td>
                <td>
                  {s.stationId}/{s.connectorId}
                </td>
                <td>{s.fleetId}</td>
                <td>
                  {comps.length
                    ? comps.map((c) => (
                        <span key={c.name} className={`tag ${c.status === "SUCCEEDED" ? "ok" : "bad"}`}>
                          {c.name}
                        </span>
                      ))
                    : "—"}
                </td>
              </tr>
              {open === s.sagaId && (
                <tr>
                  <td colSpan={5}>
                    <SagaCard saga={s} />
                  </td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </tbody>
    </table>
  );
}
