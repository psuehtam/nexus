import { useState, type SubmitEvent } from 'react';
import { Link } from 'react-router-dom';
import { useResource, write, type Installation, type Page } from './api';
import { useAuth } from './auth';
import { Alert, date, Note, number, Pager } from './ui';

interface WarrantyData { start_date?: string | null; end_date?: string | null; duration_months?: number | null; km_limit?: number | null; km_basis?: string | null; baseline_km?: number | null; installation_id?: string | null; conditions?: string | null; evidence?: string | null; }
interface Summary {
  warranty: { revision: number; data: WarrantyData; end_date?: string; days_remaining?: number; term_status: string; km_status: string; km_remaining?: number; km_observed_at?: string; missing: string[]; suggestion: { years?: number; km?: number; sources: Record<string, unknown> } };
  health: { soh_percent?: number; soh_observed_at?: string; cycle_count?: number; cycles_observed_at?: string; soh_threshold?: number; expected_cycles?: number; soh_limit_reached: boolean; cycle_limit_reached: boolean; field_sources: Record<string, unknown> };
}
const statusLabel: Record<string, string> = { unknown: 'Dados insuficientes', expired: 'Prazo encerrado', expiring: 'Termina nos próximos 90 dias', within_term: 'Dentro do prazo', not_started: 'Prazo ainda não iniciado', limit_reached: 'Limite atingido', below_limit_at_measurement: 'Abaixo do limite na última leitura' };
const display = (value?: number | null, unit = '') => value == null ? 'Não informado' : `${number(value)}${unit}`;
export function BatteryStatus({ id, installations }: { id: string; installations: Installation[] }) {
  const { user } = useAuth(); const result = useResource<Summary>(`/batteries/${id}/warranty`); const [editing, setEditing] = useState(false);
  if (!result.data) return <Alert error={result.error} />;
  const { warranty: w, health: h } = result.data;
  return <section className="panel"><div className="section-heading"><h2>Garantia e vida útil</h2>{user && <button className="button secondary" onClick={() => setEditing(!editing)}>{editing ? 'Fechar edição' : 'Editar garantia'}</button>}</div>
    <div className="form-grid"><div><h3>Garantia</h3><p>{statusLabel[w.term_status]}</p><p>Término: {w.end_date || 'Não informado'} · Dias restantes: {display(w.days_remaining)}</p><p>Quilometragem: {statusLabel[w.km_status]}</p><p>Saldo conforme leitura: {display(w.km_remaining, ' km')} · {w.km_observed_at ? date(w.km_observed_at) : 'Sem leitura válida'}</p><p>Condições: {w.data.conditions || 'Não informadas'}</p><p>Fonte: {w.data.evidence || 'Não informada'}</p>{w.missing.length > 0 && <small>Faltam dados: {w.missing.join('; ')}.</small>}</div>
    <div><h3>Vida útil e saúde</h3><p>SOH: {display(h.soh_percent, '%')} · {h.soh_observed_at ? date(h.soh_observed_at) : 'Sem medição'}</p><p>Limiar técnico: {display(h.soh_threshold, '%')}{h.soh_limit_reached ? ' · Limite atingido' : ''}</p><p>Ciclos: {display(h.cycle_count)} / {display(h.expected_cycles)} esperados{h.cycle_limit_reached ? ' · Limite atingido' : ''}</p><small>{h.cycles_observed_at ? `Medição: ${date(h.cycles_observed_at)}` : 'Sem medição de ciclos'}</small><p>Fontes técnicas: {Object.values(h.field_sources).map(String).join(' · ') || 'Não informadas'}</p></div></div>
    <Note>Garantia encerrada não determina inutilização da bateria. Os indicadores representam os dados cadastrados e não avaliam todas as condições contratuais.</Note>
    {editing && <WarrantyForm key={w.revision} id={id} summary={result.data} installations={installations} onSaved={() => { setEditing(false); result.reload(); }} />}
  </section>;
}
function WarrantyForm({ id, summary, installations, onSaved }: { id: string; summary: Summary; installations: Installation[]; onSaved: () => void }) {
  const original = summary.warranty.data;
  const [values, setValues] = useState<Record<string, string>>(() => Object.fromEntries(['start_date','end_date','duration_months','km_limit','km_basis','baseline_km','installation_id','conditions','evidence'].map(k => [k, String(original[k as keyof WarrantyData] ?? '')])));
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  const change = (key: string, value: string) => setValues(v => ({ ...v, [key]: value }));
  async function submit(e: SubmitEvent<HTMLFormElement>) { e.preventDefault(); setBusy(true); setError(''); const data: Record<string, unknown> = { revision: summary.warranty.revision }; for (const [k, v] of Object.entries(values)) data[k] = v === '' ? null : ['duration_months','km_limit','baseline_km'].includes(k) ? Number(v) : v; try { await write(`/batteries/${id}/warranty`, data, 'PUT'); onSaved(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } }
  return <form className="edit-form" onSubmit={submit}><Note>Referência do modelo, para conferência: {display(summary.warranty.suggestion.years, ' anos')} e {display(summary.warranty.suggestion.km, ' km')}. Confirme a aplicação a esta bateria antes de cadastrar. Fontes: {Object.values(summary.warranty.suggestion.sources).map(String).join(' · ') || 'Não informadas'}.</Note>
    <div className="form-grid">{[['start_date','Início','date'],['end_date','Término explícito','date'],['duration_months','Duração em meses','number'],['km_limit','Limite de quilometragem','number'],['baseline_km','Hodômetro inicial','number']].map(([key,label,type]) => <label className="field" key={key}><span>{label}</span><input type={type} min={type === 'number' ? 0 : undefined} step={key === 'duration_months' ? 1 : 'any'} value={values[key]} onChange={e => change(key,e.target.value)} /></label>)}
    <label className="field"><span>Base de contagem</span><select value={values.km_basis} onChange={e => change('km_basis',e.target.value)}><option value="">Não informada</option><option value="absolute">Hodômetro total do veículo</option><option value="since_start">Quilômetros desde o hodômetro inicial</option></select></label>
    <label className="field"><span>Instalação usada na garantia</span><select value={values.installation_id} onChange={e => change('installation_id',e.target.value)}><option value="">Não informada</option>{installations.map(i => <option key={i.id} value={i.id}>{i.vehicle?.public_identifier || i.public_identifier || i.vehicle?.id || i.id} · {i.installed_at ? date(i.installed_at) : 'Data pendente'}</option>)}</select></label></div>
    <label className="field"><span>Condições</span><textarea value={values.conditions} onChange={e => change('conditions',e.target.value)} /></label><label className="field"><span>Fonte / evidência</span><textarea required minLength={3} value={values.evidence} onChange={e => change('evidence',e.target.value)} /></label><Alert error={error} /><button className="button" disabled={busy}>{busy ? 'Salvando…' : 'Salvar garantia'}</button>
  </form>;
}
export function BatteryAlerts() {
  const [offset, setOffset] = useState(0); const result = useResource<Page<{ battery_id: string; identifier: string; reasons: string[] }>>(`/admin/battery-alerts?offset=${offset}`, 60000);
  const labels: Record<string,string> = { expired:'Prazo encerrado', expiring:'Garantia termina em até 90 dias', km_limit:'Limite de quilometragem atingido', soh_limit:'Limiar de SOH atingido', cycle_limit:'Ciclos esperados atingidos' };
  return <section className="panel"><h2>Alertas das baterias</h2><Alert error={result.error} />{result.data?.items.map(i => <Link className="history-row" to={`/baterias/${i.battery_id}`} key={i.battery_id}><strong>{i.identifier}</strong><span>{i.reasons.map(r => labels[r]).join(' · ')}</span></Link>)}{result.data?.total === 0 && <p>Nenhum alerta nos dados disponíveis.</p>}{result.data && <Pager total={result.data.total} offset={offset} setOffset={setOffset} />}</section>;
}
