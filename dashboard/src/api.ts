import { useCallback, useEffect, useState } from 'react';

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}
let csrfToken = '';
export function setCsrfToken(token: string) { csrfToken = token; }
export function randomId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body) headers.set('Content-Type', 'application/json');
  if (init.method && !['GET', 'HEAD'].includes(init.method.toUpperCase()) && csrfToken) headers.set('X-CSRF-Token', csrfToken);
  const response = await fetch('/api/v3' + path, { ...init, headers, credentials: 'same-origin', cache: 'no-store' });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : Array.isArray(data.detail) ? data.detail.map((d: {msg?: string}) => d.msg).join('; ') : data.detail?.message;
    throw new ApiError(detail || `Não foi possível concluir a solicitação (${response.status}).`, response.status);
  }
  return data as T;
}
const pendingWrites = new Map<string, string>();
export async function write<T>(path: string, body: unknown, method = 'POST') {
  const encoded = JSON.stringify(body); const logical = method + path + encoded;
  const key = pendingWrites.get(logical) || randomId(); pendingWrites.set(logical, key);
  try {
    const result = await api<T>(path, { method, body: encoded, headers: { 'Idempotency-Key': key } });
    pendingWrites.delete(logical); return result;
  } catch (error) {
    if (error instanceof ApiError && error.status < 500) pendingWrites.delete(logical);
    throw error;
  }
}
export function useResource<T>(path: string | null, interval = 0) {
  const [data, setData] = useState<T>(); const [error, setError] = useState(''); const [loading, setLoading] = useState(true); const [version, setVersion] = useState(0);
  const reload = useCallback(() => setVersion(n => n + 1), []);
  useEffect(() => {
    // Clear the previous resource when its URL changes, before the async response arrives.
    // oxlint-disable-next-line react/react-compiler
    if (!path) { setLoading(false); setData(undefined); setError(''); return; }
    // oxlint-disable-next-line react/react-compiler
    const controller = new AbortController(); setLoading(true); setError(''); setData(undefined);
    const load = () => api<T>(path, { signal: controller.signal }).then(value => { setData(value); setError(''); }).catch((e: Error) => { if (e.name !== 'AbortError') setError(e.message); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    void load(); const timer = interval ? window.setInterval(load, interval) : undefined;
    return () => { controller.abort(); if (timer) window.clearInterval(timer); };
  }, [path, version, interval]);
  return { data, error, loading, reload };
}
export interface Publication { id: string; reference_period: string; status?: string; total_units?: number; planned_units?: number; created_units?: number; coverage?: string; pending_classifications?: number; }
export interface Model { id: string; manufacturer: string; model: string; powertrain_type: string; vehicle_type?: string; vehicle_category: string; vehicle_subtype?: string; segment?: string; body_type?: string; is_heavy_vehicle?: boolean; classification_source?: string; classification_confidence?: string; quantity: number; units_created: number; identified_count?: number; batteries_registered: number; }
export interface Vehicle { id: string; public_identifier?: string; public_code?: string; sequence?: number; plate?: string; vin?: string; renavam?: string; state?: string; municipality?: string; identity_status?: string; registration_status?: string; battery_status?: string; battery_count?: number; revision?: number; manufacturer?: string; model?: string; powertrain_type?: string; vehicle_category?: string; }
export interface Battery { id: string; battery_identifier?: string; manufacturer_serial_number?: string; passport_identifier?: string; current_status?: string; soh_percent?: number; last_observed_at?: string; cycle_count?: number; manufacturer?: string; }
export interface Installation { id: string; battery?: Battery; vehicle?: Vehicle; public_identifier?: string; is_current?: boolean; installed_at?: string; removed_at?: string; position?: string; }
export interface Field { id: string; label: string; group: string; description?: string; }
export type FieldStatus = 'pending' | 'draft' | 'confirmed' | 'not_applicable';
export interface FieldValue { value: string; status: FieldStatus; source?: string; reason?: string; updated_at?: string; actor?: string; }
export interface Dossier { id: string; code: string; schema_version: string; revision: number; fields: Field[]; values: Record<string, FieldValue>; counts: Record<FieldStatus | 'total', number>; history: { revision: number; created_at?: string; actor?: string; reason?: string }[]; }
export interface Page<T> { items: T[]; total: number; limit: number; offset: number; publication?: Publication; }
export interface ModelsResponse extends Page<Model> { summary?: { published: number; registered: number; identified: number; with_battery: number }; }
export interface UnitsResponse extends Page<Vehicle> { model_group: Model; }
export interface VehicleResponse { vehicle: Vehicle; membership?: { public_identifier: string }; model_group?: Model; registration_status: string; installations: Installation[]; publication?: Publication; }
export interface BatteryEvent { id?: string; event_type?: string; type?: string; observed_at?: string; notes?: string; soh_percent?: number; odometer_km?: number; vehicle_id?: string; cycle_count?: number; }
export interface BatteryResponse { battery: Battery; battery_model?: { manufacturer?: string; nominal_capacity_kwh?: number }; installations: Installation[]; events: BatteryEvent[]; }
