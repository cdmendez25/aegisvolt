// Cliente GraphQL mínimo: el endpoint es relativo (/graphql), así que cada
// ambiente (dev/test/prod) habla con SU propio gateway a través del Ingress.
export const GRAPHQL_URL = import.meta.env.VITE_GRAPHQL_URL ?? "/graphql";

export async function gql<T>(query: string, variables: Record<string, unknown> = {}): Promise<T> {
  const res = await fetch(GRAPHQL_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ query, variables }),
  });
  const body = await res.json();
  if (body.errors?.length) throw new Error(body.errors.map((e: { message: string }) => e.message).join("; "));
  return body.data as T;
}

export type ConnectorStatus = "AVAILABLE" | "RESERVED" | "CHARGING" | "OUT_OF_SERVICE";

export interface Connector {
  connectorId: string;
  type: "FAST" | "ULTRA";
  powerKw: number;
  status: ConnectorStatus;
  reservedBy: string | null;
  fleetId: string | null;
}
export interface Station {
  stationId: string;
  name: string;
  city: string;
  availableConnectors: number;
  connectors: Connector[];
}
export interface Wallet {
  fleetId: string;
  name: string;
  balance: number;
  currency: string;
}
export interface FeatureFlag {
  name: string;
  enabled: boolean;
  description: string;
  updatedAt: string | null;
  updatedBy: string | null;
}
export interface EnvInfo {
  name: string;
  version: string;
  revision: string;
  flagAdminEnabled: boolean;
}
export interface SagaStep {
  name: string;
  action: "EXECUTE" | "COMPENSATE";
  status: "SUCCEEDED" | "FAILED";
  detail: string;
  attempts: number;
  at: string;
}
export interface Saga {
  sagaId: string;
  correlationId: string;
  status: string;
  stationId: string;
  connectorId: string;
  fleetId: string;
  energyKwh: number;
  amount: number | null;
  connectorType: string | null;
  failedStep: string | null;
  errorCode: string | null;
  errorMessage: string | null;
  createdAt: string;
  steps: SagaStep[];
}
export interface WalletTx {
  sagaId: string;
  fleetId: string;
  type: string;
  amount: number;
  status: string;
  reason: string | null;
  createdAt: string;
  surgeApplied: boolean;
}
export interface Quote {
  amount: number;
  multiplier: number;
  pricePerKwh: number;
  basePricePerKwh: number;
  peakHour: boolean;
  surgeEnabled: boolean;
  peakWindow: string;
}
export interface Reading {
  powerKw: number;
  energyKwh: number;
  progressPct: number;
  voltageV: number;
  currentA: number;
  temperatureC: number;
  elapsedMin: number;
  completed: boolean;
}

const SAGA_FIELDS = `sagaId correlationId status stationId connectorId fleetId energyKwh amount connectorType
  failedStep errorCode errorMessage createdAt steps { name action status detail attempts at }`;

export const DASHBOARD_QUERY = `{
  environment { name version revision flagAdminEnabled }
  featureFlags { name enabled description updatedAt updatedBy }
  stations { stationId name city availableConnectors
    connectors { connectorId type powerKw status reservedBy fleetId } }
  wallets { fleetId name balance currency }
  sagas(limit: 25) { ${SAGA_FIELDS} }
  transactions(limit: 25) { sagaId fleetId type amount status reason createdAt surgeApplied }
}`;

export interface Dashboard {
  environment: EnvInfo;
  featureFlags: FeatureFlag[];
  stations: Station[];
  wallets: Wallet[];
  sagas: Saga[];
  transactions: WalletTx[];
}

export const RESERVE_MUTATION = `mutation($i: ReserveAndStartChargingInput!) {
  reserveAndStartCharging(input: $i) { ${SAGA_FIELDS} } }`;

export const FINISH_MUTATION = `mutation($s: String!) { finishCharging(sagaId: $s) { ${SAGA_FIELDS} } }`;

export const SET_FLAG_MUTATION = `mutation($n: String!, $e: Boolean!) {
  setFeatureFlag(name: $n, enabled: $e) { name enabled updatedAt updatedBy } }`;

export const QUOTE_QUERY = `query($t: String!, $k: Float!) {
  priceQuote(connectorType: $t, energyKwh: $k) {
    amount multiplier pricePerKwh basePricePerKwh peakHour surgeEnabled peakWindow } }`;

export const TELEMETRY_QUERY = `query($s: String!) { telemetry(sagaId: $s) {
  current { powerKw energyKwh progressPct voltageV currentA temperatureC elapsedMin completed } } }`;
